"""
CRYPTOBOT - Flask Web Server
Auto-starts on Railway, runs 24/7, serves dashboard
"""

import os
import logging
import threading
import time
from datetime import datetime
from flask import Flask, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from engine import (
    get_all_prices, get_price, get_ohlcv, get_24h_stats,
    calculate_indicators, get_indicator_snapshot,
    detect_patterns, generate_signal, paper_trade,
    get_pair_state, get_all_states, ACTIVE_PAIRS, PAPER_TRADING
)
from ai_brain import analyze_trade, get_brain_summary, get_weights

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'cryptobot-2024')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

bot_running     = False
_initialized    = False
scheduler       = BackgroundScheduler()
system_log      = []
scan_count      = 0
last_scan       = None
last_signals    = {}
last_indicators = {}
last_patterns   = {}
prices_cache    = {}
SCAN_INTERVAL   = int(os.getenv('SCAN_INTERVAL', 60))


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


def scan_pair(pair):
    global last_signals, last_indicators, last_patterns
    add_log(f'[{pair}] Scanning...', 'info')
    try:
        df = get_ohlcv(pair, interval='1h', limit=100)
        if df is None or len(df) < 60:
            add_log(f'[{pair}] Insufficient data', 'error')
            return
        df = calculate_indicators(df)
        indicators = get_indicator_snapshot(df)
        last_indicators[pair] = indicators
        patterns = detect_patterns(df)
        last_patterns[pair] = patterns
        if patterns:
            add_log(f'[{pair}] Patterns: {", ".join([p["name"] for p in patterns[:3]])}', 'info')
        weights = get_weights()
        signal_data = generate_signal(indicators, patterns, 0.0, weights)
        last_signals[pair] = signal_data
        add_log(f'[{pair}] RSI:{indicators["rsi"]:.0f} Signal:{signal_data["signal"]} ({signal_data["confidence"]:.0f}%)',
                'success' if signal_data['signal'] != 'HOLD' else 'info')
        price = indicators['price']
        result, closed_trade = paper_trade(pair, signal_data, price)
        if result['action'] == 'open':
            add_log(result['message'], 'success')
        elif result['action'] == 'close':
            add_log(result['message'], 'success' if result['pnl'] > 0 else 'error')
            if closed_trade:
                memory = analyze_trade(closed_trade, patterns, indicators, 0.0, pair)
                if memory:
                    add_log(f'[{pair}] AI: {memory["verdict"]} | {memory["key_lesson"]}', 'sol')
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
        log.exception(f"Scan failed for {pair}")


def run_all_scans():
    global scan_count, last_scan
    if not bot_running:
        return
    scan_count += 1
    add_log(f'=== Scan #{scan_count} ===', 'info')
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
            prices = get_all_prices()
            if prices:
                prices_cache.update(prices)
                socketio.emit('prices', prices)
        except:
            pass
        time.sleep(3)


# ── ROUTES ──
@app.route('/')
def index():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'index.html')
    with open(path, 'r', encoding='utf-8') as f:
        return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route('/api/status')
def api_status():
    return jsonify({
        'bot_running': bot_running, 'paper_mode': PAPER_TRADING,
        'scan_count': scan_count, 'last_scan': last_scan,
        'pairs': ACTIVE_PAIRS, 'uptime': 'active',
    })

@app.route('/api/prices')
def api_prices():
    prices = get_all_prices()
    stats = {pair: get_24h_stats(pair) for pair in ACTIVE_PAIRS}
    return jsonify({'prices': prices, 'stats': stats})

@app.route('/api/state')
def api_state():
    return jsonify({
        'states': get_all_states(), 'signals': last_signals,
        'indicators': last_indicators, 'patterns': last_patterns,
        'last_scan': last_scan,
    })

@app.route('/api/brain')
def api_brain():
    return jsonify(get_brain_summary())

@app.route('/api/logs')
def api_logs():
    return jsonify({'logs': system_log[-60:]})

@app.route('/api/bot/start', methods=['POST'])
def bot_start():
    global bot_running
    bot_running = True
    add_log('Bot started manually', 'success')
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
    return jsonify({'status': 'scan triggered'})


# ── WEBSOCKET ──
@socketio.on('connect')
def on_connect():
    emit('status', {'bot_running': bot_running, 'pairs': ACTIVE_PAIRS})
    emit('state', {
        'states': get_all_states(), 'signals': last_signals,
        'indicators': last_indicators, 'prices': prices_cache,
    })
    emit('logs', {'logs': system_log[-30:]})

@socketio.on('disconnect')
def on_disconnect():
    pass


# ── STARTUP ──
def initialize():
    global bot_running, _initialized
    if _initialized:
        return
    _initialized = True
    bot_running = True

    try:
        scheduler.add_job(run_all_scans, 'interval', minutes=SCAN_INTERVAL, id='scan_job')
        scheduler.start()
    except Exception as e:
        log.warning(f"Scheduler: {e}")

    threading.Thread(target=price_ticker, daemon=True).start()

    def delayed_scan():
        time.sleep(30)
        try:
            add_log('CRYPTOBOT online — BTC ETH SOL BNB | Paper Mode', 'sol')
            run_all_scans()
        except Exception as e:
            log.error(f"First scan error: {e}")

    threading.Thread(target=delayed_scan, daemon=True).start()
    log.info('CRYPTOBOT initialized')


initialize()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
