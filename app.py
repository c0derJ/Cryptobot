"""
CRYPTOBOT - Flask Web Server
Auto-starts on Railway, runs 24/7, serves dashboard
"""

# ⚠️ CRITICAL: Must run eventlet.monkey_patch() BEFORE importing ANY other modules
import eventlet
eventlet.monkey_patch()

import os
import sys
import logging
import threading
import time
from datetime import datetime

# Flask imports
from flask import Flask, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'cryptobot-2024')
CORS(app)

# Initialize SocketIO with eventlet
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# Global state
bot_running = False
_initialized = False
system_log = []
scan_count = 0
last_scan = None
last_signals = {}
last_indicators = {}
last_patterns = {}
prices_cache = {}
ACTIVE_PAIRS = os.getenv('ACTIVE_PAIRS', 'BTC,ETH,SOL,BNB').split(',')
SCAN_INTERVAL = int(os.getenv('SCAN_INTERVAL', 60))
PAPER_TRADING = os.getenv('PAPER_TRADING', 'true').lower() == 'true'
scheduler = None


def add_log(msg, level='info'):
    """Add log entry and broadcast via WebSocket."""
    entry = {
        'time': datetime.now().strftime('%H:%M:%S'),
        'message': msg,
        'level': level
    }
    system_log.append(entry)
    if len(system_log) > 300:
        system_log.pop(0)
    try:
        socketio.emit('log', entry)
    except Exception:
        pass
    log.info(f"[{level.upper()}] {msg}")


# ═══════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/api/status')
def api_status():
    """Healthcheck endpoint - must respond quickly."""
    return jsonify({
        'bot_running': bot_running,
        'paper_mode': PAPER_TRADING,
        'scan_count': scan_count,
        'last_scan': last_scan,
        'pairs': ACTIVE_PAIRS,
        'uptime': 'active',
        'status': 'ok'
    })


@app.route('/')
def index():
    """Serve the dashboard HTML."""
    try:
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
        index_path = os.path.join(static_dir, 'index.html')
        
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}
        else:
            # Fallback to inline HTML if file doesn't exist
            return '''
            <!DOCTYPE html>
            <html>
            <head>
                <title>CRYPTOBOT</title>
                <style>
                    body { background: #03050a; color: #e8f0ff; font-family: monospace; padding: 20px; }
                    h1 { color: #00cfff; }
                </style>
            </head>
            <body>
                <h1>🤖 CRYPTOBOT Running</h1>
                <p>Dashboard loaded successfully. API is operational.</p>
                <p>Status: <span id="status">Loading...</span></p>
                <script>
                    fetch('/api/status')
                        .then(r => r.json())
                        .then(d => document.getElementById('status').textContent = d.bot_running ? 'Running' : 'Stopped');
                </script>
            </body>
            </html>
            ''', 200, {'Content-Type': 'text/html; charset=utf-8'}
    except Exception as e:
        log.error(f"Error serving index: {e}")
        return f'<h1>CRYPTOBOT Running</h1><p>Dashboard loading... {str(e)}</p>', 200


@app.route('/api/prices')
def api_prices():
    """Get current prices for all pairs."""
    try:
        from engine import get_all_prices, get_24h_stats
        prices = get_all_prices()
        stats = {pair: get_24h_stats(pair) for pair in ACTIVE_PAIRS}
        return jsonify({'prices': prices, 'stats': stats})
    except Exception as e:
        log.error(f"Price API error: {e}")
        return jsonify({'prices': prices_cache, 'stats': {}, 'error': str(e)})


@app.route('/api/state')
def api_state():
    """Get current trading state for all pairs."""
    try:
        from engine import get_all_states
        return jsonify({
            'states': get_all_states(),
            'signals': last_signals,
            'indicators': last_indicators,
            'patterns': last_patterns,
            'last_scan': last_scan,
        })
    except Exception as e:
        log.error(f"State API error: {e}")
        return jsonify({'states': {}, 'signals': {}, 'error': str(e)})


