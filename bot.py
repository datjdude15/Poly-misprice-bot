import argparse
import csv
import math
import os
import re
import time
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


def get_telegram_token(cfg: dict) -> str:
    return str(cfg.get("telegram_bot_token", "")).strip()


def get_telegram_chat_id(cfg: dict) -> str:
    return str(cfg.get("telegram_chat_id", "")).strip()


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

    deltas = [price_history[i] - price_history[i - 1] for i in range(1, len(price_history))]
    avg_step = sum(deltas) / len(deltas) if deltas else 0.0
    accel = deltas[-1] - deltas[0] if len(deltas) >= 2 else 0.0

    raw = 50.0 + (move * 0.9) + (avg_step * 8.0) + (accel * 2.0)
    return clamp(raw, 0.0, 100.0)


def fetch_btc_spot_from_coinbase() -> float:
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


def classify_grade(signal: str, edge_cents: float, prob_up: float, prob_down: float, momentum_strength: float) -> str:
    directional_prob = prob_up if signal == "BUY UP" else prob_down

    if edge_cents >= 45 and directional_prob >= 0.62:
        return "TIER1"
    if edge_cents >= 25 and directional_prob >= 0.56:
        return "TIER2"
    return "WATCH"


def build_signal(prob_up, yes_price, no_price, btc_price, hour_open, momentum_strength, minutes_left, cfg):
    strat = get_strategy(cfg)

    min_edge_cents = float(strat.get("min_edge_cents", 25))
    min_move_abs = float(strat.get("min_move_abs", 1.8))
    min_entry_price = float(strat.get("min_entry_price", 0.01))
    max_entry_price = float(strat.get("max_entry_price", 0.85))
    no_trade_min_minutes_left = float(strat.get("no_trade_min_minutes_left", 1))
    no_trade_max_minutes_left = float(strat.get("no_trade_max_minutes_left", 59))
    momentum_min_score = float(strat.get("momentum_min_score", 45))
    strong_edge_override_cents = float(strat.get("strong_edge_override_cents", 40))
    high_momentum_override_score = float(strat.get("high_momentum_override_score", 85))
    min_prob_trade = float(strat.get("min_prob_trade", 0.56))

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

    if minutes_left < no_trade_min_minutes_left or minutes_left > no_trade_max_minutes_left:
        result["reason"] = "FAILED_NO_TRADE_WINDOW"
        return result

    best_edge = max(edge_up_c, edge_down_c)

    high_momentum_override = (
        momentum_strength >= high_momentum_override_score
        or momentum_strength <= (100 - high_momentum_override_score)
    )

    if abs_move < min_move_abs and best_edge < strong_edge_override_cents and not high_momentum_override:
        result["reason"] = "FAILED_MIN_MOVE"
        return result

    bullish_ok = momentum_strength >= momentum_min_score
    bearish_ok = momentum_strength <= (100 - momentum_min_score)

    up_prob_ok = prob_up >= min_prob_trade
    down_prob_ok = prob_down >= min_prob_trade

    up_ok = edge_up_c >= min_edge_cents and bullish_ok and up_prob_ok and min_entry_price <= yes_price <= max_entry_price
    down_ok = edge_down_c >= min_edge_cents and bearish_ok and down_prob_ok and min_entry_price <= no_price <= max_entry_price

    if up_ok and edge_up_c >= edge_down_c:
        result["signal"] = "BUY UP"
        result["reason"] = "EDGE_UP_CONFIRMED"
        return result

    if down_ok and edge_down_c > edge_up_c:
        result["signal"] = "BUY DOWN"
        result["reason"] = "EDGE_DOWN_CONFIRMED"
        return result

    result["reason"] = "FAILED_ENTRY_FILTER"
    return result


def load_open_trades(csv_path: str):
    if not os.path.exists(csv_path):
        return []

    rows = []
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            normalized = {field: row.get(field, "") for field in OPEN_TRADE_FIELDS}
            if normalized["resolved"] == "":
                normalized["resolved"] = "false"
            rows.append(normalized)
    return rows


def write_open_trades(csv_path: str, rows):
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OPEN_TRADE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            normalized = {field: row.get(field, "") for field in OPEN_TRADE_FIELDS}
            writer.writerow(normalized)


def append_open_trade(cfg: dict, row: dict):
    csv_path = cfg.get("logging", {}).get("open_trade_log_path", "open_trades.csv")
    rows = load_open_trades(csv_path)

    normalized = {field: row.get(field, "") for field in OPEN_TRADE_FIELDS}
    if normalized["resolved"] == "":
        normalized["resolved"] = "false"

    rows.append(normalized)
    write_open_trades(csv_path, rows)


def send_telegram(cfg: dict, text: str):
    token = get_telegram_token(cfg)
    chat_id = get_telegram_chat_id(cfg)
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)


def maybe_emit_trade(signal_data, market_state, yes_price, no_price, btc_price, cfg):
    signal = signal_data["signal"]
    if signal == "NO TRADE":
        return

    edge_cents = signal_data["edge_up_c"] if signal == "BUY UP" else signal_data["edge_down_c"]
    entry_price = yes_price if signal == "BUY UP" else no_price
    grade = classify_grade(
        signal,
        edge_cents,
        signal_data["prob_up"],
        signal_data["prob_down"],
        signal_data["momentum_strength"],
    )

    append_open_trade(
        cfg,
        {
            "trade_id": f"{market_state.slug}|{int(time.time())}",
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "slug": market_state.slug,
            "action": signal,
            "grade": grade,
            "entry_price": f"{entry_price:.3f}",
            "edge_cents": f"{edge_cents:.2f}",
            "prob_up": f"{signal_data['prob_up']:.4f}",
            "prob_down": f"{signal_data['prob_down']:.4f}",
            "momentum": f"{signal_data['momentum_strength']:.1f}",
            "move": f"{signal_data['abs_move']:.2f}",
            "hour_open_btc": f"{market_state.hour_open_btc:.2f}",
            "btc_entry": f"{btc_price:.2f}",
            "resolved": "false",
        },
    )

    send_telegram(
        cfg,
        f"🚨 TRADE SIGNAL\n"
        f"Action: {signal}\n"
        f"Grade: {grade}\n"
        f"Slug: {market_state.slug}\n"
        f"Entry: {entry_price:.3f}\n"
        f"Edge: {edge_cents:.2f}c\n"
        f"Prob Up: {signal_data['prob_up']}\n"
        f"Prob Down: {signal_data['prob_down']}\n"
        f"Momentum: {signal_data['momentum_strength']}",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    cfg = load_config(args.config)
    poll_seconds = get_poll_seconds(cfg)

    price_history = []

    while True:
        try:
            market_state = resolve_current_market_state()
            btc_price = fetch_btc_spot_from_coinbase()
            yes_price = fetch_public_clob_midpoint(market_state.yes_token_id)
            no_price = fetch_public_clob_midpoint(market_state.no_token_id)

            minutes_left = calc_minutes_left()

            price_history.append(btc_price)
            if len(price_history) > 12:
                price_history = price_history[-12:]

            momentum_strength = calc_momentum_strength(price_history)

            prob_up = probability_up(
                btc_price,
                market_state.hour_open_btc,
                minutes_left,
                momentum_strength,
                cfg,
            )

            signal_data = build_signal(
                prob_up,
                yes_price,
                no_price,
                btc_price,
                market_state.hour_open_btc,
                momentum_strength,
                minutes_left,
                cfg,
            )

            maybe_emit_trade(
                signal_data,
                market_state,
                yes_price,
                no_price,
                btc_price,
                cfg,
            )

        except Exception as e:
            log(f"Loop error: {e}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
