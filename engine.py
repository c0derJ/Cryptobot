"""
CRYPTOBOT - Core Engine
Live prices via Kraken API (works in Canada, free, no API key needed)
TA indicators, pattern detection, paper trading for BTC/ETH/SOL/BNB
"""

import os
import time
import logging
import requests
import pandas as pd
import ta
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ── CONFIG ──
PAPER_TRADING   = os.getenv('PAPER_TRADING', 'true').lower() == 'true'
TRADE_AMOUNT    = float(os.getenv('TRADE_AMOUNT', 1000))
LEVERAGE        = float(os.getenv('LEVERAGE', 1))
STOP_LOSS_PCT   = float(os.getenv('STOP_LOSS_PCT', 3))
TAKE_PROFIT_PCT = float(os.getenv('TAKE_PROFIT_PCT', 6))
ACTIVE_PAIRS    = os.getenv('ACTIVE_PAIRS', 'BTC,ETH,SOL,BNB').split(',')

# Kraken symbol mapping
KRAKEN_SYMBOLS = {
    'BTC': 'XBT/USD',  # Kraken uses XBT for Bitcoin
    'ETH': 'ETH/USD',
    'SOL': 'SOL/USD',
    'BNB': 'BNB/USD',
}

# Kraken API endpoint
KRAKEN_API = 'https://api.kraken.com/0/public'

# Colors for UI
PAIR_COLORS = {
    'BTC': '#F7931A',
    'ETH': '#627EEA',
    'SOL': '#9945FF',
    'BNB': '#F3BA2F',
}

# Cache for OHLCV data
ohlcv_cache = {}
last_fetch_time = {}

# ── PAPER TRADING STATE (per pair) ──
def make_paper_state():
    return {
        'balance':     TRADE_AMOUNT,
        'position':    None,
        'entry_price': None,
        'entry_time':  None,
        'stop_loss':   None,
        'take_profit': None,
        'trades':      [],
        'wins':        0,
        'losses':      0,
        'total_pnl':   0.0,
    }

paper_states = {pair: make_paper_state() for pair in ACTIVE_PAIRS}

# ══════════════════════════════════════════════════
# LIVE PRICE FEED — Kraken API
# ══════════════════════════════════════════════════
def get_all_prices():
    """Fetch all prices from Kraken."""
    try:
        url = f'{KRAKEN_API}/Ticker'
        log.debug(f"Fetching prices from Kraken")
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        if data['error']:
            log.error(f"Kraken API error: {data['error']}")
            return {}
        
        prices = {}
        for pair, kraken_symbol in KRAKEN_SYMBOLS.items():
            # Convert format: XBT/USD -> XXBTZUSD for Kraken API
            symbol_key = kraken_symbol.replace('/', '').upper()
            if symbol_key in data['result']:
                ticker = data['result'][symbol_key]
                prices[pair] = float(ticker['c'][0])  # 'c' is last price
        
        log.debug(f"Got prices: {prices}")
        return prices
    except Exception as e:
        log.error(f"Price fetch error: {e}")
        return {}


def get_price(pair):
    """Get single pair price."""
    try:
        kraken_symbol = KRAKEN_SYMBOLS.get(pair)
        if not kraken_symbol:
            return None
            
        symbol_key = kraken_symbol.replace('/', '').upper()
        url = f'{KRAKEN_API}/Ticker?pair={symbol_key}'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        if data['error'] or symbol_key not in data['result']:
            return None
            
        return float(data['result'][symbol_key]['c'][0])
    except Exception as e:
        log.error(f"Price fetch error for {pair}: {e}")
        return None


