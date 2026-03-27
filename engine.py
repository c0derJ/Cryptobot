"""
CRYPTOBOT - Core Engine
Live prices via CoinCap API (free, no API key, works globally)
TA indicators, pattern detection, paper trading for BTC/ETH/SOL/BNB
"""

import os
import time
import logging
import requests
import pandas as pd
import numpy as np
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

# CoinCap asset IDs
COINCAP_IDS = {
    'BTC': 'bitcoin',
    'ETH': 'ethereum',
    'SOL': 'solana',
    'BNB': 'binance-coin',
}

# Colors for UI
PAIR_COLORS = {
    'BTC': '#F7931A',
    'ETH': '#627EEA',
    'SOL': '#9945FF',
    'BNB': '#F3BA2F',
}

# Cache for price data
price_cache = {}
last_price_fetch = 0
ohlcv_cache = {}
last_ohlcv_fetch = {}

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
# LIVE PRICE FEED — CoinCap API (works in Canada)
# ══════════════════════════════════════════════════
def get_all_prices():
    """Fetch all prices from CoinCap."""
    global last_price_fetch, price_cache
    
    # Rate limit: don't fetch more than once every 5 seconds
    now = time.time()
    if now - last_price_fetch < 5 and price_cache:
        return price_cache
    
    try:
        url = 'https://api.coincap.io/v2/assets'
        params = {
            'ids': ','.join([COINCAP_IDS[p] for p in ACTIVE_PAIRS if p in COINCAP_IDS]),
            'limit': 100
        }
        
        log.debug("Fetching prices from CoinCap")
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        prices = {}
        for asset in data.get('data', []):
            for pair, asset_id in COINCAP_IDS.items():
                if asset['id'] == asset_id:
                    prices[pair] = float(asset['priceUsd'])
        
        if prices:
            price_cache = prices
            last_price_fetch = now
            log.debug(f"Got prices: {prices}")
        
        return prices
    except Exception as e:
        log.error(f"Price fetch error: {e}")
        return price_cache if price_cache else {}


def get_price(pair):
    """Get single pair price."""
    try:
        asset_id = COINCAP_IDS.get(pair)
        if not asset_id:
            return None
            
        url = f'https://api.coincap.io/v2/assets/{asset_id}'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        return float(data['data']['priceUsd'])
    except Exception as e:
        log.error(f"Price fetch error for {pair}: {e}")
        return None


def get_ohlcv(pair, interval='1h', limit=100):
    """Fetch OHLCV data using CoinCap's history endpoint."""
    try:
        asset_id = COINCAP_IDS.get(pair)
        if not asset_id:
            log.error(f"No CoinCap ID for {pair}")
            return None
        
        # Rate limiting - wait 3 seconds between requests to same pair
        now = time.time()
        if pair in last_ohlcv_fetch and now - last_ohlcv_fetch[pair] < 3:
            if pair in ohlcv_cache:
                cached_df = ohlcv_cache[pair]
                if cached_df is not None and len(cached_df) >= limit:
                    log.debug(f"Using cached data for {pair}")
                    return cached_df
        
        # Map interval to CoinCap interval
        interval_map = {
            '1h': 'h1',
            '4h': 'h4', 
            '1d': 'd1'
        }
        coin_interval = interval_map.get(interval, 'h1')
        
        # CoinCap historical data endpoint
        url = f'https://api.coincap.io/v2/assets/{asset_id}/history'
        params = {
            'interval': coin_interval,
            'limit': limit
        }
        
        log.debug(f"Fetching OHLCV for {pair} from CoinCap")
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        
        if not data or 'data' not in data or not data['data']:
            log.warning(f"No OHLCV data for {pair}")
            return ohlcv_cache.get(pair, None)
        
        # CoinCap only gives price and time, we need to generate OHLC
        # Use the price as close, approximate open/high/low based on price movement
        history = data['data']
        
        df_data = []
        for i, entry in enumerate(history[-limit:]):
            price = float(entry['priceUsd'])
            timestamp = entry['time']
            
            # Approximate OHLC based on price and previous price
            if i > 0:
                prev_price = float(history[-limit + i - 1]['priceUsd'])
                if price > prev_price:
                    open_price = prev_price
                    high_price = price * 1.002
                    low_price = prev_price * 0.998
                else:
                    open_price = prev_price
                    high_price = prev_price * 1.002
                    low_price = price * 0.998
            else:
                open_price = price
                high_price = price * 1.001
                low_price = price * 0.999
            
            df_data.append({
                'time': timestamp,
                'open': open_price,
                'high': high_price,
                'low': low_price,
                'close': price,
                'volume': 0  # Volume not available in free tier
            })
        
        df = pd.DataFrame(df_data)
        df['time'] = pd.to_datetime(df['time'])
        df.set_index('time', inplace=True)
        
        # Cache the data
        ohlcv_cache[pair] = df
        last_ohlcv_fetch[pair] = now
        
        log.info(f"Fetched {len(df)} candles for {pair}")
        return df[['open', 'high', 'low', 'close', 'volume']]
        
    except requests.exceptions.RequestException as e:
        log.error(f"OHLCV request error for {pair}: {e}")
        return ohlcv_cache.get(pair, None)
    except Exception as e:
        log.error(f"OHLCV fetch error for {pair}: {e}")
        return ohlcv_cache.get(pair, None)


def get_24h_stats(pair):
    """Get 24h price change stats from CoinCap."""
    try:
        asset_id = COINCAP_IDS.get(pair)
        if not asset_id:
            return {'change_pct': 0, 'high': 0, 'low': 0, 'volume': 0}
        
        url = f'https://api.coincap.io/v2/assets/{asset_id}'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        asset_data = data['data']
        
        return {
            'change_pct': float(asset_data.get('changePercent24Hr', 0)),
            'high': float(asset_data.get('maxPrice', 0)),
            'low': float(asset_data.get('minPrice', 0)),
            'volume': float(asset_data.get('volumeUsd24Hr', 0)),
        }
    except Exception as e:
        log.error(f"24h stats error for {pair}: {e}")
        return {'change_pct': 0, 'high': 0, 'low': 0, 'volume': 0}


# ══════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE
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

    log.debug(f"Detected {len(detected)} patterns")
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
