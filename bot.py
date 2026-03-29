import argparse
import csv
import math
import os
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yaml

from market_resolver import resolve_current_market_state, fetch_public_clob_midpoint


UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")

OPEN_TRADE_FIELDS = [
    "trade_id",
    "timestamp_utc",
    "slug",
    "action",
    "grade",
    "entry_price",
    "edge_cents",
    "prob_up",
    "prob_down",
    "momentum",
    "move",
    "hour_open_btc",
    "btc_entry",
    "resolved",
    "result",
    "btc_settle",
    "settled_at_utc",
]

CLOSED_TRADE_FIELDS = [
    "trade_id",
    "timestamp_utc",
    "slug",
    "action",
    "grade",
    "entry_price",
    "edge_cents",
    "prob_up",
    "prob_down",
    "momentum",
    "move",
    "hour_open_btc",
    "btc_entry",
    "resolved",
    "result",
    "btc_settle",
    "settled_at_utc",
]


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


def get_telegram_token(cfg: dict) -> str:
    return str(cfg.get("telegram_bot_token", "")).strip()


def get_telegram_chat_id(cfg: dict) -> str:
    return str(cfg.get("telegram_chat_id", "")).strip()


def get_open_trade_log_path(cfg: dict) -> str:
    return str(get_logging_cfg(cfg).get("open_trade_log_path", "open_trades.csv")).strip()


def get_closed_trade_log_path(cfg: dict) -> str:
    return str(get_logging_cfg(cfg).get("closed_trade_log_path", "closed_trades.csv")).strip()


def get_summary_log_path(cfg: dict) -> str:
    return str(get_logging_cfg(cfg).get("summary_log_path", "performance_summary.csv")).strip()


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


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

    prob = sigmoid(raw)
    return clamp(prob, 0.01, 0.99)


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


def classify_grade(signal: str, edge_cents: float, prob_up: float, prob_down: float) -> str:
    directional_prob = prob_up if signal == "BUY UP" else prob_down

    if edge_cents >= 45 and directional_prob >= 0.62:
        return "TIER1"
    if edge_cents >= 25 and directional_prob >= 0.56:
        return "TIER2"
    return "WATCH"


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

    min_edge_cents = float(strat.get("min_edge_cents", 25))
    min_move_abs = float(strat.get("min_move_abs", 3))
    min_entry_price = float(strat.get("min_entry_price", 0.03))
    max_entry_price = float(strat.get("max_entry_price", 0.85))
    small_trade_block_min_price = float(strat.get("small_trade_block_min_price", 0.01))
    no_trade_min_minutes_left = float(strat.get("no_trade_min_minutes_left", 1))
    no_trade_max_minutes_left = float(strat.get("no_trade_max_minutes_left", 59))
    momentum_min_score = float(strat.get("momentum_min_score", 45))
    strong_edge_override_cents = float(strat.get("strong_edge_override_cents", 40))
    min_prob_trade = float(strat.get("min_prob_trade", 0.56))
    high_momentum_override_score = float(strat.get("high_momentum_override_score", 85))

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
        "debug": {
            "min_edge_cents": min_edge_cents,
            "min_move_abs": min_move_abs,
            "min_entry_price": min_entry_price,
            "max_entry_price": max_entry_price,
            "small_trade_block_min_price": small_trade_block_min_price,
            "no_trade_min_minutes_left": no_trade_min_minutes_left,
            "no_trade_max_minutes_left": no_trade_max_minutes_left,
            "momentum_min_score": momentum_min_score,
            "strong_edge_override_cents": strong_edge_override_cents,
            "min_prob_trade": min_prob_trade,
            "high_momentum_override_score": high_momentum_override_score,
        },
    }

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

    if edge_up_c >= min_edge_cents and edge_down_c >= min_edge_cents:
        if not up_prob_ok and not down_prob_ok:
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


def should_send_alert(
    key: str,
    cooldowns: dict[str, float],
    cooldown_seconds: int,
    now_ts: float,
) -> bool:
    last_ts = cooldowns.get(key, 0.0)
    if now_ts - last_ts >= cooldown_seconds:
        cooldowns[key] = now_ts
        return True
    return False


