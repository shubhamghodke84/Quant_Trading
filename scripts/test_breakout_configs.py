#!/usr/bin/env python3
"""Quick breakout config tester — runs multiple configs and prints comparison."""
import sys, yaml, pandas as pd, numpy as np, logging, time
logging.disable(logging.WARNING)
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

with open('config/config_live_50000.yaml') as f:
    config = yaml.safe_load(f)

from src.backtest.backtest_engine import BacktestEngine
from src.strategies.breakout_strategy import BreakoutStrategy
from src.core.types import Symbol

symbol = Symbol(
    ticker='XAUUSD', pip_value=Decimal('0.01'), min_lot=Decimal('0.04'),
    max_lot=Decimal('0.04'), lot_step=Decimal('0.01'), value_per_lot=Decimal('100'),
    min_stops_distance=Decimal('1.0'), leverage=Decimal('30')
)

bars = pd.read_csv('data/historical/XAUUSD_5m_real.csv', parse_dates=['timestamp'], index_col='timestamp')
print(f"Loaded {len(bars)} bars\n")

def run(name, ov):
    cfg = {**config['strategies']['breakout'], **ov}
    s = BreakoutStrategy(symbol, cfg)
    e = BacktestEngine(strategy=s, initial_capital=Decimal('49660.28'), risk_config=config, slippage_model='realistic')
    t0 = time.time()
    r = e.run(bars=bars)
    dt = time.time() - t0
    d = r.total_return / 291
    line = f'{name:<45} T={r.total_trades:>4} WR={r.win_rate:>5.1f}% PF={r.profit_factor:.2f} ${r.total_return:>8.2f} d=${d:>6.2f} ({dt:.0f}s)'
    print(line, flush=True)
    return r

# All new filters OFF
off = {
    'bb_squeeze_enabled': False, 'htf_trend_enabled': False,
    'macd_confirmation': False, 'close_position_pct': 1.0,
    'session_filter_enabled': False
}
gs = [[4,9],[13,16],[21,23]]

print(f"{'Config':<45} {'T':>4} {'WR':>6} {'PF':>5} {'Return':>10} {'Daily':>8} Time")
print("-" * 95)

# 1. v1 baseline (no new filters)
run('v1_baseline', {**off, 'donchian_period': 20, 'cooldown_bars': 8, 'atr_stop_multiplier': 2.5, 'rr_ratio': 2.0})

# 2. Session only
run('session_only', {**off, 'session_filter_enabled': True, 'allowed_sessions': gs, 'donchian_period': 20, 'cooldown_bars': 8, 'atr_stop_multiplier': 2.5, 'rr_ratio': 2.0})

# 3. Session + lower cooldown
run('session+cd3', {**off, 'session_filter_enabled': True, 'allowed_sessions': gs, 'donchian_period': 20, 'cooldown_bars': 3, 'atr_stop_multiplier': 2.5, 'rr_ratio': 2.0})

# 4. Session + DC14 + CD3
run('session+dc14+cd3', {**off, 'session_filter_enabled': True, 'allowed_sessions': gs, 'donchian_period': 14, 'cooldown_bars': 3, 'atr_stop_multiplier': 2.5, 'rr_ratio': 2.0})

# 5. Session + DC14 + CD3 + MACD
run('session+dc14+cd3+macd', {**off, 'session_filter_enabled': True, 'allowed_sessions': gs, 'donchian_period': 14, 'cooldown_bars': 3, 'atr_stop_multiplier': 2.5, 'rr_ratio': 2.0, 'macd_confirmation': True})

# 6. Session + DC14 + CD3 + bar conviction 0.35
run('session+dc14+cd3+conv35', {**off, 'session_filter_enabled': True, 'allowed_sessions': gs, 'donchian_period': 14, 'cooldown_bars': 3, 'atr_stop_multiplier': 2.5, 'rr_ratio': 2.0, 'close_position_pct': 0.35})

# 7. Session + DC14 + CD3 + tighter SL + higher RR
run('session+dc14+cd3+rr2.5', {**off, 'session_filter_enabled': True, 'allowed_sessions': gs, 'donchian_period': 14, 'cooldown_bars': 3, 'atr_stop_multiplier': 2.0, 'rr_ratio': 2.5})

# 8. Session + DC14 + CD3 + MACD + conviction
run('session+dc14+cd3+macd+conv', {**off, 'session_filter_enabled': True, 'allowed_sessions': gs, 'donchian_period': 14, 'cooldown_bars': 3, 'atr_stop_multiplier': 2.5, 'rr_ratio': 2.0, 'macd_confirmation': True, 'close_position_pct': 0.35})

# 9. Best combo attempt: session + dc14 + cd3 + macd + rr2.5
run('session+dc14+cd3+macd+rr2.5', {**off, 'session_filter_enabled': True, 'allowed_sessions': gs, 'donchian_period': 14, 'cooldown_bars': 3, 'atr_stop_multiplier': 2.0, 'rr_ratio': 2.5, 'macd_confirmation': True})

# 10. BB squeeze only test
run('session+dc14+cd3+bb70', {**off, 'session_filter_enabled': True, 'allowed_sessions': gs, 'donchian_period': 14, 'cooldown_bars': 3, 'atr_stop_multiplier': 2.5, 'rr_ratio': 2.0, 'bb_squeeze_enabled': True, 'bb_squeeze_percentile': 70})

print("\nDone!")
