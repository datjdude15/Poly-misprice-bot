from trade_executor import place_market_buy
import argparse
import csv
import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yaml

from market_resolver import resolve_current_market_state, fetch_public_clob_midpoint
from strategy import passes_ladder_filters
from trade_logic import (
    classify_trade_strength,
    get_dynamic_sl_percent,
    get_strong_grace_period_seconds,
    is_strong_trade_row,
    should_block_same_slug_reentry,
    should_force_time_pressure_exit,
    compute_ladder_exit_price
)


UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")

DEFAULT_OPEN_TRADES_FILE = "/app/data/open_trades.csv"
DEFAULT_CLOSED_TRADES_FILE = "/app/data/closed_trades.csv"
DEFAULT_SUMMARY_FILE = "/app/data/performance_summary.json"

OPEN_FIELDS = [
    "trade_id",
    "created_utc",
    "entry_utc",
    "slug",
    "action",
    "grade",
    "tier",
    "size_usd",
    "entry_price",
    "edge_cents",
    "prob_up",
    "prob_down",
    "momentum",
    "move",
    "market_regime"
    "btc_entry",
    "hour_open_btc",
    "tp_price",
    "sl_price",
    "max_hold_seconds",
    "highest_midpoint_seen",
    "ladder_stop_price",
    "exit_mode",
    "ladder_eligible",
    "time_exit_deadline_utc",
    "market_hour_end_et",
    "scalp_status",
    "scalp_exit_reason",
    "scalp_exit_price",
    "scalp_exit_utc",
    "scalp_pnl_pct",
    "settle_status",
    "settle_result",
    "settle_btc",
    "settle_utc",
]

CLOSED_FIELDS = OPEN_FIELDS.copy()


def log(msg: str):
    now = datetime.now(UTC).strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def get_mode(cfg: dict) -> str:
    return str(cfg.get("mode", "paper")).lower()


def get_poll_seconds(cfg: dict) -> int:
    if "poll_seconds" in cfg:
        return int(cfg["poll_seconds"])
    return int(cfg.get("app", {}).get("poll_interval_seconds", 5))


def get_strategy(cfg: dict) -> dict:
    return cfg.get("strategy", {})


def get_risk(cfg: dict) -> dict:
    return cfg.get("risk", {})


def get_logging_cfg(cfg: dict) -> dict:
    return cfg.get("logging", {})


def get_max_spread_pct(cfg: dict) -> float:
    return float(cfg.get("max_spread_pct", 0.12))


def get_min_book_depth(cfg: dict) -> float:
    return float(cfg.get("min_book_depth", 100))


def get_open_trades_file(cfg: dict) -> str:
    return str(get_logging_cfg(cfg).get("open_trade_log_path", DEFAULT_OPEN_TRADES_FILE))


def get_closed_trades_file(cfg: dict) -> str:
    return str(get_logging_cfg(cfg).get("closed_trade_log_path", DEFAULT_CLOSED_TRADES_FILE))


def get_summary_file(cfg: dict) -> str:
    return str(get_logging_cfg(cfg).get("summary_log_path", DEFAULT_SUMMARY_FILE))


def get_telegram_token(cfg: dict) -> str:
    return str(cfg.get("telegram_bot_token", "")).strip()


def get_telegram_chat_id(cfg: dict) -> str:
    return str(cfg.get("telegram_chat_id", "")).strip()


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def ensure_csv(path: str, fieldnames: list[str]):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()


def read_csv_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(path: str, fieldnames: list[str], rows: list[dict]):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def append_csv_row(path: str, fieldnames: list[str], row: dict):
    ensure_csv(path, fieldnames)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writerow(row)


def probability_up(
    btc_price: float,
    hour_open: float,
    minutes_left: float,
    momentum_strength: float,
    cfg: dict,
) -> float:
    model = cfg.get("model", {})

    dist_scale = float(model.get("distance_scale_usd", 35.0))
    momentum_weight = float(model.get("momentum_weight", 0.35))
    time_weight = float(model.get("time_weight", 0.75))

    diff = btc_price - hour_open
    normalized_diff = diff / dist_scale

    time_factor = 1.0 + time_weight * (1.0 - (minutes_left / 60.0))
    raw = normalized_diff * time_factor

    mom_centered = (momentum_strength - 50.0) / 25.0
    raw += mom_centered * momentum_weight

    return clamp(sigmoid(raw), 0.01, 0.99)


def calc_minutes_left() -> float:
    now_et = datetime.now(ET)
    return 60.0 - now_et.minute - (now_et.second / 60.0)


def calc_momentum_strength(price_history: list[float]) -> float:
    if len(price_history) < 3:
        return 50.0

    first = price_history[0]
    last = price_history[-1]
    move = last - first

    deltas = []
    for i in range(1, len(price_history)):
        deltas.append(price_history[i] - price_history[i - 1])

    avg_step = sum(deltas) / len(deltas) if deltas else 0.0
    accel = deltas[-1] - deltas[0] if len(deltas) >= 2 else 0.0

    raw = 50.0 + (move * 0.9) + (avg_step * 8.0) + (accel * 2.0)
    return clamp(raw, 0.0, 100.0)


def fetch_btc_spot_from_coinbase() -> float:
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