def maybe_emit_trade(
    signal_data: dict,
    market_state,
    yes_price: float,
    no_price: float,
    cfg: dict,
    alert_cooldowns: dict[str, float],
):
    signal = signal_data["signal"]
    mode = get_mode(cfg).upper()
    strat = get_strategy(cfg)

    trade_alerts_enabled = bool(strat.get("telegram_trade_alerts", True))
    near_miss_alerts_enabled = bool(strat.get("telegram_near_miss_alerts", True))
    alert_cooldown_seconds = int(strat.get("telegram_alert_cooldown_seconds", 300))
    near_miss_ratio = float(strat.get("near_miss_ratio", 0.8))
    min_edge_cents = float(strat.get("min_edge_cents", 25))

    now_ts = time.time()

    if signal == "NO TRADE":
        log(
            f"[PASS] slug={market_state.slug} "
            f"reason={signal_data['reason']} "
            f"move={signal_data['abs_move']} "
            f"prob_up={signal_data['prob_up']:.3f} "
            f"prob_down={signal_data['prob_down']:.3f} "
            f"edge_up={signal_data['edge_up_c']}c "
            f"edge_down={signal_data['edge_down_c']}c "
            f"mom={signal_data['momentum_strength']} "
            f"thresholds={signal_data['debug']}"
        )

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

        if near_miss_alerts_enabled and best_side is not None:
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
                    f"Prob Up: {signal_data['prob_up']:.3f}\n"
                    f"Prob Down: {signal_data['prob_down']:.3f}\n"
                    f"Momentum: {signal_data['momentum_strength']}\n"
                    f"BTC: {market_state.hour_open_btc:.2f} open reference"
                )
                send_telegram(cfg, msg)
        return

    edge_cents = signal_data["edge_up_c"] if signal == "BUY UP" else signal_data["edge_down_c"]
    entry_price = yes_price if signal == "BUY UP" else no_price
    tier, size = calc_order_size(signal, edge_cents, cfg)
    grade = classify_grade(
        signal,
        edge_cents,
        float(signal_data["prob_up"]),
        float(signal_data["prob_down"]),
    )

    log(
        f"[TRADE] TRADE BRANCH REACHED "
        f"mode={mode} "
        f"slug={market_state.slug} "
        f"action={signal} "
        f"grade={grade} "
        f"entry={entry_price:.3f} "
        f"edge={edge_cents}c "
        f"tier={tier} "
        f"size=${size} "
        f"move={signal_data['abs_move']} "
        f"prob_up={signal_data['prob_up']:.3f} "
        f"prob_down={signal_data['prob_down']:.3f} "
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

    log("🚀 BOT STARTING")
    log(f"Loading config from {args.config}")
    log(f"Mode -> {get_mode(cfg).upper()}")
    log(f"Polling every {poll_seconds} seconds")
    log(f"[DEBUG] Active strategy -> {get_strategy(cfg)}")

    if args.test_alert:
        ok = send_telegram(cfg, "✅ PolySniperBot test alert successful. Telegram is connected.")
        if ok:
            log("Test complete")
        else:
            log("Test failed")
        return

    startup_alerts = bool(get_strategy(cfg).get("telegram_startup_alerts", False))
    if startup_alerts:
        send_telegram(
            cfg,
            f"🟢 PolySniperBot online\nMode: {get_mode(cfg).upper()}\nPoll: {poll_seconds}s",
        )

    price_history: list[float] = []
    current_slug = None
    alert_cooldowns: dict[str, float] = {}
    last_heartbeat_ts = 0.0

    while True:
        try:
            now_ts = time.time()
            if now_ts - last_heartbeat_ts >= 60:
                log("Heartbeat... bot running")
                last_heartbeat_ts = now_ts

            market_state = resolve_current_market_state()
            current_slug = market_state.slug

            btc_price = fetch_btc_spot_from_coinbase()
            yes_price = fetch_public_clob_midpoint(market_state.yes_token_id)
            no_price = fetch_public_clob_midpoint(market_state.no_token_id)
            minutes_left = calc_minutes_left()

            price_history.append(btc_price)
            if len(price_history) > 12:
                price_history = price_history[-12:]

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
                cfg,
                alert_cooldowns,
            )

        except Exception as e:
            slug_text = current_slug if current_slug else "unknown"
            log(f"Loop error: {e} | slug={slug_text}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
