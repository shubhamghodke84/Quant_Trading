"""
Breakout Strategy - Donchian Channel breakouts with research-backed improvements.

Research basis (2025-2026):
- arXiv:2602.18912: Volatility-normalized breakout thresholds (θ=1.5-2.5×σ); vol-spike suppression
- arXiv:2602.11708: ADX rising as momentum confirmation; regime-conditional Sharpe decomposition
- arXiv:2510.03236: BB squeeze empirically validates low-vol preceding high-vol expansion
- arXiv:2405.08101: Volume ratio 1.3-1.5× needed for genuine aggressive flow detection
- arXiv:2307.10649: U-shaped intraday volume — London/NY open breakouts have highest follow-through
- arXiv:2601.19504: Asymmetric long/short sizing — 70/30 bias; SELL needs higher conviction

Key improvements over prior version:
1. Bug fix: bb_width_avg (NameError) → replaced with bb_prior_avg throughout
2. Minimum breakout distance: (close - donchian_upper) / ATR >= 0.15 — filters barely-cleared levels
3. ATR vol-spike suppression: suppress when ATR > 1.5× 20-bar mean (fear/overreaction regime)
4. H1 HTF trend alignment: only long when H1 EMA21 rising; only short when falling (cached, every 60 bars)
5. Body/ATR ratio raised: 0.35 → 0.45 (research optimal θ threshold for genuine breakout bars)
6. Volume ratio default raised: 1.2 → 1.35 (aggressive flow detection, not just noise expansion)
7. Asymmetric SELL strength: Gold's long-term upward drift → SELL threshold = BUY threshold + 0.05
8. Regime filter: restored to default global thresholds (ADX=25, Hurst=True, score≥2)
   Previously used loose custom params (ADX=15, Hurst=False) that fired too readily on ranging bars

Entry Logic (HIGH WIN-RATE version):
- Only trade when regime = TREND (global regime filter — ADX + Hurst, score ≥ 2)
- Close must breach Donchian channel (previous bar's upper/lower, not current)
- Breakout close must be >= min_breakout_atr_dist × ATR beyond the channel
- BB squeeze must have occurred in the prior lookback window (coiled energy prerequisite)
- Bar body >= min_body_atr_ratio × ATR (doji/wick-only breakouts filtered)
- ATR must not be in fear/vol-spike territory (ATR < atr_spike_mult × 20-bar ATR MA)
- H1 EMA21 direction must align with breakout direction (when available)
- ADX >= adx_min_threshold AND rising (strengthening trend at breakout bar)
- VWAP alignment: price above VWAP for longs, below for shorts
- RSI not overbought/oversold at entry
- Stochastic %K: not already overbought/oversold
- Volume >= volume_ratio_min × 20-bar average
- Minimum signal strength gate (BUY: min_signal_strength, SELL: +0.05 asymmetric)

Exit Logic:
- Stop loss: ATR-based, capped at opposite Donchian boundary
- Take profit: Configurable reward/risk ratio
"""

from typing import Optional, Dict
import pandas as pd

from .base_strategy import BaseStrategy
from .regime_filter import RegimeFilter
from .multi_timeframe_filter import MultiTimeframeFilter
from ..core.types import Symbol, Signal
from ..core.constants import MarketRegime, OrderSide
from ..data.indicators import Indicators