def fetch_order_book_snapshot(token_id: str) -> dict:
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()

    bids = data.get("bids", []) or []
    asks = data.get("asks", []) or []

    best_bid = float(bids[0]["price"]) if bids else 0.0
    best_ask = float(asks[0]["price"]) if asks else 0.0
    best_bid_size = float(bids[0]["size"]) if bids else 0.0
    best_ask_size = float(asks[0]["size"]) if asks else 0.0

    mid_price = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
    spread = best_ask - best_bid if best_bid > 0 and best_ask > 0 else 999.0
    spread_pct = spread / max(mid_price, 0.01)
    
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_size": best_bid_size,
        "best_ask_size": best_ask_size,
        "mid_price": mid_price,
        "spread": spread,
        "spread_pct": spread_pct,
    }


def build_pseudo_spot_bars(price_history: list[float], chunk_size: int = 3) -> list[dict]:
    """
    Temporary fallback until real OHLC bars are implemented.
    Groups recent BTC spot samples into pseudo-bars.

    With 5-second polling:
    - chunk_size=3 gives ~15-second bars
    - chunk_size=6 gives ~30-second bars
    """
    if not price_history:
        return []

    bars = []
    for i in range(0, len(price_history), chunk_size):
        chunk = price_history[i:i + chunk_size]
        if not chunk:
            continue
        bars.append({
            "open": chunk[0],
            "high": max(chunk),
            "low": min(chunk),
            "close": chunk[-1],
        })
    return bars


def bar_range(bar: dict) -> float:
    return max(0.0, float(bar["high"]) - float(bar["low"]))


def bar_overlap_pct(bar1: dict, bar2: dict) -> float:
    """
    Returns overlap as % of the smaller bar's range.
    """
    high1, low1 = float(bar1["high"]), float(bar1["low"])
    high2, low2 = float(bar2["high"]), float(bar2["low"])

    overlap = max(0.0, min(high1, high2) - max(low1, low2))
    min_range = max(min(bar_range(bar1), bar_range(bar2)), 1e-9)
    return overlap / min_range


