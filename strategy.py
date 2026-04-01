from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, Optional


@dataclass
class Tick:
    btc_price: float
    yes_price: float
    no_price: float
    hour_open: float
    ts: datetime


class MomentumIndicator:
    def __init__(self, window: int = 6):
        self.window = max(3, window)
        self.prices: Deque[float] = deque(maxlen=self.window)

    def update(self, value: float) -> float:
        self.prices.append(value)
        if len(self.prices) < 3:
            return 0.5
        delta = self.prices[-1] - self.prices[0]
        abs_sum = sum(
            abs(self.prices[i] - self.prices[i - 1])
            for i in range(1, len(self.prices))
        ) or 1e-9
        directional_efficiency = abs(delta) / abs_sum
        slope = delta / max(abs(self.prices[0]), 1e-9)
        raw = 0.5 + max(-0.5, min(0.5, directional_efficiency * 2.0 * slope))
        return max(0.0, min(1.0, raw))


def minutes_left_in_hour(ts: datetime | None = None) -> int:
    ts = ts or datetime.now(timezone.utc)
    return 60 - ts.minute


def pct_change(new_value: float, old_value: float) -> float:
    if old_value == 0:
        return 0.0
    return (new_value - old_value) / old_value


def candle_body_size(bar: dict) -> float:
    return abs(bar["close"] - bar["open"])


def get_recent_bars(bars: list[dict], lookback: int) -> list[dict]:
    if len(bars) < lookback:
        return bars[:]
    return bars[-lookback:]


def passes_time_left_sweet_spot_filter(
    cfg: Dict,
    now: datetime | None = None,
) -> tuple[bool, str]:
    ladder_cfg = cfg["strategy"]["ladder_filters"]

    if not ladder_cfg["enable_time_left_sweet_spot_filter"]:
        return True, "time_left_filter_disabled"

    time_cfg = ladder_cfg["time_left_sweet_spot"]
    minutes_left = minutes_left_in_hour(now)

    passed = (
        time_cfg["min_minutes_left_for_entry"]
        <= minutes_left
        <= time_cfg["max_minutes_left_for_entry"]
    )

    if passed:
        return True, f"time_left_ok:{minutes_left}"
    return False, f"time_left_out_of_range:{minutes_left}"


def passes_move_exhaustion_filter(
    spot_bars: list[dict],
    side: str,
    cfg: Dict,
) -> tuple[bool, str]:
    ladder_cfg = cfg["strategy"]["ladder_filters"]

    if not ladder_cfg["enable_move_exhaustion_filter"]:
        return True, "move_exhaustion_disabled"

    exhaustion_cfg = ladder_cfg["move_exhaustion"]
    recent = get_recent_bars(spot_bars, exhaustion_cfg["lookback_bars"])

    if len(recent) < exhaustion_cfg["lookback_bars"]:
        return True, "move_exhaustion_insufficient_bars"

    bodies = [candle_body_size(bar) for bar in recent]

    shrinking_bodies = True
    if exhaustion_cfg["require_shrinking_bodies"]:
        shrinking_bodies = all(
            bodies[i] <= bodies[i - 1] for i in range(1, len(bodies))
        )

    new_extremes = 0
    if side == "BUY_UP":
        running_high = recent[0]["high"]
        for bar in recent[1:]:
            if bar["high"] > running_high:
                new_extremes += 1
                running_high = bar["high"]
    else:
        running_low = recent[0]["low"]
        for bar in recent[1:]:
            if bar["low"] < running_low:
                new_extremes += 1
                running_low = bar["low"]

    no_fresh_extremes = new_extremes <= exhaustion_cfg["max_new_extremes"]
    passed = shrinking_bodies and no_fresh_extremes

    if passed:
        return True, f"move_exhaustion_ok:bodies={bodies},new_extremes={new_extremes}"
    return False, f"move_exhaustion_fail:bodies={bodies},new_extremes={new_extremes}"


