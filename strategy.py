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
        abs_sum = sum(abs(self.prices[i] - self.prices[i - 1]) for i in range(1, len(self.prices))) or 1e-9
        directional_efficiency = abs(delta) / abs_sum
        slope = (delta / max(abs(self.prices[0]), 1e-9))
        raw = 0.5 + max(-0.5, min(0.5, directional_efficiency * 2.0 * slope))
        return max(0.0, min(1.0, raw))


def minutes_left_in_hour(ts: datetime | None = None) -> int:
    ts = ts or datetime.now(timezone.utc)
    return 59 - ts.minute


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


def kelly_cash_size(bankroll_usd: float, tier: str, kelly_fraction: float, min_order_usd: float, max_order_usd: float) -> float:
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
    edge_cents = compute_edge_cents(tick.btc_price - tick.hour_open, entry_price, direction)
    setup = choose_setup(entry_price, edge_cents)
    tier = trade_tier(entry_price, edge_cents)

    blocked_by = None
    if setup == "PASS":
        blocked_by = "FAILED_MIN_EDGE"
    elif not (cfg["strategy"]["min_entry_price"] <= entry_price <= cfg["strategy"]["max_entry_price"]):
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
    if mins_left < cfg["strategy"]["no_trade_min_minutes_left"] or mins_left > cfg["strategy"]["no_trade_max_minutes_left"]:
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

def pct_change(new_value: float, old_value: float) -> float:
    if old_value == 0:
        return 0.0
    return (new_value - old_value) / old_value


def candle_body_size(bar: dict) -> float:
    return abs(bar["close"] - bar["open"])


def get_minutes_left_in_hour(now=None) -> int:
    if now is None:
        now = datetime.now(timezone.utc)
    return 60 - now.minute


def get_recent_bars(bars: list[dict], lookback: int) -> list[dict]:
    if len(bars) < lookback:
        return bars[:]
    return bars[-lookback:]