def classify_market_regime(
    price_history: list[float],
    pseudo_bars: list[dict],
    btc_price: float,
    hour_open_btc: float,
    minutes_left: float,
) -> str:
    """
    Regimes:
    - trend
    - chop
    - late_acceleration
    - spike_fade
    - neutral
    """
    if len(price_history) < 6 or len(pseudo_bars) < 3:
        return "neutral"

    abs_move = abs(btc_price - hour_open_btc)

    # ---- trend detection ----
    higher_highs = 0
    higher_lows = 0
    lower_highs = 0
    lower_lows = 0

    for i in range(1, len(pseudo_bars)):
        prev_bar = pseudo_bars[i - 1]
        curr_bar = pseudo_bars[i]

        if curr_bar["high"] > prev_bar["high"]:
            higher_highs += 1
        if curr_bar["low"] > prev_bar["low"]:
            higher_lows += 1
        if curr_bar["high"] < prev_bar["high"]:
            lower_highs += 1
        if curr_bar["low"] < prev_bar["low"]:
            lower_lows += 1

    directional_trend = (
        (higher_highs >= 2 and higher_lows >= 2)
        or (lower_highs >= 2 and lower_lows >= 2)
    )

    # ---- chop detection ----
    open_crosses = 0
    for i in range(1, len(price_history)):
        prev_diff = price_history[i - 1] - hour_open_btc
        curr_diff = price_history[i] - hour_open_btc
        if prev_diff == 0:
            continue
        if (prev_diff > 0 and curr_diff < 0) or (prev_diff < 0 and curr_diff > 0):
            open_crosses += 1

    overlaps = []
    for i in range(1, len(pseudo_bars)):
        overlaps.append(bar_overlap_pct(pseudo_bars[i - 1], pseudo_bars[i]))
    avg_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0

    chop_like = open_crosses >= 2 and avg_overlap >= 0.45

    # ---- late acceleration detection ----
    early_prices = price_history[: max(3, len(price_history) // 2)]
    early_move = abs(early_prices[-1] - early_prices[0]) if len(early_prices) >= 2 else 0.0

    recent_bars = pseudo_bars[-2:]
    recent_ranges = [bar_range(bar) for bar in recent_bars]
    recent_expansion = all(r > 0 for r in recent_ranges) and (
        recent_ranges[-1] >= recent_ranges[0] * 1.15
    )

    late_accel_like = (
        minutes_left <= 25
        and early_move < max(8.0, abs_move * 0.45)
        and abs_move >= 12.0
        and recent_expansion
    )

    # ---- spike fade detection ----
    first_half = pseudo_bars[: max(2, len(pseudo_bars) // 2)]
    second_half = pseudo_bars[max(1, len(pseudo_bars) // 2):]

    first_half_ranges = [bar_range(b) for b in first_half]
    second_half_ranges = [bar_range(b) for b in second_half]

    first_half_avg = sum(first_half_ranges) / len(first_half_ranges) if first_half_ranges else 0.0
    second_half_avg = sum(second_half_ranges) / len(second_half_ranges) if second_half_ranges else 0.0

    recent_bodies = [abs(b["close"] - b["open"]) for b in pseudo_bars[-3:]]
    shrinking_bodies = len(recent_bodies) >= 3 and (
        recent_bodies[-1] <= recent_bodies[-2] <= recent_bodies[-3]
    )

    spike_fade_like = (
        abs_move >= 18.0
        and first_half_avg > 0
        and second_half_avg < first_half_avg * 0.85
        and shrinking_bodies
    )

    # ---- classify in priority order ----
    if late_accel_like:
        return "late_acceleration"

    if spike_fade_like:
        return "spike_fade"

    if chop_like:
        return "chop"

    if directional_trend:
        return "trend"

    return "neutral"


def build_signal(
    prob_up: float,
    yes_price: float,
    no_price: float,
    btc_price: float,
    hour_open: float,
    momentum_strength: float,
    minutes_left: float,
    cfg: dict,
) -> dict:
    strat = get_strategy(cfg)

    min_edge_cents = float(strat.get("min_edge_cents", 22))
    min_move_abs = float(strat.get("min_move_abs", 1.2))
    min_entry_price = float(strat.get("min_entry_price", 0.015))
    max_entry_price = float(strat.get("max_entry_price", 0.80))
    small_trade_block_min_price = float(strat.get("small_trade_block_min_price", 0.01))
    no_trade_min_minutes_left = float(strat.get("no_trade_min_minutes_left", 2))
    no_trade_max_minutes_left = float(strat.get("no_trade_max_minutes_left", 58))
    momentum_min_score = float(strat.get("momentum_min_score", 42))
    strong_edge_override_cents = float(strat.get("strong_edge_override_cents", 38))
    high_momentum_override_score = float(strat.get("high_momentum_override_score", 85))
    min_prob_trade = float(strat.get("min_prob_trade", 0.55))

    prob_down = 1.0 - prob_up
    edge_up_c = (prob_up - yes_price) * 100.0
    edge_down_c = (prob_down - no_price) * 100.0
    abs_move = abs(btc_price - hour_open)

    result = {
        "signal": "NO TRADE",
        "reason": "",
        "edge_up_c": round(edge_up_c, 2),
        "edge_down_c": round(edge_down_c, 2),
        "prob_up": round(prob_up, 4),
        "prob_down": round(prob_down, 4),
        "momentum_strength": round(momentum_strength, 1),
        "abs_move": round(abs_move, 2),
    }
    
    if abs_move < 8:
        result["reason"] = "FAILED_MIN_REAL_MOVE"
        return result
    
    if minutes_left < no_trade_min_minutes_left or minutes_left > no_trade_max_minutes_left:
        result["reason"] = "FAILED_NO_TRADE_WINDOW"
        return result

    if yes_price is None or no_price is None:
        result["reason"] = "FAILED_MISSING_PRICE"
        return result

    if yes_price < small_trade_block_min_price and no_price < small_trade_block_min_price:
        result["reason"] = "FAILED_SMALL_TRADE_BLOCK"
        return result

    best_edge = max(edge_up_c, edge_down_c)

    high_momentum_override = (
        momentum_strength >= high_momentum_override_score
        or momentum_strength <= (100.0 - high_momentum_override_score)
    )

    if abs_move < min_move_abs and best_edge < strong_edge_override_cents and not high_momentum_override:
        result["reason"] = "FAILED_MIN_MOVE"
        return result

    bullish_ok = momentum_strength >= momentum_min_score
    bearish_ok = momentum_strength <= (100.0 - momentum_min_score)

    up_prob_ok = prob_up >= min_prob_trade
    down_prob_ok = prob_down >= min_prob_trade

    up_ok = (
        edge_up_c >= min_edge_cents
        and min_entry_price <= yes_price <= max_entry_price
        and bullish_ok
        and up_prob_ok
    )

    down_ok = (
        edge_down_c >= min_edge_cents
        and min_entry_price <= no_price <= max_entry_price
        and bearish_ok
        and down_prob_ok
    )

    if up_ok and edge_up_c >= edge_down_c:
        result["signal"] = "BUY UP"
        result["reason"] = "EDGE_UP_CONFIRMED"
        return result

    if down_ok and edge_down_c > edge_up_c:
        result["signal"] = "BUY DOWN"
        result["reason"] = "EDGE_DOWN_CONFIRMED"
        return result

    if edge_up_c >= min_edge_cents and not bullish_ok:
        result["reason"] = "FAILED_MOMENTUM_CONFIRMATION"
        return result

    if edge_down_c >= min_edge_cents and not bearish_ok:
        result["reason"] = "FAILED_MOMENTUM_CONFIRMATION"
        return result

    if edge_up_c >= min_edge_cents and not up_prob_ok:
        result["reason"] = "FAILED_PROBABILITY_FILTER"
        return result

    if edge_down_c >= min_edge_cents and not down_prob_ok:
        result["reason"] = "FAILED_PROBABILITY_FILTER"
        return result

    if edge_up_c < min_edge_cents and edge_down_c < min_edge_cents:
        result["reason"] = "FAILED_MIN_EDGE"
        return result

    result["reason"] = "FAILED_ENTRY_FILTER"
    return result


def calc_order_size(signal: str, edge_cents: float, cfg: dict) -> tuple[str, float]:
    risk = get_risk(cfg)

    bankroll = float(risk.get("bankroll_usd", 1000))
    min_order = float(risk.get("min_order_usd", 15))
    max_order = float(risk.get("max_order_usd", 60))

    if edge_cents >= 50:
        tier = "LARGE"
        size = min(max_order, max(min_order, bankroll * 0.06))
    elif edge_cents >= 40:
        tier = "MEDIUM"
        size = min(max_order, max(min_order, bankroll * 0.04))
    else:
        tier = "SMALL"
        size = min(max_order, max(min_order, bankroll * 0.025))

    return tier, round(size, 2)


def classify_grade(signal: str, edge_cents: float, prob_up: float, prob_down: float) -> str:
    directional_prob = prob_up if signal == "BUY UP" else prob_down
    if edge_cents >= 45 and directional_prob >= 0.62:
        return "TIER1"
    if edge_cents >= 25 and directional_prob >= 0.55:
        return "TIER2"
    return "WATCH"


def send_telegram(cfg: dict, text: str) -> bool:
    token = get_telegram_token(cfg)
    chat_id = get_telegram_chat_id(cfg)

    if not token or not chat_id:
        log("[ALERT] Telegram not configured")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}

    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        log("[ALERT] Telegram sent")
        return True
    except Exception as e:
        log(f"[ALERT] Telegram failed: {e}")
        return False


def should_send_alert(key: str, cooldowns: dict[str, float], cooldown_seconds: int, now_ts: float) -> bool:
    last_ts = cooldowns.get(key, 0.0)
    if now_ts - last_ts >= cooldown_seconds:
        cooldowns[key] = now_ts
        return True
    return False


def get_market_hour_end_et() -> datetime:
    now_et = datetime.now(ET)
    return now_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def get_tp_pct(cfg: dict) -> float:
    return float(get_strategy(cfg).get("tp_percent", 0.20))


def get_sl_pct(cfg: dict) -> float:
    return float(get_strategy(cfg).get("sl_percent", 0.12))


def get_max_hold_seconds(cfg: dict) -> int:
    return int(get_strategy(cfg).get("max_hold_seconds", 480))


def write_summary(cfg: dict):
    closed_rows = read_csv_rows(get_closed_trades_file(cfg))

    total = len(closed_rows)
    scalp_wins = sum(1 for r in closed_rows if r.get("scalp_status") == "WIN")
    scalp_losses = sum(1 for r in closed_rows if r.get("scalp_status") == "LOSS")
    settle_wins = sum(1 for r in closed_rows if r.get("settle_result") == "WIN")
    settle_losses = sum(1 for r in closed_rows if r.get("settle_result") == "LOSS")

    summary = {
        "total_closed_trades": total,
        "scalp_wins": scalp_wins,
        "scalp_losses": scalp_losses,
        "scalp_win_rate": round((scalp_wins / total) if total else 0.0, 4),
        "settle_wins": settle_wins,
        "settle_losses": settle_losses,
        "settle_win_rate": round((settle_wins / total) if total else 0.0, 4),
        "last_updated_utc": datetime.now(UTC).isoformat(),
    }

    with open(get_summary_file(cfg), "w") as f:
        json.dump(summary, f, indent=2)


def trade_exists_for_slug_action(cfg: dict, slug: str, action: str) -> bool:
    rows = read_csv_rows(get_open_trades_file(cfg))
    for row in rows:
        if row.get("slug") == slug and row.get("action") == action:
            return True
    return False


def closed_trade_exists(cfg: dict, trade_id: str) -> bool:
    rows = read_csv_rows(get_closed_trades_file(cfg))
    return any(r.get("trade_id") == trade_id for r in rows)


def create_open_trade_row(
    cfg: dict,
    trade_id: str,
    market_state,
    signal: str,
    grade: str,
    tier: str,
    size: float,
    entry_price: float,
    edge_cents: float,
    prob_up: float,
    prob_down: float,
    momentum: float,
    move: float,
    btc_price: float,
    ladder_eligible: bool,
    market_regime: str,
) -> dict:
    tp_pct = get_tp_pct(cfg)
    minutes_left = calc_minutes_left()
    sl_pct = get_dynamic_sl_percent(
        edge_cents=edge_cents,
        move_abs=move,
        momentum=momentum,
        entry_price=entry_price,
        minutes_left=minutes_left,
        cfg=cfg,
    )

    max_hold_seconds = get_max_hold_seconds(cfg)
    tp_price = round(entry_price * (1.0 + tp_pct), 4)
    sl_price = round(max(entry_price * (1.0 - sl_pct), 0.001), 4)
    
    
    now_utc = datetime.now(UTC)
    hour_end_et = get_market_hour_end_et()

    return {
        "trade_id": trade_id,
        "created_utc": now_utc.isoformat(),
        "entry_utc": now_utc.isoformat(),
        "slug": market_state.slug,
        "action": signal,
        "grade": grade,
        "tier": tier,
        "size_usd": round(size, 2),
        "entry_price": round(entry_price, 4),
        "edge_cents": round(edge_cents, 2),
        "prob_up": round(prob_up, 4),
        "prob_down": round(prob_down, 4),
        "momentum": round(momentum, 1),
        "move": round(move, 2),
        "market_regime": market_regime,
        "btc_entry": round(btc_price, 2),
        "hour_open_btc": round(float(market_state.hour_open_btc), 2),
        "tp_price": tp_price,
        "sl_price": sl_price,
        "max_hold_seconds": max_hold_seconds,
        "ladder_eligible": str(ladder_eligible),
        "time_exit_deadline_utc": (now_utc + timedelta(seconds=max_hold_seconds)).isoformat(),
        "market_hour_end_et": hour_end_et.isoformat(),
        "scalp_status": "OPEN",
        "scalp_exit_reason": "",
        "scalp_exit_price": "",
        "scalp_exit_utc": "",
        "scalp_pnl_pct": "",
        "settle_status": "OPEN",
        "settle_result": "",
        "settle_btc": "",
        "settle_utc": "",
    }


def close_trade_record(cfg: dict, trade_id: str, updated_row: dict):
    if closed_trade_exists(cfg, trade_id):
        return

    open_rows = read_csv_rows(get_open_trades_file(cfg))
    remaining_rows = [r for r in open_rows if r.get("trade_id") != trade_id]
    write_csv_rows(get_open_trades_file(cfg), OPEN_FIELDS, remaining_rows)
    append_csv_row(get_closed_trades_file(cfg), CLOSED_FIELDS, updated_row)
    write_summary(cfg)


def dedupe_open_rows(rows):
    """Keep only one OPEN row per unique trade_id."""
    seen = set()
    clean = []
    for row in rows:
        tid = row.get("trade_id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        clean.append(row)
    return clean


def safe_close_trade_record(read_csv_rows, write_csv_rows, append_csv_row,
                            open_path, closed_path, open_fields, closed_fields,
                            trade_id, updated_row):
    """
    Anti-spam close:
    - removes ALL matching open rows
    - checks closed file before appending
    """
    open_rows = read_csv_rows(open_path)
    closed_rows = read_csv_rows(closed_path)

    for row in closed_rows:
        if row.get("trade_id") == trade_id:
            return False

    remaining = [r for r in open_rows if r.get("trade_id") != trade_id]
    remaining = dedupe_open_rows(remaining)

    write_csv_rows(open_path, open_fields, remaining)
    append_csv_row(closed_path, closed_fields, updated_row)
    return True


def monitor_open_trades(cfg: dict):
    open_path = get_open_trades_file(cfg)
    closed_path = get_closed_trades_file(cfg)

    open_rows = dedupe_open_rows(read_csv_rows(open_path))
    write_csv_rows(open_path, OPEN_FIELDS, open_rows)

    if not open_rows:
        return

    now_utc = datetime.now(UTC)
    now_et = datetime.now(ET)

    changed = False
    rows_to_keep = []

    for row in open_rows:
        try:
            if row.get("scalp_status") in ("WIN", "LOSS") and row.get("settle_status") == "CLOSED":
                continue

            action = row["action"]
            slug = row["slug"]
            trade_id = row["trade_id"]

            entry_price = float(row["entry_price"])
            tp_price = float(row["tp_price"])
            sl_price = float(row["sl_price"])
            hour_open_btc = float(row["hour_open_btc"])

            market_hour_end_et = datetime.fromisoformat(row["market_hour_end_et"])
            deadline_utc = datetime.fromisoformat(row["time_exit_deadline_utc"])

            midpoint = None
            if now_et < market_hour_end_et:
                try:
                    state = resolve_current_market_state()
                    if state.slug == slug:
                        token = state.yes_token_id if action == "BUY UP" else state.no_token_id
                        midpoint = fetch_public_clob_midpoint(token)
                except Exception as e:
                    midpoint = None
            

            if row.get("scalp_status", "OPEN") == "OPEN":
                exit_reason = None
                exit_price = None

                entry_utc_dt = datetime.fromisoformat(row["entry_utc"])
                seconds_open = (now_utc - entry_utc_dt).total_seconds()

                strong_trade = is_strong_trade_row(row, cfg)
                grace_seconds = get_strong_grace_period_seconds(cfg)

                minutes_left = max(0.0, (market_hour_end_et - now_et).total_seconds() / 60.0)

                ladder_stop_price = None
                ladder_eligible = str(row.get("ladder_eligible", "False")).lower() == "true"

                if midpoint is not None and ladder_eligible:
                    ladder_stop_price, ladder_updates = compute_ladder_exit_price(
                        entry_price=entry_price,
                        midpoint=midpoint,
                        minutes_left=minutes_left,
                        row=row,
                        cfg=cfg,
                    )
                    for k, v in ladder_updates.items():
                        row[k] = v

                active_stop_price = sl_price
                row["exit_mode"] = "STATIC"

                if ladder_stop_price is not None:
                    active_stop_price = max(sl_price, ladder_stop_price)
                    row["exit_mode"] = "LADDER"

                if midpoint is not None:
                    if midpoint >= tp_price:
                        if row["exit_mode"] == "LADDER":
                            pass
                        else:
                            exit_reason = "TP"
                            exit_price = midpoint
                    elif midpoint <= active_stop_price:
                        if strong_trade and seconds_open < grace_seconds and active_stop_price == sl_price:
                            pass
                        else:
                            exit_reason = "LADDER_STOP" if row["exit_mode"] == "LADDER" else "SL"
                            exit_price = midpoint

                if exit_reason is None:
                    force_exit, force_reason = should_force_time_pressure_exit(
                        midpoint=midpoint,
                        entry_price=entry_price,
                        tp_price=tp_price,
                        minutes_left=minutes_left,
                        action=action,
                        hour_open_btc=hour_open_btc,
                        current_btc=fetch_btc_spot_from_coinbase(),
                        cfg=cfg,
                    )
                    if force_exit and midpoint is not None:
                        exit_reason = force_reason
                        exit_price = midpoint

                if exit_reason is None and now_utc >= deadline_utc and midpoint is not None:
                    exit_reason = "TIME_EXIT"
                    exit_price = midpoint

                if exit_reason and exit_price is not None:
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
                    scalp_result = "WIN" if pnl_pct > 0 else "LOSS"

                    row["scalp_status"] = scalp_result
                    row["scalp_exit_reason"] = exit_reason
                    row["scalp_exit_price"] = round(exit_price, 4)
                    row["scalp_exit_utc"] = now_utc.isoformat()
                    row["scalp_pnl_pct"] = round(pnl_pct, 2)

                    icon = "✅" if scalp_result == "WIN" else "❌"
                    send_telegram(
                        cfg,
                        f"{icon} TRADE CLOSED\n"
                        f"Mode: {get_mode(cfg).upper()}\n"
                        f"Action: {action}\n"
                        f"Grade: {row['grade']}\n"
                        f"Slug: {slug}\n"
                        f"Entry: {entry_price:.3f}\n"
                        f"Exit: {float(row['scalp_exit_price']):.3f}\n"
                        f"Reason: {exit_reason}\n"
                        f"Exit Mode: {row.get('exit_mode', 'STATIC')}\n"
                        f"PnL: {float(row['scalp_pnl_pct']):.2f}%"
                    )
                    changed = True

            if row.get("settle_status", "OPEN") == "OPEN" and now_et >= market_hour_end_et + timedelta(seconds=10):
                settle_btc = fetch_btc_spot_from_coinbase()
                went_up = settle_btc > hour_open_btc
                                
                if action == "BUY UP":
                    settle_result = "WIN" if went_up else "LOSS"
                else:
                    settle_result = "WIN" if not went_up else "LOSS"

                row["settle_status"] = "CLOSED"
                row["settle_result"] = settle_result
                row["settle_btc"] = round(settle_btc, 2)
                row["settle_utc"] = now_utc.isoformat()
                changed = True

            if row.get("scalp_status") in ("WIN", "LOSS") and row.get("settle_status") == "CLOSED":
                safe_close_trade_record(
                    read_csv_rows,
                    write_csv_rows,
                    append_csv_row,
                    open_path,
                    closed_path,
                    OPEN_FIELDS,
                    CLOSED_FIELDS,
                    trade_id,
                    row,
                )
                changed = True
                continue

            rows_to_keep.append(row)

        except Exception:
            rows_to_keep.append(row)

    if changed:
        write_csv_rows(open_path, OPEN_FIELDS, dedupe_open_rows(rows_to_keep))


def maybe_emit_trade(
    signal_data: dict,
    market_state,
    yes_price: float,
    no_price: float,
    btc_price: float,
    cfg: dict,
    alert_cooldowns: dict[str, float],
    recent_spot_bars: list[dict],
    market_regime: str,
):
    signal = signal_data["signal"]
    mode = get_mode(cfg).upper()
    strat = get_strategy(cfg)

    trade_alerts_enabled = bool(strat.get("telegram_trade_alerts", True))
    near_miss_alerts_enabled = bool(strat.get("telegram_near_miss_alerts", False))
    alert_cooldown_seconds = int(strat.get("telegram_alert_cooldown_seconds", 180))
    near_miss_ratio = float(strat.get("near_miss_ratio", 0.8))
    min_edge_cents = float(strat.get("min_edge_cents", 22))

    now_ts = time.time()

    if signal == "NO TRADE":
        log(
            f"[PASS] slug={market_state.slug} "
            f"reason={signal_data['reason']} "
            f"move={signal_data['abs_move']} "
            f"prob_up={signal_data['prob_up']:.4f} "
            f"prob_down={signal_data['prob_down']:.4f} "
            f"edge_up={signal_data['edge_up_c']}c "
            f"edge_down={signal_data['edge_down_c']}c "
            f"mom={signal_data['momentum_strength']}"
        )

        if not near_miss_alerts_enabled:
            return

        edge_up = float(signal_data["edge_up_c"])
        edge_down = float(signal_data["edge_down_c"])
        near_miss_cutoff = min_edge_cents * near_miss_ratio

        best_side = None
        best_edge = None

        if edge_up >= edge_down and edge_up >= near_miss_cutoff:
            best_side = "UP"
            best_edge = edge_up
        elif edge_down > edge_up and edge_down >= near_miss_cutoff:
            best_side = "DOWN"
            best_edge = edge_down

        if best_side is not None:
            alert_key = f"near_miss:{market_state.slug}:{best_side}:{signal_data['reason']}"
            if should_send_alert(alert_key, alert_cooldowns, alert_cooldown_seconds, now_ts):
                entry_price = yes_price if best_side == "UP" else no_price
                msg = (
                    f"⚠️ NEAR MISS ({best_side})\n"
                    f"Mode: {mode}\n"
                    f"Slug: {market_state.slug}\n"
                    f"Reason Blocked: {signal_data['reason']}\n"
                    f"Entry Price: {entry_price:.3f}\n"
                    f"Edge: {best_edge}c\n"
                    f"Prob Up: {signal_data['prob_up']:.4f}\n"
                    f"Prob Down: {signal_data['prob_down']:.4f}\n"
                    f"Momentum: {signal_data['momentum_strength']}\n"
                    f"BTC: {market_state.hour_open_btc:.2f} open reference"
                )
                send_telegram(cfg, msg)
        return

    if trade_exists_for_slug_action(cfg, market_state.slug, signal):
        log(f"[TRADE] skipped duplicate for slug={market_state.slug} action={signal}")
        return

    edge_cents = signal_data["edge_up_c"] if signal == "BUY UP" else signal_data["edge_down_c"]
    entry_price = yes_price if signal == "BUY UP" else no_price
    tier, size = calc_order_size(signal, edge_cents, cfg)
    grade = classify_grade(signal, edge_cents, float(signal_data["prob_up"]), float(signal_data["prob_down"]))

    ladder_side = signal.replace(" ", "_")
    spot_price_now = btc_price
    spot_reference_price = float(market_state.hour_open_btc)
    market_price_now = entry_price
    market_reference_price = 0.50  # temporary fallback anchor until setup-start price is tracked

    passed_ladder_filters, ladder_filter_reasons = passes_ladder_filters(
        side=ladder_side,
        spot_bars=recent_spot_bars,
        spot_price_now=spot_price_now,
        spot_reference_price=spot_reference_price,
        market_price_now=market_price_now,
        market_reference_price=market_reference_price,
        cfg=cfg,
        now=datetime.now(UTC),
    )

    ladder_eligible = bool(passed_ladder_filters)

    log(
        f"[LADDER FILTER CHECK] "
        f"slug={market_state.slug} "
        f"side={ladder_side} "
        f"passed={passed_ladder_filters} "
        f"ladder_eligible={ladder_eligible} "
        f"reasons={ladder_filter_reasons}"
    )

    if not ladder_eligible:
        log(
            f"[TRADE] normal-mode only "
            f"slug={market_state.slug} "
            f"action={signal} "
            f"reasons={ladder_filter_reasons}"
        )
    else:
        log(
            f"[TRADE] ladder-eligible "
            f"slug={market_state.slug} "
            f"action={signal} "
            f"reasons={ladder_filter_reasons}"
        )

    token_id = market_state.yes_token_id if signal == "BUY UP" else market_state.no_token_id
    book = fetch_order_book_snapshot(token_id)

    min_book_depth = get_min_book_depth(cfg)

    if book["best_bid_size"] < min_book_depth or book["best_ask_size"] < min_book_depth:
        log(
            f"[TRADE] blocked SIZE_FILTER "
            f"slug={market_state.slug} action={signal} "
            f"bid_size={book['best_bid_size']:.2f} "
            f"ask_size={book['best_ask_size']:.2f}"
        )
        return
    
    if grade == "WATCH":
        log(f"[TRADE] blocked WATCH grade for slug={market_state.slug}")
        return
    
    open_rows = read_csv_rows(get_open_trades_file(cfg))
    closed_rows = read_csv_rows(get_closed_trades_file(cfg)) 
   
    if should_block_same_slug_reentry(
        open_rows=open_rows,
        closed_rows=closed_rows,
        slug=market_state.slug,
        action=signal,
        prob_up=float(signal_data["prob_up"]),
        prob_down=float(signal_data["prob_down"]),
        move_abs=float(signal_data["abs_move"]),
        cfg=cfg,
    ):
        log(f"[TRADE] blocked same-slug reentry for slug={market_state.slug} action={signal}")
        return

   if get_mode(cfg).lower() == "live":
       token_id = (
           market_state.yes_token_id
           if signal == "BUY UP"
           else market_state.no_token_id
       )

       live_result = place_market_buy(
           token_id=token_id,
           price=entry_price,
           size_usd=size,
       )

       log(f"[LIVE ORDER] {live_result}")
    
    trade_id = f"{market_state.slug}-{signal}-{uuid.uuid4().hex[:8]}"
    trade_row = create_open_trade_row(
        cfg=cfg,
        trade_id=trade_id,
        market_state=market_state,
        signal=signal,
        grade=grade,
        tier=tier,
        size=size,
        entry_price=entry_price,
        edge_cents=edge_cents,
        prob_up=float(signal_data["prob_up"]),
        prob_down=float(signal_data["prob_down"]),
        momentum=float(signal_data["momentum_strength"]),
        move=float(signal_data["abs_move"]),
        btc_price=float(btc_price),
        ladder_eligible=ladder_eligible,
        market_regime=market_regime,
    )
    append_csv_row(get_open_trades_file(cfg), OPEN_FIELDS, trade_row)

    log(
        f"[TRADE] mode={mode} "
        f"slug={market_state.slug} "
        f"action={signal} "
        f"regime={market_regime} "
        f"ladder_eligible={ladder_eligible} "
        f"grade={grade} "
        f"entry={entry_price:.3f} "
        f"edge={edge_cents}c "
        f"tier={tier} "
        f"size=${size} "
        f"move={signal_data['abs_move']} "
        f"prob_up={signal_data['prob_up']:.4f} "
        f"prob_down={signal_data['prob_down']:.4f} "
        f"mom={signal_data['momentum_strength']}"
    )

    if trade_alerts_enabled:
        alert_key = f"trade:{market_state.slug}:{signal}"
        if should_send_alert(alert_key, alert_cooldowns, alert_cooldown_seconds, now_ts):
            msg = (
                f"🚨 TRADE SIGNAL\n"
                f"Mode: {mode}\n"
                f"Action: {signal}\n"
                f"Grade: {grade}\n"
                f"Slug: {market_state.slug}\n"
                f"Entry: {entry_price:.3f}\n"
                f"Edge: {edge_cents}c\n"
                f"Tier: {tier}\n"
                f"Size: ${size}\n"
                f"Move: {signal_data['abs_move']}\n"
                f"Prob Up: {signal_data['prob_up']:.4f}\n"
                f"Prob Down: {signal_data['prob_down']:.4f}\n"
                f"Momentum: {signal_data['momentum_strength']}"
            )
            send_telegram(cfg, msg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--test-alert", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    poll_seconds = get_poll_seconds(cfg)

    log("=== LAST 60 CLOSED TRADES ===")
    closed_rows = read_csv_rows(get_closed_trades_file(cfg))

    for row in closed_rows[-60:]:
        log(
            f"[CLOSED] {row.get('entry_utc')} | "
            f"{row.get('action')} | "
            f"{row.get('grade')} | "
            f"entry={row.get('entry_price')} | "
            f"exit={row.get('scalp_exit_price')} | "
            f"pnl={row.get('scalp_pnl_pct')}% | "
            f"reason={row.get('scalp_exit_reason')}"
        )

    log("=== END CLOSED TRADES ===")
    
    ensure_csv(get_open_trades_file(cfg), OPEN_FIELDS)
    ensure_csv(get_closed_trades_file(cfg), CLOSED_FIELDS)

    log("🚀 BOT STARTING")
    log(f"Loading config from {args.config}")
    log(f"Mode -> {get_mode(cfg).upper()}")
    log(f"Polling every {poll_seconds} seconds")

    if args.test_alert:
        ok = send_telegram(cfg, "✅ PolySniperBot test alert successful. Telegram is connected.")
        log("Test complete" if ok else "Test failed")
        return

    startup_alerts = bool(get_strategy(cfg).get("telegram_startup_alerts", False))
    if startup_alerts:
        send_telegram(
            cfg,
            f"🟢 PolySniperBot online\nMode: {get_mode(cfg).upper()}\nPoll: {poll_seconds}s",
        )

    price_history: list[float] = []
    current_slug = None
    current_hour_open_btc: float | None = None
    alert_cooldowns: dict[str, float] = {}
    last_heartbeat_ts = 0.0
    last_tracker_ts = 0.0

    while True:
        try:
            now_ts = time.time()

            if now_ts - last_heartbeat_ts >= 60:
                log("Heartbeat... bot running")
                last_heartbeat_ts = now_ts

            if now_ts - last_tracker_ts >= 5:
                monitor_open_trades(cfg)
                last_tracker_ts = now_ts

            market_state = resolve_current_market_state()
            btc_price = fetch_btc_spot_from_coinbase()

            if current_slug != market_state.slug:
                price_history = []
                current_slug = market_state.slug
                current_hour_open_btc = btc_price
                log(
                    f"[ANCHOR] New hour anchor set "
                    f"slug={market_state.slug} "
                    f"hour_open_btc={current_hour_open_btc:.2f}"
                )

            if current_hour_open_btc is None:
                current_hour_open_btc = btc_price
                log(
                    f"[ANCHOR] Fallback hour anchor set "
                    f"slug={market_state.slug} "
                    f"hour_open_btc={current_hour_open_btc:.2f}"
                )

            market_state.hour_open_btc = current_hour_open_btc

            yes_price = fetch_public_clob_midpoint(market_state.yes_token_id)
            no_price = fetch_public_clob_midpoint(market_state.no_token_id)
            minutes_left = calc_minutes_left()
            price_history.append(btc_price)
            if len(price_history) > 12:
                price_history = price_history[-12:]

            recent_spot_bars = build_pseudo_spot_bars(price_history, chunk_size=3)

            market_regime = classify_market_regime(
                price_history=price_history,
                pseudo_bars=recent_spot_bars,
                btc_price=btc_price,
                hour_open_btc=market_state.hour_open_btc,
                minutes_left=minutes_left,
            )
            
            momentum_strength = calc_momentum_strength(price_history)
            abs_move = abs(btc_price - market_state.hour_open_btc)

            prob_up = probability_up(
                btc_price=btc_price,
                hour_open=market_state.hour_open_btc,
                minutes_left=minutes_left,
                momentum_strength=momentum_strength,
                cfg=cfg,
            )

            log(
                f"[TICK] slug={market_state.slug} "
                f"btc={btc_price:.2f} "
                f"open={market_state.hour_open_btc:.2f} "
                f"move={abs_move:.2f} "
                f"yes={yes_price if yes_price is not None else 'None'} "
                f"no={no_price if no_price is not None else 'None'} "
                f"mom={momentum_strength:.1f} "
                f"mins_left={minutes_left:.1f}"
                f"regime={market_regime} "
            )

            signal_data = build_signal(
                prob_up=prob_up,
                yes_price=yes_price,
                no_price=no_price,
                btc_price=btc_price,
                hour_open=market_state.hour_open_btc,
                momentum_strength=momentum_strength,
                minutes_left=minutes_left,
                cfg=cfg,
            )

            maybe_emit_trade(
                signal_data,
                market_state,
                yes_price,
                no_price,
                btc_price,
                cfg,
                alert_cooldowns,
                recent_spot_bars,
                market_regime,
            )

        except Exception as e:
            slug_text = current_slug if current_slug else "unknown"
            log(f"Loop error: {e} | slug={slug_text}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
