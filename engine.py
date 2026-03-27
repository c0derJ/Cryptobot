"""
CRYPTOBOT - Core Engine
Live prices via Binance public API (fastest free source)
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

# CoinGecko IDs
COIN_IDS = {
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
# LIVE PRICE FEED — CoinGecko (free, no geo-blocks)
# ══════════════════════════════════════════════════
def get_all_prices():
    """Fetch all 4 prices from CoinGecko."""
    try:
        ids = ','.join(COIN_IDS.values())
        url = f'https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd'
        r = requests.get(url, timeout=10)
        data = r.json()
        return {
            'BTC': float(data.get('bitcoin',{}).get('usd', 0)),
            'ETH': float(data.get('ethereum',{}).get('usd', 0)),
            'SOL': float(data.get('solana',{}).get('usd', 0)),
            'BNB': float(data.get('binancecoin',{}).get('usd', 0)),
        }
    except Exception as e:
        log.error(f"Price fetch error: {e}")
        return {}


def get_price(pair):
    """Get single pair price."""
    prices = get_all_prices()
    return prices.get(pair, 0)


def get_ohlcv(pair, interval='1h', limit=100):
    """Fetch OHLCV from CoinGecko market chart."""
    try:
        coin_id = COIN_IDS.get(pair, 'bitcoin')
        url = f'https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=5&interval=hourly'
        r = requests.get(url, timeout=15)
        data = r.json()
        prices = data.get('prices', [])
        volumes = data.get('total_volumes', [])
        if len(prices) < 60:
            log.error(f"Not enough price data for {pair}: {len(prices)} points")
            return None
        rows = []
        for i in range(1, len(prices)):
            c   = float(prices[i][1])
            p   = float(prices[i-1][1])
            vol = float(volumes[i][1]) if i < len(volumes) else 0
            rows.append({
                'time':   pd.Timestamp(prices[i][0], unit='ms'),
                'open':   p,
                'high':   max(c, p) * 1.002,
                'low':    min(c, p) * 0.998,
                'close':  c,
                'volume': vol,
            })
        df = pd.DataFrame(rows).set_index('time')
        return df.tail(limit)
    except Exception as e:
        log.error(f"OHLCV fetch error for {pair}: {e}")
        return None


def get_24h_stats(pair):
    """Get 24h stats from CoinGecko."""
    try:
        coin_id = COIN_IDS.get(pair, 'bitcoin')
        url = f'https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={coin_id}'
        r = requests.get(url, timeout=10)
        data = r.json()
        if data and len(data) > 0:
            d = data[0]
            return {
                'change_pct': d.get('price_change_percentage_24h', 0) or 0,
                'high':       d.get('high_24h', 0) or 0,
                'low':        d.get('low_24h', 0) or 0,
                'volume':     d.get('total_volume', 0) or 0,
            }
    except Exception as e:
        log.error(f"Stats error {pair}: {e}")
    return {'change_pct': 0, 'high': 0, 'low': 0, 'volume': 0}


# ══════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE
# ══════════════════════════════════════════════════
def calculate_indicators(df):
    """Full indicator suite: RSI, MACD, BB, MAs, ATR, Stochastic, OBV."""
    try:
        c = df['close']
        h = df['high']
        l = df['low']
        v = df['volume']

        # RSI
        df['rsi'] = ta.momentum.RSIIndicator(c, window=14).rsi()

        # MACD
        macd = ta.trend.MACD(c, window_fast=12, window_slow=26, window_sign=9)
        df['macd']        = macd.macd()
        df['macd_signal'] = macd.macd_signal()
        df['macd_hist']   = macd.macd_diff()

        # Bollinger Bands
        bb = ta.volatility.BollingerBands(c, window=20, window_dev=2)
        df['bb_upper'] = bb.bollinger_hband()
        df['bb_mid']   = bb.bollinger_mavg()
        df['bb_lower'] = bb.bollinger_lband()
        df['bb_width'] = bb.bollinger_wband()

        # Moving Averages
        df['ma20']  = ta.trend.SMAIndicator(c, window=20).sma_indicator()
        df['ma50']  = ta.trend.SMAIndicator(c, window=50).sma_indicator()
        df['ema9']  = ta.trend.EMAIndicator(c, window=9).ema_indicator()
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
    last = df.iloc[-1]
    prev = df.iloc[-2]

    def safe(val, default=0):
        try:
            v = float(val)
            return default if pd.isna(v) else v
        except:
            return default

    close    = safe(last['close'])
    bb_upper = safe(last['bb_upper'], close)
    bb_lower = safe(last['bb_lower'], close)
    bb_range = bb_upper - bb_lower
    bb_pos   = ((close - bb_lower) / bb_range * 100) if bb_range > 0 else 50

    macd     = safe(last['macd'])
    macd_sig = safe(last['macd_signal'])
    macd_bull = macd > macd_sig
    macd_cross_bull = float(prev['macd']) < float(prev['macd_signal']) and macd > macd_sig
    macd_cross_bear = float(prev['macd']) > float(prev['macd_signal']) and macd < macd_sig

    ma20 = safe(last['ma20'], close)
    ma50 = safe(last['ma50'], close)

    return {
        'price':           round(close, 4),
        'rsi':             round(safe(last['rsi'], 50), 2),
        'macd':            round(macd, 6),
        'macd_signal':     round(macd_sig, 6),
        'macd_bull':       macd_bull,
        'macd_cross_bull': macd_cross_bull,
        'macd_cross_bear': macd_cross_bear,
        'macd_hist':       round(safe(last['macd_hist']), 6),
        'bb_upper':        round(bb_upper, 4),
        'bb_lower':        round(bb_lower, 4),
        'bb_mid':          round(safe(last['bb_mid'], close), 4),
        'bb_pos':          round(bb_pos, 1),
        'bb_width':        round(safe(last['bb_width']), 4),
        'ma20':            round(ma20, 4),
        'ma50':            round(ma50, 4),
        'ema9':            round(safe(last['ema9'], close), 4),
        'ema21':           round(safe(last['ema21'], close), 4),
        'above_ma20':      close > ma20,
        'above_ma50':      close > ma50,
        'atr':             round(safe(last['atr']), 4),
        'stoch_k':         round(safe(last['stoch_k'], 50), 2),
        'stoch_d':         round(safe(last['stoch_d'], 50), 2),
        'adx':             round(safe(last['adx'], 20), 2),
        'obv_rising':      safe(last['obv']) > safe(prev['obv']),
    }


# ══════════════════════════════════════════════════
# COMPLETE PATTERN DETECTION ENGINE
# ══════════════════════════════════════════════════
def detect_patterns(df):
    """Detect 30+ candlestick and chart patterns."""
    detected = []
    if len(df) < 5:
        return detected

    c  = df.iloc[-1]
    p  = df.iloc[-2]
    p2 = df.iloc[-3]
    p3 = df.iloc[-4]

    def body(candle):    return abs(float(candle['close']) - float(candle['open']))
    def range_(candle):  return float(candle['high']) - float(candle['low'])
    def is_bull(candle): return float(candle['close']) > float(candle['open'])
    def upper_wick(candle): return float(candle['high']) - max(float(candle['close']), float(candle['open']))
    def lower_wick(candle): return min(float(candle['close']), float(candle['open'])) - float(candle['low'])

    bc = body(c); rc = range_(c); bull_c = is_bull(c)
    bp = body(p); rp = range_(p); bull_p = is_bull(p)
    bp2 = body(p2); bull_p2 = is_bull(p2)
    uw_c = upper_wick(c); lw_c = lower_wick(c)

    # ── SINGLE CANDLE ──
    if rc > 0:
        # Hammer
        if lw_c >= 2*bc and uw_c <= bc*0.3 and not bull_c:
            detected.append({'id':'hammer','name':'Hammer','signal':'BULLISH','reliability':72,'category':'Candlestick'})
        # Inverted Hammer
        if uw_c >= 2*bc and lw_c <= bc*0.3 and not bull_c:
            detected.append({'id':'inv_hammer','name':'Inverted Hammer','signal':'BULLISH','reliability':65,'category':'Candlestick'})
        # Shooting Star
        if uw_c >= 2*bc and lw_c <= bc*0.3 and bull_p:
            detected.append({'id':'shooting_star','name':'Shooting Star','signal':'BEARISH','reliability':74,'category':'Candlestick'})
        # Hanging Man
        if lw_c >= 2*bc and uw_c <= bc*0.3 and bull_p:
            detected.append({'id':'hanging_man','name':'Hanging Man','signal':'BEARISH','reliability':65,'category':'Candlestick'})
        # Doji
        if bc <= rc*0.1:
            if lw_c >= rc*0.6:
                detected.append({'id':'dragonfly_doji','name':'Dragonfly Doji','signal':'BULLISH','reliability':68,'category':'Candlestick'})
            elif uw_c >= rc*0.6:
                detected.append({'id':'gravestone_doji','name':'Gravestone Doji','signal':'BEARISH','reliability':70,'category':'Candlestick'})
            else:
                detected.append({'id':'doji','name':'Doji','signal':'NEUTRAL','reliability':55,'category':'Candlestick'})
        # Marubozu
        if uw_c <= bc*0.05 and lw_c <= bc*0.05 and bc > 0:
            detected.append({'id':'bull_marubozu' if bull_c else 'bear_marubozu',
                           'name':'Bullish Marubozu' if bull_c else 'Bearish Marubozu',
                           'signal':'BULLISH' if bull_c else 'BEARISH','reliability':78 if bull_c else 77,'category':'Candlestick'})

    # ── TWO CANDLE ──
    if bc > 0 and bp > 0:
        # Bullish Engulfing
        if not bull_p and bull_c and float(c['open']) < float(p['close']) and float(c['close']) > float(p['open']) and bc > bp:
            detected.append({'id':'bull_engulfing','name':'Bullish Engulfing','signal':'BULLISH','reliability':82,'category':'Candlestick'})
        # Bearish Engulfing
        if bull_p and not bull_c and float(c['open']) > float(p['close']) and float(c['close']) < float(p['open']) and bc > bp:
            detected.append({'id':'bear_engulfing','name':'Bearish Engulfing','signal':'BEARISH','reliability':81,'category':'Candlestick'})
        # Tweezer Top
        if bull_p and not bull_c and abs(float(p['high']) - float(c['high'])) <= float(c['high'])*0.001:
            detected.append({'id':'tweezer_top','name':'Tweezer Top','signal':'BEARISH','reliability':70,'category':'Candlestick'})
        # Tweezer Bottom
        if not bull_p and bull_c and abs(float(p['low']) - float(c['low'])) <= float(c['low'])*0.001:
            detected.append({'id':'tweezer_bottom','name':'Tweezer Bottom','signal':'BULLISH','reliability':70,'category':'Candlestick'})
        # Piercing Line
        if not bull_p and bull_c and float(c['open']) < float(p['low']) and float(c['close']) > (float(p['open'])+float(p['close']))/2:
            detected.append({'id':'piercing','name':'Piercing Line','signal':'BULLISH','reliability':74,'category':'Candlestick'})
        # Dark Cloud Cover
        if bull_p and not bull_c and float(c['open']) > float(p['high']) and float(c['close']) < (float(p['open'])+float(p['close']))/2:
            detected.append({'id':'dark_cloud','name':'Dark Cloud Cover','signal':'BEARISH','reliability':73,'category':'Candlestick'})

    # ── THREE CANDLE ──
    if bp2 > 0:
        # Morning Star
        if not bull_p2 and bp <= bp2*0.3 and bull_c and float(c['close']) > (float(p2['open'])+float(p2['close']))/2:
            detected.append({'id':'morning_star','name':'Morning Star','signal':'BULLISH','reliability':84,'category':'Candlestick'})
        # Evening Star
        if bull_p2 and bp <= bp2*0.3 and not bull_c and float(c['close']) < (float(p2['open'])+float(p2['close']))/2:
            detected.append({'id':'evening_star','name':'Evening Star','signal':'BEARISH','reliability':83,'category':'Candlestick'})
        # Three White Soldiers
        if bull_c and bull_p and bull_p2 and float(c['close']) > float(p['close']) > float(p2['close']):
            detected.append({'id':'three_soldiers','name':'Three White Soldiers','signal':'BULLISH','reliability':83,'category':'Candlestick'})
        # Three Black Crows
        if not bull_c and not bull_p and not bull_p2 and float(c['close']) < float(p['close']) < float(p2['close']):
            detected.append({'id':'three_crows','name':'Three Black Crows','signal':'BEARISH','reliability':82,'category':'Candlestick'})

    # ── CHART PATTERNS (20-candle window) ──
    w = df.iloc[-20:]

    # Bull Flag
    pole = w['close'].max() - w['close'].min()
    last5_range = w.iloc[-5:]['close'].max() - w.iloc[-5:]['close'].min()
    if pole > 0 and last5_range < pole*0.3 and float(c['close']) > float(w.iloc[-5]['close']):
        detected.append({'id':'bull_flag','name':'Bull Flag','signal':'BULLISH','reliability':85,'category':'Chart'})

    # Bear Flag
    if pole > 0 and last5_range < pole*0.3 and float(c['close']) < float(w.iloc[-5]['close']):
        detected.append({'id':'bear_flag','name':'Bear Flag','signal':'BEARISH','reliability':84,'category':'Chart'})

    # Double Bottom
    lows = w['low'].nsmallest(2).values
    if len(lows)==2 and abs(lows[0]-lows[1])/max(lows[0],0.001) < 0.015:
        detected.append({'id':'double_bottom','name':'Double Bottom','signal':'BULLISH','reliability':83,'category':'Chart'})

    # Double Top
    highs = w['high'].nlargest(2).values
    if len(highs)==2 and abs(highs[0]-highs[1])/max(highs[0],0.001) < 0.015:
        detected.append({'id':'double_top','name':'Double Top','signal':'BEARISH','reliability':83,'category':'Chart'})

    # Ascending Triangle
    recent_highs = w['high'].tail(10)
    recent_lows  = w['low'].tail(10)
    if recent_highs.std() < recent_highs.mean()*0.005 and recent_lows.is_monotonic_increasing:
        detected.append({'id':'asc_triangle','name':'Ascending Triangle','signal':'BULLISH','reliability':80,'category':'Chart'})

    # Descending Triangle
    if recent_lows.std() < recent_lows.mean()*0.005 and recent_highs.is_monotonic_decreasing:
        detected.append({'id':'desc_triangle','name':'Descending Triangle','signal':'BEARISH','reliability':80,'category':'Chart'})

    # Cup & Handle (simplified)
    if len(w) >= 20:
        left = float(w.iloc[0]['close'])
        mid  = float(w.iloc[10]['close'])
        right = float(w.iloc[-1]['close'])
        if left > mid and right > mid and abs(left-right)/max(left,0.001) < 0.03:
            detected.append({'id':'cup_handle','name':'Cup & Handle','signal':'BULLISH','reliability':86,'category':'Chart'})

    log.info(f"Detected {len(detected)} patterns")
    return detected


# ══════════════════════════════════════════════════
# SIGNAL FUSION ENGINE
# ══════════════════════════════════════════════════
def generate_signal(indicators, patterns, sentiment_score, pattern_weights):
    """Combine all signals into LONG/SHORT/HOLD with confidence score."""
    bull = 0
    bear = 0
    reasons = []

    # RSI
    rsi = indicators['rsi']
    if rsi < 30:   bull += 3; reasons.append(f'RSI oversold ({rsi:.1f})')
    elif rsi < 40: bull += 1.5; reasons.append(f'RSI low ({rsi:.1f})')
    elif rsi > 70: bear += 3; reasons.append(f'RSI overbought ({rsi:.1f})')
    elif rsi > 60: bear += 1.5; reasons.append(f'RSI high ({rsi:.1f})')

    # MACD
    if indicators['macd_cross_bull']:   bull += 3.5; reasons.append('MACD bullish crossover ⚡')
    elif indicators['macd_cross_bear']: bear += 3.5; reasons.append('MACD bearish crossover ⚡')
    elif indicators['macd_bull']:       bull += 1;   reasons.append('MACD above signal')
    else:                               bear += 1;   reasons.append('MACD below signal')

    # Bollinger Bands
    bp = indicators['bb_pos']
    if bp < 15:   bull += 2.5; reasons.append(f'Price at BB lower ({bp:.0f}%)')
    elif bp < 30: bull += 1;   reasons.append(f'Price near BB lower')
    elif bp > 85: bear += 2.5; reasons.append(f'Price at BB upper ({bp:.0f}%)')
    elif bp > 70: bear += 1;   reasons.append(f'Price near BB upper')

    # Moving Averages
    if indicators['above_ma20'] and indicators['above_ma50']:
        bull += 2; reasons.append('Above MA20 & MA50')
    elif not indicators['above_ma20'] and not indicators['above_ma50']:
        bear += 2; reasons.append('Below MA20 & MA50')

    # EMA cross
    if indicators['ema9'] > indicators['ema21']:
        bull += 1; reasons.append('EMA9 > EMA21')
    else:
        bear += 1; reasons.append('EMA9 < EMA21')

    # Stochastic
    sk = indicators['stoch_k']
    if sk < 20:   bull += 1.5; reasons.append(f'Stoch oversold ({sk:.0f})')
    elif sk > 80: bear += 1.5; reasons.append(f'Stoch overbought ({sk:.0f})')

    # ADX (trend strength)
    adx = indicators['adx']
    if adx > 25: 
        if bull > bear: bull += 1
        else: bear += 1
        reasons.append(f'Strong trend ADX ({adx:.0f})')

    # OBV
    if indicators['obv_rising'] and bull > bear:
        bull += 1; reasons.append('OBV confirming bullish')
    elif not indicators['obv_rising'] and bear > bull:
        bear += 1; reasons.append('OBV confirming bearish')

    # Pattern scores with learned weights
    for pat in patterns:
        weight = pattern_weights.get(pat['id'], pat['reliability']) / 100
        score = 2.5 * weight
        if pat['signal'] == 'BULLISH':
            bull += score; reasons.append(f"📊 {pat['name']} ({pat['reliability']}%)")
        elif pat['signal'] == 'BEARISH':
            bear += score; reasons.append(f"📊 {pat['name']} ({pat['reliability']}%)")

    # Sentiment
    if sentiment_score > 0.4:   bull += 2;   reasons.append(f'News bullish ({sentiment_score:+.2f})')
    elif sentiment_score > 0.2: bull += 1;   reasons.append(f'News mildly bullish')
    elif sentiment_score < -0.4: bear += 2;  reasons.append(f'News bearish ({sentiment_score:+.2f})')
    elif sentiment_score < -0.2: bear += 1;  reasons.append(f'News mildly bearish')

    total = bull + bear
    confidence = abs(bull-bear)/total*100 if total > 0 else 0

    if bull > bear and confidence >= 25:   signal = 'LONG'
    elif bear > bull and confidence >= 25: signal = 'SHORT'
    else:                                  signal = 'HOLD'

    return {
        'signal':     signal,
        'bull_score': round(bull, 2),
        'bear_score': round(bear, 2),
        'confidence': round(confidence, 1),
        'reasons':    reasons[:8],
    }


# ══════════════════════════════════════════════════
# PAPER TRADING ENGINE
# ══════════════════════════════════════════════════
def paper_trade(pair, signal_data, price):
    """Execute paper trade for a specific pair."""
    ps = paper_states[pair]
    result = {'action':'none','pair':pair,'price':price}

    # Check SL/TP on open position
    if ps['position']:
        entry = ps['entry_price']
        pos   = ps['position']
        sl    = ps['stop_loss']
        tp    = ps['take_profit']
        hit_sl = (pos=='long' and price<=sl) or (pos=='short' and price>=sl)
        hit_tp = (pos=='long' and price>=tp) or (pos=='short' and price<=tp)

        if hit_sl or hit_tp:
            pnl = ((price-entry)/entry if pos=='long' else (entry-price)/entry) * TRADE_AMOUNT * LEVERAGE
            ps['balance'] += pnl
            ps['total_pnl'] += pnl
            if pnl > 0: ps['wins'] += 1
            else: ps['losses'] += 1
            trade = {
                'pair':    pair,
                'type':    pos.upper(),
                'entry':   entry,
                'exit':    price,
                'pnl':     round(pnl, 2),
                'outcome': 'WIN' if pnl>0 else 'LOSS',
                'reason':  'STOP LOSS' if hit_sl else 'TAKE PROFIT',
                'time':    datetime.now().isoformat(),
            }
            ps['trades'].append(trade)
            ps['position'] = ps['entry_price'] = ps['stop_loss'] = ps['take_profit'] = None
            result = {'action':'close','pair':pair,'price':price,'pnl':round(pnl,2),
                     'outcome':trade['outcome'],'message':f"[{pair}] {trade['outcome']} ${pnl:+.2f}"}
            return result, trade

    # Open new position
    sig = signal_data['signal']
    if sig in ('LONG','SHORT') and ps['position'] is None:
        if sig == 'LONG':
            sl = round(price*(1-STOP_LOSS_PCT/100), 6)
            tp = round(price*(1+TAKE_PROFIT_PCT/100), 6)
        else:
            sl = round(price*(1+STOP_LOSS_PCT/100), 6)
            tp = round(price*(1-TAKE_PROFIT_PCT/100), 6)
        ps['position']    = sig.lower()
        ps['entry_price'] = price
        ps['entry_time']  = datetime.now().isoformat()
        ps['stop_loss']   = sl
        ps['take_profit'] = tp
        result = {'action':'open','pair':pair,'type':sig,'entry':price,'sl':sl,'tp':tp,
                 'message':f"[PAPER][{pair}] {sig} @ ${price:.4f} SL:${sl:.4f} TP:${tp:.4f}"}

    return result, None


def get_pair_state(pair):
    """Clean state dict for dashboard."""
    ps = paper_states[pair]
    total = ps['wins'] + ps['losses']
    win_rate = (ps['wins']/total*100) if total > 0 else 0
    price = get_price(pair) or 0
    unrealized = 0
    if ps['position'] and ps['entry_price'] and price:
        if ps['position'] == 'long':
            unrealized = (price-ps['entry_price'])/ps['entry_price']*TRADE_AMOUNT*LEVERAGE
        else:
            unrealized = (ps['entry_price']-price)/ps['entry_price']*TRADE_AMOUNT*LEVERAGE
    return {
        'pair':          pair,
        'balance':       round(ps['balance'], 2),
        'position':      ps['position'].upper() if ps['position'] else 'NONE',
        'entry_price':   ps['entry_price'],
        'stop_loss':     ps['stop_loss'],
        'take_profit':   ps['take_profit'],
        'unrealized_pnl':round(unrealized, 2),
        'total_pnl':     round(ps['total_pnl'], 2),
        'total_trades':  total,
        'wins':          ps['wins'],
        'losses':        ps['losses'],
        'win_rate':      round(win_rate, 1),
        'recent_trades': ps['trades'][-5:],
        'color':         PAIR_COLORS.get(pair, '#ffffff'),
    }


def get_all_states():
    return {pair: get_pair_state(pair) for pair in ACTIVE_PAIRS}
