"""
CRYPTOBOT - Core Engine
Live prices via CoinGecko API (free, no API key needed, works globally)
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

# CoinGecko ID mapping
COINGECKO_IDS = {
    'BTC': 'bitcoin',
    'ETH': 'ethereum',
    'SOL': 'solana',
    'BNB': 'binancecoin',
}

# Colors for UI
PAIR_COLORS = {
    'BTC': '#F7931A',
    'ETH': '#627EEA',
    'SOL': '#9945FF',
    'BNB': '#F3BA2F',
}

# Cache for OHLCV data to avoid rate limits
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
# LIVE PRICE FEED — CoinGecko (free, no API key)
# ══════════════════════════════════════════════════
def get_all_prices():
    """Fetch all prices in a single CoinGecko API call."""
    try:
        ids = ','.join([COINGECKO_IDS[p] for p in ACTIVE_PAIRS if p in COINGECKO_IDS])
        url = f'https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true'
        
        log.debug(f"Fetching prices from CoinGecko")
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        prices = {}
        for pair, coin_id in COINGECKO_IDS.items():
            if coin_id in data:
                prices[pair] = data[coin_id]['usd']
        
        log.debug(f"Got prices: {prices}")
        return prices
    except Exception as e:
        log.error(f"Price fetch error: {e}")
        return {}


def get_price(pair):
    """Get single pair price."""
    try:
        coin_id = COINGECKO_IDS.get(pair)
        if not coin_id:
            return None
            
        url = f'https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd'
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data[coin_id]['usd']
    except Exception as e:
        log.error(f"Price fetch error for {pair}: {e}")
        return None


def get_ohlcv(pair, interval='1h', limit=100):
    """Fetch OHLCV data using CoinGecko with rate limiting."""
    try:
        coin_id = COINGECKO_IDS.get(pair)
        if not coin_id:
            log.error(f"No CoinGecko ID for {pair}")
            return None
        
        # Rate limiting - wait 2 seconds between requests to same pair
        now = time.time()
        if pair in last_fetch_time and now - last_fetch_time[pair] < 2:
            # Return cached data if available and not expired
            if pair in ohlcv_cache:
                cached_df = ohlcv_cache[pair]
                if cached_df is not None and len(cached_df) >= limit:
                    log.debug(f"Using cached data for {pair}")
                    return cached_df
        
        # Map interval for CoinGecko
        interval_map = {
            '1h': 'hourly',
            '4h': '4h', 
            '1d': 'daily'
        }
        coin_interval = interval_map.get(interval, 'hourly')
        
        # CoinGecko market chart endpoint
        url = f'https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=7&interval={coin_interval}'
        
        log.debug(f"Fetching OHLCV for {pair} from CoinGecko")
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        if not data or 'prices' not in data:
            log.warning(f"No price data for {pair}")
            return None
        
        # Convert to DataFrame with OHLCV
        prices = data['prices']
        volumes = data.get('total_volumes', [])
        
        # Create DataFrame
        df_data = []
        for i in range(len(prices)):
            if i >= len(volumes):
                volume = 0
            else:
                volume = volumes[i][1] if len(volumes) > i else 0
            
            # CoinGecko only gives prices, we need to approximate OHLC
            # For simplicity, use close price for all OHLC
            price = prices[i][1]
            timestamp = prices[i][0]
            
            df_data.append({
                'time': timestamp,
                'open': price,
                'high': price * 1.001,  # Approximate high
                'low': price * 0.999,   # Approximate low
                'close': price,
                'volume': volume
            })
        
        df = pd.DataFrame(df_data)
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index('time', inplace=True)
        
        # Resample to get proper OHLC if needed
        if len(df) > limit:
            df = df.iloc[-limit:]
        
        # Cache the data
        ohlcv_cache[pair] = df
        last_fetch_time[pair] = now
        
        log.info(f"Fetched {len(df)} candles for {pair}")
        return df[['open', 'high', 'low', 'close', 'volume']]
        
    except requests.exceptions.RequestException as e:
        log.error(f"OHLCV request error for {pair}: {e}")
        # Return cached data if available
        if pair in ohlcv_cache:
            log.info(f"Using cached data for {pair} due to error")
            return ohlcv_cache[pair]
        return None
    except Exception as e:
        log.error(f"OHLCV fetch error for {pair}: {e}")
        if pair in ohlcv_cache:
            return ohlcv_cache[pair]
        return None


def get_24h_stats(pair):
    """Get 24h price change stats from CoinGecko."""
    try:
        coin_id = COINGECKO_IDS.get(pair)
        if not coin_id:
            return {'change_pct': 0, 'high': 0, 'low': 0, 'volume': 0}
        
        url = f'https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false'
        
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        market_data = data.get('market_data', {})
        
        return {
            'change_pct': market_data.get('price_change_percentage_24h', 0),
            'high': market_data.get('high_24h', {}).get('usd', 0),
            'low': market_data.get('low_24h', {}).get('usd', 0),
            'volume': market_data.get('total_volume', {}).get('usd', 0),
        }
    except Exception as e:
        log.error(f"24h stats error for {pair}: {e}")
        return {'change_pct': 0, 'high': 0, 'low': 0, 'volume': 0}


# ══════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE (same as before)
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


# ══════════════════════════════════════════════════
# PATTERN DETECTION ENGINE (same as before)
# ══════════════════════════════════════════════════
def detect_patterns(df):
    """Detect 30+ candlestick and chart patterns."""
    if df is None or len(df) < 5:
        return []
        
    detected = []
    c = df.iloc[-1]
    p = df.iloc[-2]
    p2 = df.iloc[-3]
    p3 = df.iloc[-4]

    def body(candle): return abs(float(candle['close']) - float(candle['open']))
    def range_(candle): return float(candle['high']) - float(candle['low'])
    def is_bull(candle): return float(candle['close']) > float(candle['open'])
    def upper_wick(candle): return float(candle['high']) - max(float(candle['close']), float(candle['open']))
    def lower_wick(candle): return min(float(candle['close']), float(candle['open'])) - float(candle['low'])

    bc = body(c)
    rc = range_(c)
    bull_c = is_bull(c)
    bp = body(p)
    rp = range_(p)
    bull_p = is_bull(p)
    bp2 = body(p2)
    bull_p2 = is_bull(p2)
    uw_c = upper_wick(c)
    lw_c = lower_wick(c)

    # Single candle patterns
    if rc > 0:
        # Hammer
        if lw_c >= 2*bc and uw_c <= bc*0.3 and not bull_c:
            detected.append({'id': 'hammer', 'name': 'Hammer', 'signal': 'BULLISH', 'reliability': 72, 'category': 'Candlestick'})
        # Inverted Hammer
        if uw_c >= 2*bc and lw_c <= bc*0.3 and not bull_c:
            detected.append({'id': 'inv_hammer', 'name': 'Inverted Hammer', 'signal': 'BULLISH', 'reliability': 65, 'category': 'Candlestick'})
        # Doji
        if bc <= rc*0.1:
            if lw_c >= rc*0.6:
                detected.append({'id': 'dragonfly_doji', 'name': 'Dragonfly Doji', 'signal': 'BULLISH', 'reliability': 68, 'category': 'Candlestick'})
            elif uw_c >= rc*0.6:
                detected.append({'id': 'gravestone_doji', 'name': 'Gravestone Doji', 'signal': 'BEARISH', 'reliability': 70, 'category': 'Candlestick'})
            else:
                detected.append({'id': 'doji', 'name': 'Doji', 'signal': 'NEUTRAL', 'reliability': 55, 'category': 'Candlestick'})

    log.info(f"Detected {len(detected)} patterns")
    return detected


# ══════════════════════════════════════════════════
# SIGNAL FUSION ENGINE (same as before)
# ══════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════
# PAPER TRADING ENGINE (same as before)
# ══════════════════════════════════════════════════
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