def passes_failed_continuation_filter(
    spot_bars: list[dict],
    side: str,
    cfg: Dict,
) -> tuple[bool, str]:
    ladder_cfg = cfg["strategy"]["ladder_filters"]

    if not ladder_cfg["enable_failed_continuation_filter"]:
        return True, "failed_continuation_disabled"

    fc_cfg = ladder_cfg["failed_continuation"]
    lookback = fc_cfg["lookback_bars"]
    retest_bars = fc_cfg["retest_bars"]
    min_rejection_pct = fc_cfg["min_rejection_pct"]

    recent = get_recent_bars(spot_bars, lookback)

    if len(recent) < lookback:
        return True, "failed_continuation_insufficient_bars"

    pre_retest = recent[:-retest_bars]
    retest_window = recent[-retest_bars:]

    if not pre_retest or not retest_window:
        return True, "failed_continuation_insufficient_structure"

    if side == "BUY_UP":
        old_extreme = max(bar["high"] for bar in pre_retest)
        retest_high = max(bar["high"] for bar in retest_window)
        final_close = retest_window[-1]["close"]

        retest_happened = retest_high >= old_extreme * (1 - min_rejection_pct)
        failed_hold = final_close < old_extreme
        passed = retest_happened and failed_hold

        if passed:
            return True, (
                f"failed_up_continuation_ok:"
                f"old_extreme={old_extreme},"
                f"retest_high={retest_high},"
                f"final_close={final_close}"
            )
        return False, (
            f"failed_up_continuation_fail:"
            f"old_extreme={old_extreme},"
            f"retest_high={retest_high},"
            f"final_close={final_close}"
        )

    old_extreme = min(bar["low"] for bar in pre_retest)
    retest_low = min(bar["low"] for bar in retest_window)
    final_close = retest_window[-1]["close"]

    retest_happened = retest_low <= old_extreme * (1 + min_rejection_pct)
    failed_hold = final_close > old_extreme
    passed = retest_happened and failed_hold

    if passed:
        return True, (
            f"failed_down_continuation_ok:"
            f"old_extreme={old_extreme},"
            f"retest_low={retest_low},"
            f"final_close={final_close}"
        )
    return False, (
        f"failed_down_continuation_fail:"
        f"old_extreme={old_extreme},"
        f"retest_low={retest_low},"
        f"final_close={final_close}"
    )


def passes_spot_market_disconnect_filter(
    side: str,
    spot_price_now: float,
    spot_reference_price: float,
    market_price_now: float,
    market_reference_price: float,
    cfg: Dict,
) -> tuple[bool, str]:
    ladder_cfg = cfg["strategy"]["ladder_filters"]

    if not ladder_cfg["enable_spot_market_disconnect_filter"]:
        return True, "spot_market_disconnect_disabled"

    disconnect_cfg = ladder_cfg["spot_market_disconnect"]

    spot_move_pct = pct_change(spot_price_now, spot_reference_price)
    market_move_pct = pct_change(market_price_now, market_reference_price)

    if side == "BUY_UP":
        spot_ok = spot_move_pct <= disconnect_cfg["max_spot_continuation_pct"]
        premium = market_move_pct - spot_move_pct
        premium_ok = premium >= disconnect_cfg["min_premium_pct"]
    else:
        spot_ok = abs(min(spot_move_pct, 0.0)) <= disconnect_cfg["max_spot_continuation_pct"]
        premium = abs(market_move_pct) - abs(spot_move_pct)
        premium_ok = premium >= disconnect_cfg["min_premium_pct"]

    passed = spot_ok and premium_ok

    if passed:
        return True, (
            f"disconnect_ok:"
            f"spot_move={spot_move_pct:.5f},"
            f"market_move={market_move_pct:.5f},"
            f"premium={premium:.5f}"
        )
    return False, (
        f"disconnect_fail:"
        f"spot_move={spot_move_pct:.5f},"
        f"market_move={market_move_pct:.5f},"
        f"premium={premium:.5f}"
    )