@app.route('/api/brain')
def api_brain():
    """Get AI brain analysis summary."""
    try:
        from ai_brain import get_brain_summary
        return jsonify(get_brain_summary())
    except Exception as e:
        log.error(f"Brain API error: {e}")
        return jsonify({'error': str(e), 'total_analyses': 0, 'current_weights': {}})


@app.route('/api/logs')
def api_logs():
    """Get recent system logs."""
    return jsonify({'logs': system_log[-60:]})


@app.route('/api/bot/start', methods=['POST'])
def bot_start():
    """Start the trading bot."""
    global bot_running
    if not bot_running:
        bot_running = True
        add_log('Bot started — scanning ' + ', '.join(ACTIVE_PAIRS), 'success')
        # Trigger first scan immediately
        threading.Thread(target=run_all_scans, daemon=True).start()
    return jsonify({'status': 'started'})


@app.route('/api/bot/stop', methods=['POST'])
def bot_stop():
    """Stop the trading bot."""
    global bot_running
    if bot_running:
        bot_running = False
        add_log('Bot stopped', 'error')
    return jsonify({'status': 'stopped'})


@app.route('/api/scan/now', methods=['POST'])
def scan_now():
    """Trigger an immediate manual scan."""
    if bot_running:
        threading.Thread(target=run_all_scans, daemon=True).start()
        add_log('Manual scan triggered', 'info')
    else:
        add_log('Cannot scan - bot not running', 'warning')
    return jsonify({'status': 'triggered'})


# ═══════════════════════════════════════════════════════════════
# WEBSOCKET EVENTS
# ═══════════════════════════════════════════════════════════════

@socketio.on('connect')
def on_connect():
    """Handle client WebSocket connection."""
    try:
        emit('status', {'bot_running': bot_running, 'pairs': ACTIVE_PAIRS})
        emit('logs', {'logs': system_log[-30:]})
        emit('state', {
            'states': {},
            'signals': last_signals,
            'indicators': last_indicators,
            'prices': prices_cache,
        })
        add_log('Client connected to WebSocket', 'info')
    except Exception as e:
        log.error(f"WebSocket connect error: {e}")


@socketio.on('disconnect')
def on_disconnect():
    """Handle client disconnection."""
    pass


# ═══════════════════════════════════════════════════════════════
# BOT CORE LOGIC
# ═══════════════════════════════════════════════════════════════

def scan_pair(pair):
    """Full analysis cycle for one pair."""
    global last_signals, last_indicators, last_patterns
    
    if not bot_running:
        return
        
    try:
        from engine import (get_ohlcv, calculate_indicators, get_indicator_snapshot,
                           detect_patterns, generate_signal, paper_trade, get_pair_state)
        from ai_brain import analyze_trade, get_weights

        add_log(f'[{pair}] Scanning...', 'info')
        
        # Fetch OHLCV data
        df = get_ohlcv(pair, interval='1h', limit=100)
        if df is None or len(df) < 60:
            add_log(f'[{pair}] Insufficient data (got {len(df) if df is not None else 0} candles)', 'error')
            return

        # Calculate indicators
        df = calculate_indicators(df)
        indicators = get_indicator_snapshot(df)
        last_indicators[pair] = indicators

        # Detect patterns
        patterns = detect_patterns(df)
        last_patterns[pair] = patterns
        if patterns:
            pattern_names = ', '.join([p['name'] for p in patterns[:3]])
            add_log(f'[{pair}] Patterns: {pattern_names}', 'info')

        # Generate trading signal
        weights = get_weights()
        signal_data = generate_signal(indicators, patterns, 0.0, weights)
        last_signals[pair] = signal_data

        add_log(
            f'[{pair}] RSI:{indicators["rsi"]:.0f} | MACD:{"✓" if indicators["macd_bull"] else "✗"} | '
            f'Signal:{signal_data["signal"]} ({signal_data["confidence"]:.0f}%)',
            'success' if signal_data['signal'] != 'HOLD' else 'info'
        )

        # Execute paper trade
        result, closed_trade = paper_trade(pair, signal_data, indicators['price'])
        
        if result['action'] == 'open':
            add_log(result['message'], 'success')
        elif result['action'] == 'close':
            add_log(result['message'], 'success' if result['pnl'] > 0 else 'error')
            # AI analysis for closed trades (non-blocking)
            if closed_trade:
                try:
                    memory = analyze_trade(closed_trade, patterns, indicators, 0.0, pair)
                    if memory and memory.get('verdict'):
                        add_log(f'[{pair}] AI: {memory["verdict"]} | {memory.get("key_lesson", "")}', 'sol')
                except Exception as e:
                    log.error(f"AI analysis error for {pair}: {e}")

        # Broadcast update via WebSocket
        try:
            socketio.emit('pair_update', {
                'pair': pair,
                'indicators': indicators,
                'patterns': patterns,
                'signal': signal_data,
                'state': get_pair_state(pair),
            })
        except Exception as e:
            log.debug(f"WebSocket emit error: {e}")

    except Exception as e:
        add_log(f'[{pair}] Scan error: {str(e)[:100]}', 'error')
        log.exception(f"Scan failed for {pair}")