def get_ohlcv(pair, interval='1h', limit=100):
    """Fetch OHLCV data from Kraken."""
    try:
        kraken_symbol = KRAKEN_SYMBOLS.get(pair)
        if not kraken_symbol:
            log.error(f"No Kraken symbol for {pair}")
            return None
        
        # Rate limiting - wait 2 seconds between requests
        now = time.time()
        if pair in last_fetch_time and now - last_fetch_time[pair] < 2:
            if pair in ohlcv_cache:
                cached_df = ohlcv_cache[pair]
                if cached_df is not None and len(cached_df) >= limit:
                    log.debug(f"Using cached data for {pair}")
                    return cached_df
        
        # Map interval to Kraken interval (minutes)
        interval_map = {
            '1h': 60,
            '4h': 240,
            '1d': 1440
        }
        kraken_interval = interval_map.get(interval, 60)
        
        symbol_key = kraken_symbol.replace('/', '').upper()
        url = f'{KRAKEN_API}/OHLC?pair={symbol_key}&interval={kraken_interval}'
        
        log.debug(f"Fetching OHLCV for {pair} from Kraken")
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        
        if data['error']:
            log.error(f"Kraken OHLCV error for {pair}: {data['error']}")
            return ohlcv_cache.get(pair, None)
        
        if symbol_key not in data['result']:
            log.warning(f"No OHLCV data for {pair}")
            return ohlcv_cache.get(pair, None)
        
        ohlcv_data = data['result'][symbol_key]
        
        if not ohlcv_data or len(ohlcv_data) == 0:
            log.warning(f"Empty OHLCV data for {pair}")
            return ohlcv_cache.get(pair, None)
        
        # Convert to DataFrame
        df_data = []
        for candle in ohlcv_data[-limit:]:  # Get last 'limit' candles
            df_data.append({
                'time': candle[0],  # timestamp
                'open': float(candle[1]),
                'high': float(candle[2]),
                'low': float(candle[3]),
                'close': float(candle[4]),
                'volume': float(candle[6])  # volume
            })
        
        df = pd.DataFrame(df_data)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        
        # Cache the data
        ohlcv_cache[pair] = df
        last_fetch_time[pair] = now
        
        log.info(f"Fetched {len(df)} candles for {pair}")
        return df[['open', 'high', 'low', 'close', 'volume']]
        
    except requests.exceptions.RequestException as e:
        log.error(f"OHLCV request error for {pair}: {e}")
        return ohlcv_cache.get(pair, None)
    except Exception as e:
        log.error(f"OHLCV fetch error for {pair}: {e}")
        return ohlcv_cache.get(pair, None)


def get_24h_stats(pair):
    """Get 24h price change stats from Kraken."""
    try:
        kraken_symbol = KRAKEN_SYMBOLS.get(pair)
        if not kraken_symbol:
            return {'change_pct': 0, 'high': 0, 'low': 0, 'volume': 0}
        
        symbol_key = kraken_symbol.replace('/', '').upper()
        url = f'{KRAKEN_API}/Ticker?pair={symbol_key}'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        if data['error'] or symbol_key not in data['result']:
            return {'change_pct': 0, 'high': 0, 'low': 0, 'volume': 0}
        
        ticker = data['result'][symbol_key]
        
        # Calculate 24h change percentage
        open_price = float(ticker['o'][0])
        last_price = float(ticker['c'][0])
        change_pct = ((last_price - open_price) / open_price) * 100
        
        return {
            'change_pct': change_pct,
            'high': float(ticker['h'][0]),
            'low': float(ticker['l'][0]),
            'volume': float(ticker['v'][1]),  # 24h volume
        }
    except Exception as e:
        log.error(f"24h stats error for {pair}: {e}")
        return {'change_pct': 0, 'high': 0, 'low': 0, 'volume': 0}


# ══════════════════════════════════════════════════
# REST OF THE CODE (same as before - indicators, patterns, signals, paper trading)
# ══════════════════════════════════════════════════