def compute_edge_cents(move: float, entry_price: float, side: str) -> float:
    # Heuristic from your current workflow; calibrate from your own logs.
    # The point is consistency across shadow/live logging, not pretending this is theoretical fair value.
    move_abs = abs(move)
    if side == "BUY_UP":
        implied = min(0.99, max(0.01, 0.50 + move_abs / 200.0))
        return max(0.0, (implied - entry_price) * 100)
    implied = min(0.99, max(0.01, 0.50 + move_abs / 200.0))
    return max(0.0, (implied - entry_price) * 100)


def choose_setup(entry_price: float, edge_cents: float) -> str:
    if edge_cents >= 45 and 0.25 <= entry_price <= 0.60:
        return "CORE"
    if edge_cents >= 35:
        return "CORE"
    return "PASS"


def trade_tier(entry_price: float, edge_cents: float) -> str:
    if edge_cents >= 45:
        return "LARGE"
    if edge_cents >= 35:
        return "MEDIUM"
    return "SMALL"


def kelly_cash_size(
    bankroll_usd: float,
    tier: str,
    kelly_fraction: float,
    min_order_usd: float,
    max_order_usd: float,
) -> float:
    tier_mult = {"LARGE": 1.0, "MEDIUM": 0.67, "SMALL": 0.4}[tier]
    suggested = bankroll_usd * kelly_fraction * tier_mult
    return round(max(min_order_usd, min(max_order_usd, suggested)), 2)


def evaluate_signal(
    tick: Tick,
    *,
    direction: str,
    momentum_score: float,
    cfg: Dict,
    reversal_ok: bool,
) -> Dict:
    entry_price = tick.yes_price if direction == "BUY_UP" else tick.no_price
    edge_cents = compute_edge_cents(
        tick.btc_price - tick.hour_open,
        entry_price,
        direction,
    )
    setup = choose_setup(entry_price, edge_cents)
    tier = trade_tier(entry_price, edge_cents)

    blocked_by: Optional[str] = None

    if setup == "PASS":
        blocked_by = "FAILED_MIN_EDGE"
    elif not (
        cfg["strategy"]["min_entry_price"]
        <= entry_price
        <= cfg["strategy"]["max_entry_price"]
    ):
        blocked_by = "FAILED_ENTRY_RANGE"
    elif abs(tick.btc_price - tick.hour_open) < cfg["strategy"]["min_move_abs"]:
        blocked_by = "FAILED_MOVE_STRENGTH"
    elif momentum_score < cfg["strategy"]["momentum_min_score"]:
        blocked_by = "FAILED_MOMENTUM"
    elif cfg["strategy"]["require_reversal_confirmation"] and not reversal_ok:
        blocked_by = "FAILED_REVERSAL_CONFIRMATION"
    elif entry_price < cfg["strategy"]["small_trade_block_min_price"]:
        blocked_by = "FAILED_SMALL_TRADE_BLOCK"

    mins_left = minutes_left_in_hour(tick.ts)
    if (
        mins_left < cfg["strategy"]["no_trade_min_minutes_left"]
        or mins_left > cfg["strategy"]["no_trade_max_minutes_left"]
    ):
        blocked_by = blocked_by or "FAILED_NO_TRADE_WINDOW"

    return {
        "side": direction,
        "setup": setup,
        "tier": tier,
        "btc_price": tick.btc_price,
        "hour_open": tick.hour_open,
        "move": tick.btc_price - tick.hour_open,
        "yes_price": tick.yes_price,
        "no_price": tick.no_price,
        "entry_price": entry_price,
        "edge_cents": round(edge_cents, 2),
        "momentum_score": round(momentum_score, 3),
        "blocked": blocked_by is not None,
        "blocked_by": blocked_by,
    }
