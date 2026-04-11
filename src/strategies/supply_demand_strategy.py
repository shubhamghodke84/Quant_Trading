"""
Supply & Demand Zone Strategy — trades the first retest of a fresh S&D zone.

Core concept (ICT-style):
  A Demand zone is the cluster of consolidation candles immediately before a
  strong bullish impulse. When price returns to that cluster it is likely to
  find buyers again → BUY entry.

  A Supply zone is the equivalent cluster before a strong bearish impulse.
  When price returns it is likely to find sellers → SELL entry.

Zone lifecycle:
  1. An impulse candle is detected  (body >= min_impulse_atr_mult × ATR).
  2. The preceding base is extracted (zone_lookback_bars candles).
  3. Zone is stored as {high, low, direction, formed_at_bar, age}.
  4. Each bar:  check if price enters any stored zone, confirm rejection,
                run confirmation filters, then emit a pure signal.
  5. Zones are invalidated when price closes THROUGH them (consumed)
     or when they exceed zone_max_age_bars (stale → deleted).

Design (codinglegits):
  - Carmack: three detection functions are pure (no side-effects, fully
    testable). Mutable state (_demand_zones, _supply_zones) is modified
    ONLY in on_bar() and is explicitly visible at the call site.
  - geohot:  simplest possible implementation — ATR-scaled boxes, no ML zone
    ranking. Max `max_active_zones` stored; oldest evicted when list is full.
  - TJ Holovachuk: one file, one responsibility. Only existing `Indicators`
    used — zero new dependencies.
  - Jeff Dean: every emitted signal carries full diagnostic metadata so
    post-trade analysis can distinguish zone types and ages.
"""

from typing import Optional, List, Dict, Any, Tuple
import pandas as pd
import numpy as np

from .base_strategy import BaseStrategy
from ..core.types import Symbol, Signal
from ..core.constants import MarketRegime, OrderSide
from ..data.indicators import Indicators


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ZoneDict = Dict[str, Any]
"""
Keys
----
direction       : "demand" | "supply"
high            : float  — top of zone
low             : float  — bottom of zone
formed_at_bar   : int    — absolute bar index when zone was formed
age_bars        : int    — incremented on each on_bar() call
"""


# ---------------------------------------------------------------------------
# Pure detection functions  (Carmack rule — no side-effects, fully testable)
# ---------------------------------------------------------------------------

def _detect_impulse(
    bar_open: float,
    bar_close: float,
    atr: float,
    min_impulse_atr_mult: float,
) -> Optional[str]:
    """Return the direction of a valid impulse candle, or None.

    An impulse occurs when the bar body (|close - open|) exceeds a
    multiple of ATR — indicating a decisive, high-energy move.

    Args:
        bar_open: Open price of the candle under test.
        bar_close: Close price of the candle under test.
        atr: Current Average True Range value.
        min_impulse_atr_mult: Body must be >= this multiple of ATR.

    Returns:
        "bullish", "bearish", or None if the bar is not an impulse.
    """
    if atr <= 0:
        return None

    body = bar_close - bar_open  # signed
    threshold = min_impulse_atr_mult * atr

    if body >= threshold:
        return "bullish"
    if body <= -threshold:
        return "bearish"
    return None


def _build_zone(
    bars: pd.DataFrame,
    impulse_bar_idx: int,
    direction: str,
    lookback_bars: int,
) -> Optional[ZoneDict]:
    """Build a Supply or Demand zone from the candles before an impulse.

    Looks `lookback_bars` candles BEFORE the impulse to identify the base
    (consolidation cluster). The zone is the bounding box of that base.

    Args:
        bars: Full OHLCV DataFrame.
        impulse_bar_idx: Integer position (iloc) of the impulse candle.
        direction: "bullish" → Demand zone | "bearish" → Supply zone.
        lookback_bars: How many candles before the impulse form the base.

    Returns:
        ZoneDict if a valid base is found, None if there is not enough data.
    """
    base_start = impulse_bar_idx - lookback_bars
    if base_start < 0:
        return None

    base = bars.iloc[base_start:impulse_bar_idx]
    if base.empty:
        return None

    zone_high = float(base["high"].max())
    zone_low = float(base["low"].min())

    # Sanity: base must have measurable width
    if zone_high <= zone_low:
        return None

    zone_type = "demand" if direction == "bullish" else "supply"

    return {
        "direction": zone_type,
        "high": zone_high,
        "low": zone_low,
        "formed_at_bar": impulse_bar_idx,
        "age_bars": 0,
    }