def calculate_indicators(df):
    """Full indicator suite: RSI, MACD, BB, MAs, ATR, Stochastic, OBV."""
    try:
        if df is None or len(df) < 20:
            return df
            
        c = df['close']
        h = df['high']
        l = df['low']
        v = df['volume']

        # RSI
        df['rsi'] = ta.momentum.RSIIndicator(c, window=14).rsi()

        # MACD
        macd = ta.trend.MACD(c, window_fast=12, window_slow=26, window_sign=9)
        df['macd'] = macd.macd()
        df['macd_signal'] = macd.macd_signal()
        df['macd_hist'] = macd.macd_diff()

        # Bollinger Bands
        bb = ta.volatility.BollingerBands(c, window=20, window_dev=2)
        df['bb_upper'] = bb.bollinger_hband()
        df['bb_mid'] = bb.bollinger_mavg()
        df['bb_lower'] = bb.bollinger_lband()
        df['bb_width'] = bb.bollinger_wband()

        # Moving Averages
        df['ma20'] = ta.trend.SMAIndicator(c, window=20).sma_indicator()
        df['ma50'] = ta.trend.SMAIndicator(c, window=50).sma_indicator()
        df['ema9'] = ta.trend.EMAIndicator(c, window=9).ema_indicator()
        df['ema21'] = ta.trend.EMAIndicator(c, window=21).ema_indicator()

        # ATR
        df['atr'] = ta.volatility.AverageTrueRange(h, l, c, window=14).average_true_range()

        # Stochastic
        stoch = ta.momentum.StochasticOscillator(h, l, c, window=14, smooth_window=3)
        df['stoch_k'] = stoch.stoch()
        df['stoch_d'] = stoch.stoch_signal()

        # OBV
        df['obv'] = ta.volume.OnBalanceVolumeIndicator(c, v).on_balance_volume()

        # ADX
        adx = ta.trend.ADXIndicator(h, l, c, window=14)
        df['adx'] = adx.adx()

        return df
    except Exception as e:
        log.error(f"Indicator error: {e}")
        return df


def get_indicator_snapshot(df):
    """Return clean indicator dict from latest candle."""
    if df is None or len(df) < 2:
        return {}
        
    last = df.iloc[-1]
    prev = df.iloc[-2]

    def safe(val, default=0):
        try:
            v = float(val)
            return default if pd.isna(v) else v
        except:
            return default

    close = safe(last['close'])
    bb_upper = safe(last['bb_upper'], close)
    bb_lower = safe(last['bb_lower'], close)
    bb_range = bb_upper - bb_lower
    bb_pos = ((close - bb_lower) / bb_range * 100) if bb_range > 0 else 50

    macd = safe(last['macd'])
    macd_sig = safe(last['macd_signal'])
    macd_bull = macd > macd_sig
    macd_cross_bull = safe(prev['macd']) < safe(prev['macd_signal']) and macd > macd_sig
    macd_cross_bear = safe(prev['macd']) > safe(prev['macd_signal']) and macd < macd_sig

    ma20 = safe(last['ma20'], close)
    ma50 = safe(last['ma50'], close)

    return {
        'price': round(close, 4),
        'rsi': round(safe(last['rsi'], 50), 2),
        'macd': round(macd, 6),
        'macd_signal': round(macd_sig, 6),
        'macd_bull': macd_bull,
        'macd_cross_bull': macd_cross_bull,
        'macd_cross_bear': macd_cross_bear,
        'macd_hist': round(safe(last['macd_hist']), 6),
        'bb_upper': round(bb_upper, 4),
        'bb_lower': round(bb_lower, 4),
        'bb_mid': round(safe(last['bb_mid'], close), 4),
        'bb_pos': round(bb_pos, 1),
        'bb_width': round(safe(last['bb_width']), 4),
        'ma20': round(ma20, 4),
        'ma50': round(ma50, 4),
        'ema9': round(safe(last['ema9'], close), 4),
        'ema21': round(safe(last['ema21'], close), 4),
        'above_ma20': close > ma20,
        'above_ma50': close > ma50,
        'atr': round(safe(last['atr']), 4),
        'stoch_k': round(safe(last['stoch_k'], 50), 2),
        'stoch_d': round(safe(last['stoch_d'], 50), 2),
        'adx': round(safe(last['adx'], 20), 2),
        'obv_rising': safe(last['obv']) > safe(prev['obv']),
    }


