"""
CRYPTOBOT - AI Brain
Claude analyzes every closed trade, updates pattern weights per pair
Bot learns and adapts independently to improve profitability
"""

import os
import json
import logging
from datetime import datetime
from anthropic import Anthropic

log = logging.getLogger(__name__)

try:
    client = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY',''))
except Exception as e:
    log.warning(f"Anthropic client init failed: {e}")
    client = None

WEIGHT_FILE = 'pattern_weights.json'

DEFAULT_WEIGHTS = {
    'hammer':72,'inv_hammer':65,'dragonfly_doji':68,'gravestone_doji':70,
    'doji':55,'bull_marubozu':78,'bear_marubozu':77,'shooting_star':74,
    'hanging_man':65,'bull_engulfing':82,'bear_engulfing':81,
    'tweezer_top':70,'tweezer_bottom':70,'piercing':74,'dark_cloud':73,
    'morning_star':84,'evening_star':83,'three_soldiers':83,'three_crows':82,
    'bull_flag':85,'bear_flag':84,'double_bottom':83,'double_top':83,
    'asc_triangle':80,'desc_triangle':80,'cup_handle':86,
}

brain_memory = []
ai_review_count = 0


def load_weights():
    try:
        if os.path.exists(WEIGHT_FILE):
            with open(WEIGHT_FILE,'r') as f:
                return json.load(f)
    except:
        pass
    return dict(DEFAULT_WEIGHTS)


def save_weights(weights):
    try:
        with open(WEIGHT_FILE,'w') as f:
            json.dump(weights, f, indent=2)
    except Exception as e:
        log.error(f"Save weights error: {e}")


pattern_weights = load_weights()


def analyze_trade(trade, patterns, indicators, sentiment, pair):
    """Send closed trade to Claude for analysis and weight updates."""
    global pattern_weights, brain_memory, ai_review_count

    if not client or not os.getenv('ANTHROPIC_API_KEY'):
        log.warning("No API key — skipping AI analysis")
        return None

    pat_names = [p['name'] for p in patterns] if patterns else ['No pattern']
    pnl = trade.get('pnl', 0)
    outcome = trade.get('outcome', 'UNKNOWN')

    prompt = f"""You are the AI brain of an autonomous crypto trading bot.

Analyze this closed {pair} trade and update pattern confidence weights.

TRADE:
- Pair: {pair}
- Type: {trade.get('type')}
- Entry: ${trade.get('entry',0):.4f}
- Exit: ${trade.get('exit',0):.4f}
- P&L: ${pnl:+.2f}
- Outcome: {outcome}
- Closed by: {trade.get('reason')}

SIGNALS AT ENTRY:
- Patterns: {', '.join(pat_names)}
- RSI: {indicators.get('rsi','N/A')}
- MACD: {'Bullish' if indicators.get('macd_bull') else 'Bearish'}
- BB Position: {indicators.get('bb_pos','N/A')}%
- ADX: {indicators.get('adx','N/A')}
- Sentiment: {sentiment:+.2f}

CURRENT WEIGHTS: {json.dumps({p:pattern_weights.get(p,'N/A') for p in [pat['id'] for pat in patterns] if patterns}, indent=2)}

Respond ONLY in this exact JSON format (no markdown):
{{
  "verdict": "pattern_valid | pattern_failed | external_factor",
  "explanation": "brief technical explanation max 50 words",
  "weight_changes": {{"pattern_id": integer_delta}},
  "key_lesson": "one actionable insight for {pair} trading",
  "confidence": 0-100,
  "adapt_stop_loss": null or new_percentage,
  "adapt_take_profit": null or new_percentage
}}

Rules:
- external_factor if >4% candle caused loss unexpectedly — set weight_changes to {{}}
- Max weight change: +5 for wins, -4 for losses
- Weights stay between 40-95
- adapt_stop_loss/take_profit: suggest new % if pattern suggests adjustment"""

    try:
        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=600,
            messages=[{'role':'user','content':prompt}]
        )
        raw = response.content[0].text.strip()
        analysis = json.loads(raw)

        changes_applied = {}
        if analysis.get('verdict') != 'external_factor':
            for pat_id, delta in analysis.get('weight_changes',{}).items():
                if pat_id in pattern_weights:
                    old = pattern_weights[pat_id]
                    new = max(40, min(95, old+int(delta)))
                    pattern_weights[pat_id] = new
                    changes_applied[pat_id] = {'from':old,'to':new,'delta':delta}
            save_weights(pattern_weights)

        # Adaptive SL/TP
        adapt_sl = analysis.get('adapt_stop_loss')
        adapt_tp = analysis.get('adapt_take_profit')

        memory_entry = {
            'timestamp':      datetime.now().isoformat(),
            'pair':           pair,
            'trade':          trade,
            'patterns':       pat_names,
            'verdict':        analysis.get('verdict'),
            'explanation':    analysis.get('explanation'),
            'key_lesson':     analysis.get('key_lesson'),
            'weight_changes': changes_applied,
            'confidence':     analysis.get('confidence',0),
            'adapt_sl':       adapt_sl,
            'adapt_tp':       adapt_tp,
        }
        brain_memory.append(memory_entry)
        if len(brain_memory) > 200:
            brain_memory.pop(0)

        ai_review_count += 1
        log.info(f"AI analysis [{pair}]: {analysis.get('verdict')} | Changes: {changes_applied}")
        return memory_entry

    except json.JSONDecodeError:
        log.error("Claude returned invalid JSON")
        return None
    except Exception as e:
        log.error(f"AI analysis error: {e}")
        return None


def get_brain_summary():
    total = len(brain_memory)
    verdicts = [m['verdict'] for m in brain_memory if m.get('verdict')]
    pair_performance = {}
    for m in brain_memory:
        p = m.get('pair','?')
        if p not in pair_performance:
            pair_performance[p] = {'wins':0,'losses':0}
        if m.get('trade',{}).get('outcome') == 'WIN':
            pair_performance[p]['wins'] += 1
        else:
            pair_performance[p]['losses'] += 1

    return {
        'total_analyses':    total,
        'ai_reviews':        ai_review_count,
        'pattern_valid':     verdicts.count('pattern_valid'),
        'pattern_failed':    verdicts.count('pattern_failed'),
        'external_factor':   verdicts.count('external_factor'),
        'current_weights':   dict(pattern_weights),
        'top_patterns':      sorted(pattern_weights.items(), key=lambda x:x[1], reverse=True)[:8],
        'recent_memory':     brain_memory[-15:][::-1],
        'pair_performance':  pair_performance,
    }


def get_weights():
    return pattern_weights
