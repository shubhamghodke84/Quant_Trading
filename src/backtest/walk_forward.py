"""
Walk-Forward Validator — Out-of-Sample Strategy Testing.

Splits historical bars into rolling in-sample (IS) / out-of-sample (OOS) windows
and runs the backtest engine on each, reporting IS vs OOS metric divergence.

A well-behaved strategy has:
  - OOS Sharpe ≥ 0.5 × IS Sharpe  (less than 50% degradation)
  - OOS profit factor ≥ 1.0        (still profitable out-of-sample)
  - OOS win rate within 10pp of IS  (not overfitting entry filter)

Usage:
    from src.backtest.walk_forward import WalkForwardValidator
    wfv = WalkForwardValidator(strategy_factory, bars, initial_capital, risk_config)
    results = wfv.run(n_splits=5, oos_ratio=0.3)
    wfv.print_report(results)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from decimal import Decimal
import pandas as pd
import numpy as np

from .backtest_engine import BacktestEngine, BacktestResult
from ..strategies.base_strategy import BaseStrategy


@dataclass
class WFWindow:
    """Single walk-forward window result."""
    window_idx: int
    is_start: str
    is_end: str
    oos_start: str
    oos_end: str
    is_result: BacktestResult
    oos_result: BacktestResult

    @property
    def sharpe_degradation(self) -> float:
        """How much Sharpe drops IS→OOS (0.0 = no drop, 1.0 = full collapse)."""
        if self.is_result.sharpe_ratio <= 0:
            return 1.0
        return 1.0 - (self.oos_result.sharpe_ratio / self.is_result.sharpe_ratio)

    @property
    def is_oos_stable(self) -> bool:
        """True when OOS performance is within acceptable bounds."""
        return (
            self.oos_result.sharpe_ratio >= 0.5 * self.is_result.sharpe_ratio
            and self.oos_result.profit_factor >= 1.0
            and abs(self.oos_result.win_rate - self.is_result.win_rate) <= 0.10
        )


@dataclass
class WalkForwardResult:
    """Aggregate walk-forward validation results."""
    windows: List[WFWindow] = field(default_factory=list)

    @property
    def n_stable(self) -> int:
        return sum(1 for w in self.windows if w.is_oos_stable)

    @property
    def stability_rate(self) -> float:
        return self.n_stable / len(self.windows) if self.windows else 0.0

    @property
    def avg_oos_sharpe(self) -> float:
        sharpes = [w.oos_result.sharpe_ratio for w in self.windows]
        return float(np.mean(sharpes)) if sharpes else 0.0

    @property
    def avg_oos_profit_factor(self) -> float:
        pfs = [w.oos_result.profit_factor for w in self.windows]
        return float(np.mean(pfs)) if pfs else 0.0

    @property
    def avg_oos_win_rate(self) -> float:
        wrs = [w.oos_result.win_rate for w in self.windows]
        return float(np.mean(wrs)) if wrs else 0.0

    @property
    def avg_sharpe_degradation(self) -> float:
        degs = [w.sharpe_degradation for w in self.windows]
        return float(np.mean(degs)) if degs else 1.0


class WalkForwardValidator:
    """
    Rolling walk-forward validation for any BacktestEngine-compatible strategy.

    Args:
        strategy_factory: Callable() → BaseStrategy — creates a fresh strategy instance
                          for each window (prevents state bleed between windows).
        bars: Full historical OHLCV DataFrame (chronologically sorted).
        initial_capital: Starting capital per window.
        risk_config: Risk engine configuration dict.
        commission_per_trade: Commission cost per trade.
        slippage_model: "realistic" | "fixed" | "aggressive".
    """

    def __init__(
        self,
        strategy_factory: Callable[[], BaseStrategy],
        bars: pd.DataFrame,
        initial_capital: Decimal,
        risk_config: Dict,
        commission_per_trade: Decimal = Decimal("0"),
        slippage_model: str = "realistic",
    ):
        self.strategy_factory = strategy_factory
        self.bars = bars.reset_index(drop=True)
        self.initial_capital = initial_capital
        self.risk_config = risk_config
        self.commission_per_trade = commission_per_trade
        self.slippage_model = slippage_model

    def run(
        self,
        n_splits: int = 5,
        oos_ratio: float = 0.30,
        min_is_bars: int = 500,
    ) -> WalkForwardResult:
        """
        Run walk-forward validation across N rolling windows.

        Args:
            n_splits: Number of IS/OOS splits to run (≥ 3 recommended).
            oos_ratio: Fraction of each window allocated to OOS testing (default 30%).
            min_is_bars: Minimum IS bars required — skip window if insufficient.

        Returns:
            WalkForwardResult with per-window and aggregate metrics.
        """
        total_bars = len(self.bars)
        # Window size so that N splits cover the full dataset
        window_size = total_bars // n_splits
        oos_size = max(1, int(window_size * oos_ratio))
        is_size = window_size - oos_size

        result = WalkForwardResult()

        for i in range(n_splits):
            is_start_idx = i * window_size
            is_end_idx = is_start_idx + is_size
            oos_end_idx = min(is_end_idx + oos_size, total_bars)

            if is_end_idx - is_start_idx < min_is_bars:
                continue  # Skip undersized windows
            if oos_end_idx <= is_end_idx:
                continue  # No OOS data

            is_bars = self.bars.iloc[is_start_idx:is_end_idx].copy()
            oos_bars = self.bars.iloc[is_end_idx:oos_end_idx].copy()

            is_result = self._run_window(is_bars)
            oos_result = self._run_window(oos_bars)

            def _fmt(idx: int) -> str:
                ts = self.bars.get("timestamp", self.bars.index)
                val = ts.iloc[idx] if idx < len(ts) else idx
                return str(val)[:10]

            window = WFWindow(
                window_idx=i,
                is_start=_fmt(is_start_idx),
                is_end=_fmt(is_end_idx - 1),
                oos_start=_fmt(is_end_idx),
                oos_end=_fmt(oos_end_idx - 1),
                is_result=is_result,
                oos_result=oos_result,
            )
            result.windows.append(window)

        return result

    def _run_window(self, bars: pd.DataFrame) -> BacktestResult:
        """Run a single backtest window with a fresh strategy instance."""
        strategy = self.strategy_factory()
        engine = BacktestEngine(
            strategy=strategy,
            initial_capital=self.initial_capital,
            risk_config=self.risk_config,
            commission_per_trade=self.commission_per_trade,
            slippage_model=self.slippage_model,
        )
        return engine.run(bars)

    @staticmethod
    def print_report(result: WalkForwardResult) -> None:
        """Print a formatted walk-forward summary to stdout."""
        if not result.windows:
            print("  Walk-Forward: no windows completed (insufficient data).")
            return

        print()
        print("  WALK-FORWARD VALIDATION")
        print("  " + "─" * 88)
        print(f"  {'Win':>3}  {'IS Period':>22}  {'OOS Period':>22}  "
              f"{'IS Sh':>6} {'OOS Sh':>6}  {'OOS PF':>6}  {'OOS WR':>6}  {'Stable':>6}")
        print("  " + "─" * 88)

        for w in result.windows:
            stable_icon = "✓" if w.is_oos_stable else "✗"
            color = "\033[92m" if w.is_oos_stable else "\033[91m"
            reset = "\033[0m"
            print(
                f"  {w.window_idx + 1:>3}  "
                f"{w.is_start}→{w.is_end}  "
                f"{w.oos_start}→{w.oos_end}  "
                f"{w.is_result.sharpe_ratio:>6.2f} {w.oos_result.sharpe_ratio:>6.2f}  "
                f"{w.oos_result.profit_factor:>6.2f}  "
                f"{w.oos_result.win_rate:>5.1%}  "
                f"{color}{stable_icon}{reset}"
            )

        print("  " + "─" * 88)
        sc = "\033[92m" if result.stability_rate >= 0.6 else "\033[91m"
        print(
            f"\n  Stability rate : {sc}{result.stability_rate:.0%}\033[0m "
            f"({result.n_stable}/{len(result.windows)} windows stable)"
        )
        print(f"  Avg OOS Sharpe : {result.avg_oos_sharpe:.2f}")
        print(f"  Avg OOS PF     : {result.avg_oos_profit_factor:.2f}")
        print(f"  Avg OOS WR     : {result.avg_oos_win_rate:.1%}")
        print(f"  Avg Sh degr.   : {result.avg_sharpe_degradation:.1%}  "
              f"(< 50% = acceptable)")
        print()

        if result.stability_rate >= 0.6:
            print("  ✓ Strategy is OOS-STABLE — safe to deploy.")
        elif result.stability_rate >= 0.4:
            print("  ⚠ Marginal stability — review entry thresholds before going live.")
        else:
            print("  ✗ Strategy is OVERFITTED — do not deploy without re-tuning.")
        print()