def detect_patterns(df):
    """Detect candlestick patterns."""
    if df is None or len(df) < 5:
        return []
        
    detected = []
    c = df.iloc[-1]

    def body(candle): return abs(float(candle['close']) - float(candle['open']))
    def range_(candle): return float(candle['high']) - float(candle['low'])
    def is_bull(candle): return float(candle['close']) > float(candle['open'])

    bc = body(c)
    rc = range_(c)
    bull_c = is_bull(c)
    uw_c = float(c['high']) - max(float(c['close']), float(c['open']))
    lw_c = min(float(c['close']), float(c['open'])) - float(c['low'])

    # Single candle patterns
    if rc > 0:
        # Hammer
        if lw_c >= 2*bc and uw_c <= bc*0.3 and not bull_c:
            detected.append({'id': 'hammer', 'name': 'Hammer', 'signal': 'BULLISH', 'reliability': 72, 'category': 'Candlestick'})
        # Doji
        if bc <= rc*0.1:
            detected.append({'id': 'doji', 'name': 'Doji', 'signal': 'NEUTRAL', 'reliability': 55, 'category': 'Candlestick'})

    log.debug(f"Detected {len(detected)} patterns for {df.index[-1]}")
    return detected


def generate_signal(indicators, patterns, sentiment_score, pattern_weights):
    """Combine all signals into LONG/SHORT/HOLD with confidence score."""
    if not indicators:
        return {'signal': 'HOLD', 'bull_score': 0, 'bear_score': 0, 'confidence': 0, 'reasons': ['No data']}
        
    bull = 0
    bear = 0
    reasons = []

    # RSI
    rsi = indicators['rsi']
    if rsi < 30:
        bull += 3
        reasons.append(f'RSI oversold ({rsi:.1f})')
    elif rsi < 40:
        bull += 1.5
        reasons.append(f'RSI low ({rsi:.1f})')
    elif rsi > 70:
        bear += 3
        reasons.append(f'RSI overbought ({rsi:.1f})')
    elif rsi > 60:
        bear += 1.5
        reasons.append(f'RSI high ({rsi:.1f})')

    # MACD
    if indicators['macd_cross_bull']:
        bull += 3.5
        reasons.append('MACD bullish crossover ⚡')
    elif indicators['macd_cross_bear']:
        bear += 3.5
        reasons.append('MACD bearish crossover ⚡')
    elif indicators['macd_bull']:
        bull += 1
        reasons.append('MACD above signal')
    else:
        bear += 1
        reasons.append('MACD below signal')

    # Bollinger Bands
    bp = indicators['bb_pos']
    if bp < 15:
        bull += 2.5
        reasons.append(f'Price at BB lower ({bp:.0f}%)')
    elif bp < 30:
        bull += 1
        reasons.append(f'Price near BB lower')
    elif bp > 85:
        bear += 2.5
        reasons.append(f'Price at BB upper ({bp:.0f}%)')
    elif bp > 70:
        bear += 1
        reasons.append(f'Price near BB upper')

    total = bull + bear
    confidence = abs(bull - bear) / total * 100 if total > 0 else 0

    if bull > bear and confidence >= 25:
        signal = 'LONG'
    elif bear > bull and confidence >= 25:
        signal = 'SHORT'
    else:
        signal = 'HOLD'

    return {
        'signal': signal,
        'bull_score': round(bull, 2),
        'bear_score': round(bear, 2),
        'confidence': round(confidence, 1),
        'reasons': reasons[:8],
    }