def _price_in_zone(
    current_high: float,
    current_low: float,
    zone_high: float,
    zone_low: float,
    tolerance: float,
) -> bool:
    """Return True when the current bar overlaps with the zone (± tolerance).

    Tolerance is ATR-scaled so it adapts to market volatility — avoids
    requiring pixel-perfect touches.

    Args:
        current_high: High of the current bar.
        current_low: Low of the current bar.
        zone_high: Upper boundary of the zone.
        zone_low: Lower boundary of the zone.
        tolerance: ATR-scaled allowance (can be 0 for exact touch requirement).

    Returns:
        True if [current_low - tolerance, current_high + tolerance] overlaps
        with [zone_low, zone_high].
    """
    return (current_low - tolerance) <= zone_high and (current_high + tolerance) >= zone_low


def _zone_consumed(
    bar_close: float,
    zone_high: float,
    zone_low: float,
    direction: str,
) -> bool:
    """Return True when price closes THROUGH the zone, invalidating it.

    A Demand zone is consumed when price closes BELOW it (support failed).
    A Supply zone is consumed when price closes ABOVE it (resistance broken).

    Args:
        bar_close: Close price of the current bar.
        zone_high: Upper boundary of the zone.
        zone_low: Lower boundary of the zone.
        direction: "demand" or "supply".

    Returns:
        True if the zone should be removed.
    """
    if direction == "demand":
        return bar_close < zone_low   # Closed below support → invalidated
    return bar_close > zone_high       # Closed above resistance → invalidated


def _rejection_ratio(
    bar_open: float,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    direction: str,
) -> float:
    """Calculate how strongly a bar rejects the zone.

    For a Demand zone (expect bounce up) we want a long lower wick.
    For a Supply zone (expect bounce down) we want a long upper wick.

    Returns a ratio in [0, 1]. Higher means stronger rejection.
    Returns 0.0 for a zero-range bar.
    """
    bar_range = bar_high - bar_low
    if bar_range <= 0:
        return 0.0

    if direction == "demand":
        # Lower wick = distance from low to min(open, close)
        wick = min(bar_open, bar_close) - bar_low
    else:
        # Upper wick = distance from max(open, close) to high
        wick = bar_high - max(bar_open, bar_close)

    return max(0.0, wick / bar_range)


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

