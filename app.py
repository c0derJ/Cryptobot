"""
CRYPTOBOT - Flask Web Server
"""
import os
import logging
import threading
import time
from datetime import datetime

# Flask first — must work before anything else
from flask import Flask, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'cryptobot-2024')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# Global state
bot_running     = False
_initialized    = False
system_log      = []
scan_count      = 0
last_scan       = None
last_signals    = {}
last_indicators = {}
last_patterns   = {}
prices_cache    = {}
ACTIVE_PAIRS    = os.getenv('ACTIVE_PAIRS', 'BTC,ETH,SOL,BNB').split(',')
SCAN_INTERVAL   = int(os.getenv('SCAN_INTERVAL', 60))
PAPER_TRADING   = os.getenv('PAPER_TRADING', 'true').lower() == 'true'


def add_log(msg, level='info'):
    entry = {'time': datetime.now().strftime('%H:%M:%S'), 'message': msg, 'level': level}
    system_log.append(entry)
    if len(system_log) > 300:
        system_log.pop(0)
    try:
        socketio.emit('log', entry)
    except:
        pass
    log.info(f"[{level.upper()}] {msg}")


# ── HEALTHCHECK ROUTE — must respond immediately ──
@app.route('/api/status')
def api_status():
    return jsonify({
        'bot_running': bot_running,
        'paper_mode':  PAPER_TRADING,
        'scan_count':  scan_count,
        'last_scan':   last_scan,
        'pairs':       ACTIVE_PAIRS,
        'uptime':      'active',
    })

@app.route('/')
def index():
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'index.html')
        with open(path, 'r', encoding='utf-8') as f:
            return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}
    except Exception as e:
        return f'<h1>CRYPTOBOT Running</h1><p>Dashboard loading... {str(e)}</p>', 200

@app.route('/api/prices')
def api_prices():
    try:
        from engine import get_all_prices, get_24h_stats
        prices = get_all_prices()
        stats = {pair: get_24h_stats(pair) for pair in ACTIVE_PAIRS}
        return jsonify({'prices': prices, 'stats': stats})
    except Exception as e:
        return jsonify({'prices': prices_cache, 'stats': {}, 'error': str(e)})

@app.route('/api/state')
def api_state():
    try:
        from engine import get_all_states
        return jsonify({
            'states': get_all_states(), 'signals': last_signals,
            'indicators': last_indicators, 'patterns': last_patterns,
            'last_scan': last_scan,
        })
    except Exception as e:
        return jsonify({'states': {}, 'signals': {}, 'error': str(e)})

@app.route('/api/brain')
def api_brain():
    try:
        from ai_brain import get_brain_summary
        return jsonify(get_brain_summary())
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/logs')
def api_logs():
    return jsonify({'logs': system_log[-60:]})

@app.route('/api/bot/start', methods=['POST'])
def bot_start():
    global bot_running
    bot_running = True
    add_log('Bot started', 'success')
    threading.Thread(target=run_all_scans, daemon=True).start()
    return jsonify({'status': 'started'})

@app.route('/api/bot/stop', methods=['POST'])
def bot_stop():
    global bot_running
    bot_running = False
    add_log('Bot stopped', 'error')
    return jsonify({'status': 'stopped'})

@app.route('/api/scan/now', methods=['POST'])
def scan_now():
    threading.Thread(target=run_all_scans, daemon=True).start()
    return jsonify({'status': 'triggered'})


# ── WEBSOCKET ──
@socketio.on('connect')
def on_connect():
    try:
        emit('status', {'bot_running': bot_running, 'pairs': ACTIVE_PAIRS})
        emit('logs', {'logs': system_log[-30:]})
    except:
        pass


# ── BOT LOGIC — imported lazily to avoid startup crashes ──
def scan_pair(pair):
    global last_signals, last_indicators, last_patterns
    try:
        from engine import (get_ohlcv, calculate_indicators, get_indicator_snapshot,
                           detect_patterns, generate_signal, paper_trade, get_pair_state)
        from ai_brain import analyze_trade, get_weights

        add_log(f'[{pair}] Scanning...', 'info')
        df = get_ohlcv(pair, interval='1h', limit=100)
        if df is None or len(df) < 60:
            add_log(f'[{pair}] No data', 'error')
            return

        df = calculate_indicators(df)
        indicators = get_indicator_snapshot(df)
        last_indicators[pair] = indicators

        patterns = detect_patterns(df)
        last_patterns[pair] = patterns
        if patterns:
            add_log(f'[{pair}] {", ".join([p["name"] for p in patterns[:3]])}', 'info')

        weights = get_weights()
        signal_data = generate_signal(indicators, patterns, 0.0, weights)
        last_signals[pair] = signal_data

        add_log(
            f'[{pair}] RSI:{indicators["rsi"]:.0f} | Signal:{signal_data["signal"]} ({signal_data["confidence"]:.0f}%)',
            'success' if signal_data['signal'] != 'HOLD' else 'info'
        )

        result, closed_trade = paper_trade(pair, signal_data, indicators['price'])
        if result['action'] == 'open':
            add_log(result['message'], 'success')
        elif result['action'] == 'close':
            add_log(result['message'], 'success' if result['pnl'] > 0 else 'error')
            if closed_trade:
                mem = analyze_trade(closed_trade, patterns, indicators, 0.0, pair)
                if mem:
                    add_log(f'[{pair}] AI: {mem.get("verdict")} | {mem.get("key_lesson","")}', 'sol')

        try:
            socketio.emit('pair_update', {
                'pair': pair, 'indicators': indicators,
                'patterns': patterns, 'signal': signal_data,
                'state': get_pair_state(pair),
            })
        except:
            pass

    except Exception as e:
        add_log(f'[{pair}] Error: {str(e)}', 'error')
        log.exception(f"Scan error for {pair}")


def run_all_scans():
    global scan_count, last_scan
    if not bot_running:
        return
    scan_count += 1
    add_log(f'=== Scan #{scan_count} — All pairs ===', 'info')
    for pair in ACTIVE_PAIRS:
        scan_pair(pair)
        time.sleep(2)
    last_scan = datetime.now().isoformat()
    try:
        socketio.emit('scan_complete', {'scan_count': scan_count, 'last_scan': last_scan})
    except:
        pass


def price_ticker():
    while True:
        try:
            from engine import get_all_prices
            prices = get_all_prices()
            if prices:
                prices_cache.update(prices)
                socketio.emit('prices', prices)
        except:
            pass
        time.sleep(3)


def initialize():
    global bot_running, _initialized
    if _initialized:
        return
    _initialized = True

    log.info('CRYPTOBOT starting up...')

    # Start price ticker
    threading.Thread(target=price_ticker, daemon=True).start()

    # Start scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(run_all_scans, 'interval', minutes=SCAN_INTERVAL, id='scan_job')
        scheduler.start()
        log.info('Scheduler started')
    except Exception as e:
        log.error(f'Scheduler error: {e}')

# Don't auto-start - wait for user to click START
bot_running = False

# Just log that system is ready
add_log('CRYPTOBOT ready — Press START to begin', 'sol')
    log.info('CRYPTOBOT initialized successfully')


# Run initialize in background thread so it never blocks gunicorn startup
threading.Thread(target=initialize, daemon=True).start()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