def paper_trade(pair, signal_data, price):
    """Execute paper trade for a specific pair."""
    ps = paper_states[pair]
    result = {'action': 'none', 'pair': pair, 'price': price}

    # Check SL/TP on open position
    if ps['position']:
        entry = ps['entry_price']
        pos = ps['position']
        sl = ps['stop_loss']
        tp = ps['take_profit']
        hit_sl = (pos == 'long' and price <= sl) or (pos == 'short' and price >= sl)
        hit_tp = (pos == 'long' and price >= tp) or (pos == 'short' and price <= tp)

        if hit_sl or hit_tp:
            pnl = ((price - entry) / entry if pos == 'long' else (entry - price) / entry) * TRADE_AMOUNT * LEVERAGE
            ps['balance'] += pnl
            ps['total_pnl'] += pnl
            if pnl > 0:
                ps['wins'] += 1
            else:
                ps['losses'] += 1
            trade = {
                'pair': pair,
                'type': pos.upper(),
                'entry': entry,
                'exit': price,
                'pnl': round(pnl, 2),
                'outcome': 'WIN' if pnl > 0 else 'LOSS',
                'reason': 'STOP LOSS' if hit_sl else 'TAKE PROFIT',
                'time': datetime.now().isoformat(),
            }
            ps['trades'].append(trade)
            ps['position'] = ps['entry_price'] = ps['stop_loss'] = ps['take_profit'] = None
            result = {'action': 'close', 'pair': pair, 'price': price, 'pnl': round(pnl, 2),
                      'outcome': trade['outcome'], 'message': f"[{pair}] {trade['outcome']} ${pnl:+.2f}"}
            return result, trade

    # Open new position
    sig = signal_data['signal']
    if sig in ('LONG', 'SHORT') and ps['position'] is None:
        if sig == 'LONG':
            sl = round(price * (1 - STOP_LOSS_PCT / 100), 6)
            tp = round(price * (1 + TAKE_PROFIT_PCT / 100), 6)
        else:
            sl = round(price * (1 + STOP_LOSS_PCT / 100), 6)
            tp = round(price * (1 - TAKE_PROFIT_PCT / 100), 6)
        ps['position'] = sig.lower()
        ps['entry_price'] = price
        ps['entry_time'] = datetime.now().isoformat()
        ps['stop_loss'] = sl
        ps['take_profit'] = tp
        result = {'action': 'open', 'pair': pair, 'type': sig, 'entry': price, 'sl': sl, 'tp': tp,
                  'message': f"[PAPER][{pair}] {sig} @ ${price:.4f} SL:${sl:.4f} TP:${tp:.4f}"}

    return result, None


def get_pair_state(pair):
    """Clean state dict for dashboard."""
    ps = paper_states[pair]
    total = ps['wins'] + ps['losses']
    win_rate = (ps['wins'] / total * 100) if total > 0 else 0
    price = get_price(pair) or 0
    unrealized = 0
    if ps['position'] and ps['entry_price'] and price:
        if ps['position'] == 'long':
            unrealized = (price - ps['entry_price']) / ps['entry_price'] * TRADE_AMOUNT * LEVERAGE
        else:
            unrealized = (ps['entry_price'] - price) / ps['entry_price'] * TRADE_AMOUNT * LEVERAGE
    return {
        'pair': pair,
        'balance': round(ps['balance'], 2),
        'position': ps['position'].upper() if ps['position'] else 'NONE',
        'entry_price': ps['entry_price'],
        'stop_loss': ps['stop_loss'],
        'take_profit': ps['take_profit'],
        'unrealized_pnl': round(unrealized, 2),
        'total_pnl': round(ps['total_pnl'], 2),
        'total_trades': total,
        'wins': ps['wins'],
        'losses': ps['losses'],
        'win_rate': round(win_rate, 1),
        'recent_trades': ps['trades'][-5:],
        'color': PAIR_COLORS.get(pair, '#ffffff'),
    }


def get_all_states():
    return {pair: get_pair_state(pair) for pair in ACTIVE_PAIRS}