class SupplyDemandStrategy(BaseStrategy):
    """Trade confirmed retests of algorithmically-detected S&D zones.

    Zones are formed around the base of impulse moves (large-body candles).
    Entry fires when price re-enters the zone and shows a rejection candle,
    subject to ADX, RSI, EMA-trend, and session filters.
    """

    def __init__(self, symbol: Symbol, config: dict) -> None:
        super().__init__(symbol, config)

        # Zone formation parameters
        self.min_impulse_atr_mult: float = config.get("min_impulse_atr_mult", 2.5)
        self.zone_lookback_bars: int = config.get("zone_lookback_bars", 5)
        self.zone_max_age_bars: int = config.get("zone_max_age_bars", 100)
        self.max_active_zones: int = config.get("max_active_zones", 3)

        # Entry / confirmation parameters
        self.zone_touch_tolerance_atr: float = config.get("zone_touch_tolerance_atr", 0.3)
        self.min_rejection_ratio: float = config.get("min_rejection_ratio", 0.55)
        self.adx_min_threshold: float = config.get("adx_min_threshold", 18)
        self.rsi_overbought: float = config.get("rsi_overbought", 75)
        self.rsi_oversold: float = config.get("rsi_oversold", 25)
        self.ema_trend_period: int = config.get("ema_trend_period", 50)
        self.long_only: bool = config.get("long_only", False)
        self.session_hours: Optional[List[int]] = config.get("session_hours", None)

        self.cooldown_bars: int = config.get("cooldown_bars", 10)
        self._bars_since_signal: int = self.cooldown_bars  # Allow first trade immediately

        # --- Explicit mutable state (Carmack: visible at call site) ---
        # Both lists are ONLY mutated inside on_bar(). Nothing else touches them.
        self._demand_zones: List[ZoneDict] = []
        self._supply_zones: List[ZoneDict] = []

    def get_name(self) -> str:
        return "supply_demand"

    # ------------------------------------------------------------------
    # Internal helpers — state management (not pure, but scoped tightly)
    # ------------------------------------------------------------------

    def _add_zone(self, zone: ZoneDict) -> None:
        """Insert zone into the matching list, evicting the oldest when full.

        Uses a while-loop so the list is correctly trimmed even if it has been
        externally seeded to a size above max_active_zones (e.g. in tests).
        """
        target = self._demand_zones if zone["direction"] == "demand" else self._supply_zones

        # Ensure oldest entries are first so pop(0) always removes the stalest
        target.sort(key=lambda z: z["formed_at_bar"])

        # Trim until there is room for one more entry
        while len(target) >= self.max_active_zones:
            evicted = target.pop(0)
            self.logger.debug(
                "Zone evicted (list full)",
                direction=evicted["direction"],
                high=evicted["high"],
                low=evicted["low"],
            )

        target.append(zone)
        self.logger.info(
            "New S&D zone formed",
            direction=zone["direction"],
            high=f"{zone['high']:.2f}",
            low=f"{zone['low']:.2f}",
        )


    def _age_and_expire_zones(self, current_close: float) -> None:
        """Increment zone ages and remove expired or consumed zones.

        Mutations are explicit here — both lists are reassigned in one place.
        """
        def _keep(zone: ZoneDict) -> bool:
            zone["age_bars"] += 1
            if zone["age_bars"] > self.zone_max_age_bars:
                self.logger.info(
                    "Zone expired (age limit)",
                    direction=zone["direction"],
                    high=f"{zone['high']:.2f}",
                    low=f"{zone['low']:.2f}",
                    age=zone["age_bars"],
                )
                return False
            if _zone_consumed(current_close, zone["high"], zone["low"], zone["direction"]):
                self.logger.info(
                    "Zone consumed (price closed through)",
                    direction=zone["direction"],
                    high=f"{zone['high']:.2f}",
                    low=f"{zone['low']:.2f}",
                )
                return False
            return True

        self._demand_zones = [z for z in self._demand_zones if _keep(z)]
        self._supply_zones = [z for z in self._supply_zones if _keep(z)]

    # ------------------------------------------------------------------
    # Session helper (consistent with other strategies)
    # ------------------------------------------------------------------

    def _in_session(self, bars: pd.DataFrame) -> bool:
        """Return True when the current bar falls within an allowed session hour."""
        if self.session_hours is None:
            return True

        hour = self._get_bar_hour(bars)
        if hour is None:
            return True  # Cannot determine — do not block

        return hour in self.session_hours

    # ------------------------------------------------------------------
    # Main signal loop
    # ------------------------------------------------------------------

    def on_bar(self, bars: pd.DataFrame) -> Optional[Signal]:
        if not self.is_enabled():
            return None

        # O(1) tail slice — bound memory regardless of history depth (Jeff Dean)
        bars = bars.tail(500)

        min_bars = self.zone_lookback_bars + 30
        if len(bars) < min_bars:
            self._log_no_signal("Insufficient data")
            return None

        # ── Cooldown gate ──────────────────────────────────────────────
        self._bars_since_signal += 1
        if self._bars_since_signal < self.cooldown_bars:
            self._log_no_signal(
                f"Cooldown: {self._bars_since_signal}/{self.cooldown_bars} bars"
            )
            return None

        # ── Session filter ─────────────────────────────────────────────
        if not self._in_session(bars):
            self._log_no_signal(f"Outside session hours")
            return None

        # ── Indicators (computed once, shared across all phases) ───────
        atr = Indicators.atr(bars, period=14)
        rsi = Indicators.rsi(bars, period=14)
        adx = Indicators.adx(bars, period=14)
        ema_trend = Indicators.ema(bars, period=self.ema_trend_period)

        current_open = float(bars["open"].iloc[-1])
        current_high = float(bars["high"].iloc[-1])
        current_low = float(bars["low"].iloc[-1])
        current_close = float(bars["close"].iloc[-1])
        current_atr = float(atr.iloc[-1])
        current_rsi = float(rsi.iloc[-1])
        current_adx = float(adx.iloc[-1])
        current_ema = float(ema_trend.iloc[-1])

        if any(np.isnan(v) for v in [current_atr, current_rsi, current_adx, current_ema]):
            self._log_no_signal("Indicator calculation failed")
            return None

        if current_atr <= 0:
            self._log_no_signal("ATR is zero — cannot scale zone boundaries")
            return None

        # ── Regime (ML override or default TREND) ─────────────────────
        regime = self.ml_regime if self.ml_regime is not None else MarketRegime.TREND

        # ── Phase 1: Detect impulse on the just-completed bar and form zone ─
        # Check bar-2 (the bar BEFORE the current one) so the current bar can
        # act as the first potential retest — avoids same-bar entry after break.
        if len(bars) >= self.zone_lookback_bars + 2:
            impulse_iloc = len(bars) - 2  # Previous bar (0-indexed into bars DataFrame)
            prev_open = float(bars["open"].iloc[-2])
            prev_close = float(bars["close"].iloc[-2])
            prev_atr = float(atr.iloc[-2]) if not np.isnan(atr.iloc[-2]) else current_atr

            impulse_dir = _detect_impulse(prev_open, prev_close, prev_atr, self.min_impulse_atr_mult)

            if impulse_dir is not None:
                zone = _build_zone(bars, impulse_iloc, impulse_dir, self.zone_lookback_bars)
                if zone is not None:
                    self._add_zone(zone)

        # ── Phase 2: Age zones and purge expired / consumed ones ───────
        # Run AFTER zone formation so a brand-new zone at age 0 is checked
        # on the very next bar (age becomes 1 after this call).
        self._age_and_expire_zones(current_close)

        # ── Phase 3: Check if price is inside any active zone ──────────
        tolerance = self.zone_touch_tolerance_atr * current_atr

        triggered_zone: Optional[ZoneDict] = None
        triggered_side: Optional[OrderSide] = None

        for zone in self._demand_zones:
            # Do not enter on the same bar the zone was just formed (age == 0)
            if zone["age_bars"] < 1:
                continue
            if _price_in_zone(current_high, current_low, zone["high"], zone["low"], tolerance):
                triggered_zone = zone
                triggered_side = OrderSide.BUY
                break  # First valid demand zone wins

        if triggered_zone is None:
            for zone in self._supply_zones:
                if zone["age_bars"] < 1:
                    continue
                if _price_in_zone(current_high, current_low, zone["high"], zone["low"], tolerance):
                    triggered_zone = zone
                    triggered_side = OrderSide.SELL
                    break  # First valid supply zone wins

        if triggered_zone is None:
            self._log_no_signal("Price not in any active S&D zone")
            return None

        # Long-only guard
        if triggered_side == OrderSide.SELL and self.long_only:
            self._log_no_signal("Long-only mode — supply zone entry skipped")
            return None

        zone_direction = triggered_zone["direction"]

        # ── Phase 4: Confirm rejection candle quality ──────────────────
        rejection = _rejection_ratio(
            current_open, current_high, current_low, current_close, zone_direction
        )

        if rejection < self.min_rejection_ratio:
            self._log_no_signal(
                f"Weak rejection at {zone_direction} zone: "
                f"{rejection:.2f} < {self.min_rejection_ratio}"
            )
            return None

        # ── Phase 5: Confirmation filters ─────────────────────────────

        # Filter: ADX must show trend is active
        if current_adx < self.adx_min_threshold:
            self._log_no_signal(
                f"ADX too low: {current_adx:.1f} < {self.adx_min_threshold}"
            )
            return None

        # Filter: RSI not extreme (avoid chasing exhausted moves)
        if triggered_side == OrderSide.BUY and current_rsi > self.rsi_overbought:
            self._log_no_signal(f"RSI overbought at demand zone: {current_rsi:.1f}")
            return None

        if triggered_side == OrderSide.SELL and current_rsi < self.rsi_oversold:
            self._log_no_signal(f"RSI oversold at supply zone: {current_rsi:.1f}")
            return None

        # Filter: EMA trend alignment — trade WITH the higher-TF trend
        if triggered_side == OrderSide.BUY and current_close < current_ema:
            self._log_no_signal(
                f"EMA({self.ema_trend_period}) bearish: "
                f"close {current_close:.2f} < EMA {current_ema:.2f}"
            )
            return None

        if triggered_side == OrderSide.SELL and current_close > current_ema:
            self._log_no_signal(
                f"EMA({self.ema_trend_period}) bullish: "
                f"close {current_close:.2f} > EMA {current_ema:.2f}"
            )
            return None

        # ── Phase 6: Signal strength and emission ──────────────────────
        # Strength formula: rejection quality drives the signal score.
        # ADX momentum adds a bonus so stronger trends score higher.
        prev_adx = float(adx.iloc[-2]) if not np.isnan(adx.iloc[-2]) else current_adx
        adx_rising = current_adx > prev_adx

        adx_norm = min((current_adx - self.adx_min_threshold) / 50.0, 1.0)
        adx_bonus = 0.10 if adx_rising else 0.0
        strength = min(0.40 + rejection * 0.35 + adx_norm * 0.15 + adx_bonus, 1.0)

        # Consume the zone — it has been traded (prevents repeated entries)
        if zone_direction == "demand":
            self._demand_zones = [z for z in self._demand_zones if z is not triggered_zone]
        else:
            self._supply_zones = [z for z in self._supply_zones if z is not triggered_zone]

        self._bars_since_signal = 0

        return self._create_signal(
            side=triggered_side,
            strength=strength,
            regime=regime,
            entry_price=current_close,
            metadata={
                # Zone diagnostics (Jeff Dean: instrument everything)
                "zone_direction": zone_direction,
                "zone_high": round(triggered_zone["high"], 2),
                "zone_low": round(triggered_zone["low"], 2),
                "zone_age_bars": triggered_zone["age_bars"],
                # Confirmation quality
                "rejection_ratio": round(rejection, 3),
                "atr": round(current_atr, 4),
                "adx": round(current_adx, 1),
                "adx_rising": adx_rising,
                "rsi": round(current_rsi, 1),
                # Active zone counts for debugging
                "active_demand_zones": len(self._demand_zones),
                "active_supply_zones": len(self._supply_zones),
            },
        )