class BreakoutStrategy(BaseStrategy):
    """Donchian Channel breakout strategy with volume, RSI, BB squeeze, Stochastic, ATR-stop, and HTF-alignment filters."""

    def __init__(self, symbol: Symbol, config: dict):
        super().__init__(symbol, config)

        self.donchian_period = config.get('donchian_period', 20)
        self.confirmation_bars = config.get('confirmation_bars', 0)
        self.only_in_regime = MarketRegime[config.get('only_in_regime', 'TREND')]

        self.adx_min_threshold = config.get('adx_min_threshold', 25)

        # Volume raised from default 1.2 → 1.35 (arXiv:2405.08101 — aggressive flow detection)
        self.volume_confirmation = config.get('volume_confirmation', True)
        self.volume_ratio_min = config.get('volume_ratio_min', 1.35)

        self.rsi_overbought = config.get('rsi_overbought', 75)
        self.rsi_oversold = config.get('rsi_oversold', 25)

        self.bb_squeeze_lookback = config.get('bb_squeeze_lookback', 10)

        self.stoch_overbought = config.get('stoch_overbought', 80)
        self.stoch_oversold = config.get('stoch_oversold', 20)

        # Body/ATR ratio raised from 0.35 → 0.45 (arXiv:2602.18912 — θ=1.5-2.5×σ quality bar)
        self.min_body_atr_ratio = config.get('min_body_atr_ratio', 0.45)

        # Minimum breakout distance beyond the channel (arXiv:2602.18912 — volatility-normalized)
        # Filters "barely cleared" entries that frequently reverse back through the level.
        self.min_breakout_atr_dist = config.get('min_breakout_atr_dist', 0.15)

        # ATR vol-spike suppression (arXiv:2602.18912 — fear regime reversals, not continuation)
        self.atr_spike_mult = config.get('atr_spike_mult', 1.5)
        self.atr_ma_period = config.get('atr_ma_period', 20)

        # Asymmetric SELL strength (arXiv:2601.19504 — Gold upward drift; 70/30 long/short bias)
        self.min_signal_strength = config.get('min_signal_strength', 0.70)
        self.min_signal_strength_sell = config.get('min_signal_strength_sell',
                                                    self.min_signal_strength + 0.05)

        self.max_ml_fakeout_prob = config.get('max_ml_fakeout_prob', 1.0)

        self.mtf_confirmation = config.get('mtf_confirmation', False)
        self.mtf_filter = MultiTimeframeFilter() if self.mtf_confirmation else None

        # Regime filter — global thresholds (ADX=25, Hurst=True, score≥2).
        # Previously overridden with loose custom params (ADX=15, Hurst=False) that classified
        # ranging bars as TREND, causing spurious breakout signals. Restored to default.
        self.regime_filter = RegimeFilter()

        # H1 HTF trend alignment cache (same pattern as vwap_strategy)
        self._h1_last_len: int = 0
        self._h1_trend_cached: Optional[bool] = None

        self.last_breakout_bar = None
        self._pending_bars_by_tf: Dict[str, pd.DataFrame] = {}

    def get_name(self) -> str:
        return "donchian_breakout"

    def set_higher_tf_bars(self, bars_by_tf: Dict[str, pd.DataFrame]) -> None:
        self._pending_bars_by_tf = bars_by_tf

    def _get_h1_trend(self, bars: pd.DataFrame) -> Optional[bool]:
        """
        Return True if H1 EMA21 is rising (bullish HTF trend),
        False if falling, None if insufficient data.

        Cached — only resamples when 60+ new 1m bars have arrived.
        Consistent with vwap_strategy resample pattern (DatetimeIndex assumed).
        """
        if len(bars) >= self._h1_last_len + 60:
            try:
                h1 = (
                    bars.resample('1h')
                    .agg({'open': 'first', 'high': 'max',
                          'low': 'min', 'close': 'last', 'volume': 'sum'})
                    .dropna(subset=['open', 'close'])
                )
                if len(h1) >= 23:
                    ema21 = Indicators.ema(h1, period=21)
                    if not pd.isna(ema21.iloc[-1]) and not pd.isna(ema21.iloc[-2]):
                        self._h1_trend_cached = bool(ema21.iloc[-1] > ema21.iloc[-2])
            except Exception:
                pass
            self._h1_last_len = len(bars)
        return self._h1_trend_cached

    def on_bar(self, bars: pd.DataFrame) -> Optional[Signal]:
        if not self.is_enabled():
            return None

        if len(bars) < self.donchian_period + self.bb_squeeze_lookback * 2 + 5:
            self._log_no_signal("Insufficient data")
            return None

        # Regime check — use ML prediction when available, else fall back to rule-based.
        regime = self.ml_regime if self.ml_regime is not None else self.regime_filter.classify(bars)
        if regime != self.only_in_regime:
            source = "ML" if self.ml_regime is not None else "rule"
            self._log_no_signal(f"Regime is {regime.value} ({source}), need {self.only_in_regime.value}")
            return None

        upper, middle, lower = Indicators.donchian_channel(bars, period=self.donchian_period)

        atr = Indicators.atr(bars, period=14)
        rsi = Indicators.rsi(bars, period=14)
        adx = Indicators.adx(bars, period=14)
        vwap = Indicators.vwap(bars)
        stoch_k, stoch_d = Indicators.stochastic(bars, period=14)
        bb_w = Indicators.bb_width(bars, period=20)
        _, _, macd_hist = Indicators.macd(bars, fast_period=12, slow_period=26, signal_period=9)

        current_close = bars['close'].iloc[-1]
        current_high  = bars['high'].iloc[-1]
        current_low   = bars['low'].iloc[-1]
        current_atr = atr.iloc[-1]
        current_rsi = rsi.iloc[-1]
        current_adx = adx.iloc[-1]
        prev_adx = adx.iloc[-2]
        current_vwap = vwap.iloc[-1]
        current_stoch_k = stoch_k.iloc[-1]
        current_bb_width = bb_w.iloc[-1]
        current_macd_hist = macd_hist.iloc[-1]

        if any(pd.isna([current_atr, current_rsi, current_adx, prev_adx, current_vwap,
                         current_stoch_k, current_bb_width, current_macd_hist])):
            self._log_no_signal("Indicator calculation failed")
            return None

        # Bar range for close-quality filter
        bar_range = float(current_high - current_low)
        close_pos = (float(current_close) - float(current_low)) / bar_range if bar_range > 0 else 0.5

        # ATR vol-spike suppression (arXiv:2602.18912)
        atr_ma = atr.rolling(window=self.atr_ma_period).mean().iloc[-1]
        if not pd.isna(atr_ma) and atr_ma > 0:
            if float(current_atr) > self.atr_spike_mult * float(atr_ma):
                self._log_no_signal(
                    f"ATR spike suppression: "
                    f"ATR={current_atr:.2f} > {self.atr_spike_mult}× MA={atr_ma:.2f}")
                return None

        # ADX must be rising into the breakout — confirms strengthening momentum
        if current_adx <= prev_adx:
            self._log_no_signal(
                f"ADX not rising ({current_adx:.1f} <= {prev_adx:.1f}), breakout lacks momentum")
            return None

        # BB squeeze: recent lookback must be tighter than the prior baseline.
        # Squeeze validates coiled energy before the expansion (arXiv:2510.03236).
        bb_recent_avg = bb_w.iloc[-self.bb_squeeze_lookback - 1:-1].mean()
        bb_prior_avg = bb_w.iloc[-self.bb_squeeze_lookback * 2 - 1:-self.bb_squeeze_lookback - 1].mean()
        bb_squeeze_ok = (bb_prior_avg > 0) and (bb_recent_avg < bb_prior_avg * 1.05)

        if not bb_squeeze_ok:
            self._log_no_signal(
                f"No BB squeeze: recent_avg={bb_recent_avg:.4f} not tight vs prior_avg={bb_prior_avg:.4f}")
            return None

        # Squeeze depth (0→1): deeper squeeze = stronger breakout bonus
        squeeze_depth = max(0.0, (bb_prior_avg - bb_recent_avg) / bb_prior_avg) if bb_prior_avg > 0 else 0.0

        # H1 HTF trend direction (cached every 60 bars)
        h1_trend = self._get_h1_trend(bars)

        # Use previous channel values for breakout level (avoids lookahead on current bar)
        breakout_upper = upper.iloc[-2]
        breakout_lower = lower.iloc[-2]

        # Volume confirmation
        volume_ok = True
        volume_ratio = 0.0
        if self.volume_confirmation and 'volume' in bars.columns:
            current_volume = bars['volume'].iloc[-1]
            avg_volume = bars['volume'].iloc[-21:-1].mean()
            if avg_volume > 0:
                volume_ratio = current_volume / avg_volume
                volume_ok = volume_ratio >= self.volume_ratio_min

        current_open = float(bars['open'].iloc[-1])
        bar_body = abs(current_close - current_open)
        min_body = float(current_atr) * self.min_body_atr_ratio

        # ── Bullish breakout ─────────────────────────────────────────────────
        if current_close > breakout_upper:

            # Body size filter: doji/wick-only breakouts have low follow-through
            if bar_body < min_body:
                self._log_no_signal(
                    f"Bullish breakout: bar body too small ({bar_body:.2f} < {min_body:.2f})")
                return None

            # Close quality: bar must close in top 60% of its range (no upper-wick rejection).
            # A bar that closes near its low despite breaking out is a fakeout signal.
            if close_pos < 0.6:
                self._log_no_signal(
                    f"Bullish breakout: close in lower {close_pos:.0%} of bar range (wick rejection)")
                return None

            # MACD must be positive at breakout — confirms momentum buildup, not just price level
            if current_macd_hist <= 0:
                self._log_no_signal(
                    f"Bullish breakout: MACD histogram negative ({current_macd_hist:.4f}), no momentum")
                return None

            # Minimum distance filter: close must clear the channel by at least 0.15 ATR.
            # arXiv:2602.18912: barely-cleared levels fail at > 60% rate vs cleared levels.
            breakout_dist = (current_close - float(breakout_upper)) / float(current_atr)
            if breakout_dist < self.min_breakout_atr_dist:
                self._log_no_signal(
                    f"Breakout distance too small "
                    f"({breakout_dist:.3f} ATR < {self.min_breakout_atr_dist})")
                return None

            if current_adx < self.adx_min_threshold:
                self._log_no_signal(f"ADX too low ({current_adx:.1f} < {self.adx_min_threshold})")
                return None

            if current_close < current_vwap:
                self._log_no_signal("Price below VWAP, rejecting LONG breakout")
                return None

            if current_rsi > self.rsi_overbought:
                self._log_no_signal(f"RSI overbought ({current_rsi:.1f} > {self.rsi_overbought})")
                return None

            if current_stoch_k > self.stoch_overbought:
                self._log_no_signal(
                    f"Stochastic already overbought (%K={current_stoch_k:.1f}), skipping LONG")
                return None

            if not volume_ok:
                self._log_no_signal(f"Volume too low (ratio={volume_ratio:.2f})")
                return None

            # H1 alignment: reject LONG against bearish HTF trend; allow when H1 unavailable
            if h1_trend is False:
                self._log_no_signal("H1 EMA21 bearish — rejecting LONG breakout against HTF trend")
                return None

            if self.mtf_confirmation and self.mtf_filter:
                if not self.mtf_filter.confirm_signal('BUY', self._pending_bars_by_tf):
                    self._log_no_signal("MTF confirmation failed for BUY")
                    return None

            ml_fakeout_prob = self.config.get('diagnostics', {}).get('fakeout_prob', 0.0)
            if ml_fakeout_prob > self.max_ml_fakeout_prob:
                self._log_no_signal(
                    f"ML rejected: fakeout probability too high ({ml_fakeout_prob:.2f})")
                return None

            # Strength formula (fixed bb_prior_avg replaces undefined bb_width_avg)
            adx_norm = min((float(current_adx) - self.adx_min_threshold) / 50.0, 1.0)
            squeeze_bonus = min(squeeze_depth * 0.20, 0.10)
            dist_bonus = min(breakout_dist / 0.50, 0.15)   # up to 0.15 for a 0.5 ATR breakout
            mtf_bonus = 0.05 if (self.mtf_confirmation and self._pending_bars_by_tf) else 0.0
            h1_bonus = 0.05 if h1_trend is True else 0.0
            strength = min(0.50 + adx_norm * 0.25 + squeeze_bonus + dist_bonus + mtf_bonus + h1_bonus, 1.0)

            if strength < self.min_signal_strength:
                self._log_no_signal(
                    f"Signal strength too low ({strength:.2f} < {self.min_signal_strength})")
                return None

            return self._create_signal(
                side=OrderSide.BUY,
                strength=strength,
                regime=regime,
                entry_price=float(current_close),
                metadata={
                    'breakout_type': 'upper',
                    'donchian_upper': float(breakout_upper),
                    'donchian_lower': float(breakout_lower),
                    'atr': float(current_atr),
                    'rsi': float(current_rsi),
                    'adx': float(current_adx),
                    'vwap': float(current_vwap),
                    'stoch_k': float(current_stoch_k),
                    'bb_width': float(current_bb_width),
                    'bb_prior_avg': float(bb_prior_avg),
                    'squeeze_depth': float(squeeze_depth),
                    'breakout_dist_atr': float(breakout_dist),
                    'volume_ratio': float(volume_ratio),
                    'h1_trend': h1_trend,
                    'mtf_confirmed': bool(self.mtf_confirmation and self._pending_bars_by_tf)
                }
            )

        # ── Bearish breakout ─────────────────────────────────────────────────
        if current_close < breakout_lower:

            if bar_body < min_body:
                self._log_no_signal(
                    f"Bearish breakout: bar body too small ({bar_body:.2f} < {min_body:.2f})")
                return None

            # Close quality: bar must close in bottom 40% of its range (no lower-wick rejection)
            if close_pos > 0.4:
                self._log_no_signal(
                    f"Bearish breakout: close in upper {close_pos:.0%} of bar range (wick rejection)")
                return None

            # MACD must be negative at breakout
            if current_macd_hist >= 0:
                self._log_no_signal(
                    f"Bearish breakout: MACD histogram positive ({current_macd_hist:.4f}), no downward momentum")
                return None

            breakout_dist = (float(breakout_lower) - current_close) / float(current_atr)
            if breakout_dist < self.min_breakout_atr_dist:
                self._log_no_signal(
                    f"Breakout distance too small "
                    f"({breakout_dist:.3f} ATR < {self.min_breakout_atr_dist})")
                return None

            if current_adx < self.adx_min_threshold:
                self._log_no_signal(
                    f"ADX too low for bearish breakout ({current_adx:.1f} < {self.adx_min_threshold})")
                return None

            if current_close > current_vwap:
                self._log_no_signal("Price above VWAP, rejecting SHORT breakout")
                return None

            if current_rsi < self.rsi_oversold:
                self._log_no_signal(f"RSI oversold ({current_rsi:.1f} < {self.rsi_oversold})")
                return None

            if current_stoch_k < self.stoch_oversold:
                self._log_no_signal(
                    f"Stochastic already oversold (%K={current_stoch_k:.1f}), skipping SHORT")
                return None

            if not volume_ok:
                self._log_no_signal(f"Volume too low (ratio={volume_ratio:.2f})")
                return None

            # H1 alignment: reject SHORT against bullish HTF trend; allow when H1 unavailable
            if h1_trend is True:
                self._log_no_signal("H1 EMA21 bullish — rejecting SHORT breakout against HTF trend")
                return None

            if self.mtf_confirmation and self.mtf_filter:
                if not self.mtf_filter.confirm_signal('SELL', self._pending_bars_by_tf):
                    self._log_no_signal("MTF confirmation failed for SELL")
                    return None

            ml_fakeout_prob = self.config.get('diagnostics', {}).get('fakeout_prob', 0.0)
            if ml_fakeout_prob > self.max_ml_fakeout_prob:
                self._log_no_signal(
                    f"ML rejected: fakeout probability too high ({ml_fakeout_prob:.2f})")
                return None

            adx_norm = min((float(current_adx) - self.adx_min_threshold) / 50.0, 1.0)
            squeeze_bonus = min(squeeze_depth * 0.20, 0.10)
            dist_bonus = min(breakout_dist / 0.50, 0.15)
            mtf_bonus = 0.05 if (self.mtf_confirmation and self._pending_bars_by_tf) else 0.0
            h1_bonus = 0.05 if h1_trend is False else 0.0
            strength = min(0.50 + adx_norm * 0.25 + squeeze_bonus + dist_bonus + mtf_bonus + h1_bonus, 1.0)

            # Asymmetric SELL threshold: Gold's upward drift means shorts need higher conviction
            if strength < self.min_signal_strength_sell:
                self._log_no_signal(
                    f"SELL strength too low ({strength:.2f} < {self.min_signal_strength_sell})")
                return None

            return self._create_signal(
                side=OrderSide.SELL,
                strength=strength,
                regime=regime,
                entry_price=float(current_close),
                metadata={
                    'breakout_type': 'lower',
                    'donchian_upper': float(breakout_upper),
                    'donchian_lower': float(breakout_lower),
                    'atr': float(current_atr),
                    'rsi': float(current_rsi),
                    'adx': float(current_adx),
                    'vwap': float(current_vwap),
                    'stoch_k': float(current_stoch_k),
                    'bb_width': float(current_bb_width),
                    'bb_prior_avg': float(bb_prior_avg),
                    'squeeze_depth': float(squeeze_depth),
                    'breakout_dist_atr': float(breakout_dist),
                    'volume_ratio': float(volume_ratio),
                    'h1_trend': h1_trend,
                    'mtf_confirmed': bool(self.mtf_confirmation and self._pending_bars_by_tf)
                }
            )

        self._log_no_signal("No breakout detected")
        return None