def run_all_scans():
    """Scan all active pairs sequentially."""
    global scan_count, last_scan
    
    if not bot_running:
        return
        
    scan_count += 1
    add_log(f'=== Scan #{scan_count} — Scanning {len(ACTIVE_PAIRS)} pairs ===', 'info')
    
    for pair in ACTIVE_PAIRS:
        if not bot_running:
            break
        scan_pair(pair)
        time.sleep(1)  # Small delay between pairs
    
    last_scan = datetime.now().isoformat()
    try:
        socketio.emit('scan_complete', {'scan_count': scan_count, 'last_scan': last_scan})
    except Exception:
        pass


def price_ticker():
    """Background thread to broadcast live prices."""
    last_emit_time = 0
    while True:
        try:
            # Rate limit to avoid flooding
            if time.time() - last_emit_time >= 2:
                from engine import get_all_prices
                prices = get_all_prices()
                if prices:
                    prices_cache.update(prices)
                    socketio.emit('prices', prices)
                    last_emit_time = time.time()
        except Exception as e:
            log.debug(f"Price ticker error: {e}")
        time.sleep(2)


def scheduler_worker():
    """Background thread for scheduled scans."""
    global scheduler
    
    # Wait for initial startup
    time.sleep(5)
    
    # Run first scan after 10 seconds if bot is running
    time.sleep(5)
    if bot_running:
        add_log('Running initial scan...', 'info')
        run_all_scans()
    
    # Then run on interval
    while True:
        if bot_running:
            time.sleep(SCAN_INTERVAL * 60)
            if bot_running:
                run_all_scans()
        else:
            time.sleep(10)


def initialize():
    """Initialize the bot in background thread."""
    global _initialized, scheduler
    
    if _initialized:
        return
    _initialized = True

    add_log(f'CRYPTOBOT initializing — Pairs: {", ".join(ACTIVE_PAIRS)} | Paper Trading: {"ON" if PAPER_TRADING else "OFF"}', 'sol')
    log.info('CRYPTOBOT initialization started')

    # Start price ticker thread
    ticker_thread = threading.Thread(target=price_ticker, daemon=True)
    ticker_thread.start()
    log.info('Price ticker started')

    # Start scheduler thread
    scheduler_thread = threading.Thread(target=scheduler_worker, daemon=True)
    scheduler_thread.start()
    log.info('Scan scheduler started')

    add_log('CRYPTOBOT ready — Press START to begin trading', 'success')
    log.info('CRYPTOBOT initialized successfully')


# ═══════════════════════════════════════════════════════════════
# APPLICATION STARTUP
# ═══════════════════════════════════════════════════════════════

# Set initial bot state
bot_running = False

# Start initialization in background (non-blocking)
threading.Thread(target=initialize, daemon=True).start()

# Log that app is ready
log.info('Flask application loaded and ready')

# Main entry point for local development
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
