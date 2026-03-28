import argparse
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yaml

from market_resolver import resolve_current_market_state, fetch_public_clob_midpoint


UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")


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


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def fetch_btc_spot_from_coinbase() -> float:
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


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


def probability_up(
    btc_price: float,
    hour_open: float,
    minutes_left: float,
    momentum_strength: float,
    cfg: dict,
) -> float:
    model = cfg.get("model", {})

    dist_scale = float(model.get("distance_scale_usd", 55.0))
    momentum_weight = float(model.get("momentum_weight", 0.25))
    time_weight = float(model.get("time_weight", 0.60))

    diff = btc_price - hour_open
    normalized_diff = diff / dist_scale

    time_factor = 1.0 + time_weight * (1.0 - (minutes_left / 60.0))
    raw = normalized_diff * time_factor

    mom_centered = (momentum_strength - 50.0) / 25.0
    raw += mom_centered * momentum_weight

    prob = sigmoid(raw)
    return clamp(prob, 0.01, 0.99)


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

    min_edge_cents = float(strat.get("min_edge_cents", 35))
    min_move_abs = float(strat.get("min_move_abs", 45))
    min_entry_price = float(strat.get("min_entry_price", 0.25))
    max_entry_price = float(strat.get("max_entry_price", 0.60))
    small_trade_block_min_price = float(strat.get("small_trade_block_min_price", 0.20))
    no_trade_min_minutes_left = float(strat.get("no_trade_min_minutes_left", 5))
    no_trade_max_minutes_left = float(strat.get("no_trade_max_minutes_left", 55))
    momentum_min_score = float(strat.get("momentum_min_score", 58))

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

    if yes_price is None or no_price is None:
        result["reason"] = "FAILED_MISSING_PRICE"
        return result

    if yes_price < small_trade_block_min_price and no_price < small_trade_block_min_price:
        result["reason"] = "FAILED_SMALL_TRADE_BLOCK"
        return result

    if abs_move < min_move_abs:
        result["reason"] = "FAILED_MIN_MOVE"
        return result

    bullish_ok = momentum_strength >= momentum_min_score
    bearish_ok = momentum_strength <= (100.0 - momentum_min_score)

    up_ok = (
        edge_up_c >= min_edge_cents
        and min_entry_price <= yes_price <= max_entry_price
        and bullish_ok
    )

    down_ok = (
        edge_down_c >= min_edge_cents
        and min_entry_price <= no_price <= max_entry_price
        and bearish_ok
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


def send_telegram_alert(cfg: dict, message: str) -> bool:
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()

    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"[ALERT_ERROR] Telegram send failed: {e}")
        return False


def maybe_emit_trade(
    signal_data: dict,
    market_state,
    yes_price: float,
    no_price: float,
    cfg: dict,
    alert_cache: dict,
):
    signal = signal_data["signal"]

    if signal == "NO TRADE":
        log(
            f"[PASS] slug={market_state.slug} "
            f"reason={signal_data['reason']} "
            f"prob_up={signal_data['prob_up']:.3f} "
            f"prob_down={signal_data['prob_down']:.3f} "
            f"edge_up={signal_data['edge_up_c']}c "
            f"edge_down={signal_data['edge_down_c']}c "
            f"mom={signal_data['momentum_strength']}"
        )
        return

    edge_cents = signal_data["edge_up_c"] if signal == "BUY UP" else signal_data["edge_down_c"]
    entry_price = yes_price if signal == "BUY UP" else no_price
    tier, size = calc_order_size(signal, edge_cents, cfg)
    mode = get_mode(cfg).upper()

    log(
        f"[TRADE] mode={mode} "
        f"slug={market_state.slug} "
        f"action={signal} "
        f"entry={entry_price:.3f} "
        f"edge={edge_cents}c "
        f"tier={tier} "
        f"size=${size} "
        f"prob_up={signal_data['prob_up']:.3f} "
        f"prob_down={signal_data['prob_down']:.3f} "
        f"mom={signal_data['momentum_strength']}"
    )

    alert_key = f"{market_state.slug}|{signal}"
    now_ts = time.time()
    cooldown_seconds = int(cfg.get("alerts", {}).get("cooldown_seconds", 300))
    last_sent = alert_cache.get(alert_key, 0)

    if now_ts - last_sent < cooldown_seconds:
        return

    alert_text = (
        f"PolySniper Signal\n"
        f"Mode: {mode}\n"
        f"Slug: {market_state.slug}\n"
        f"Action: {signal}\n"
        f"Entry: {entry_price:.3f}\n"
        f"Edge: {edge_cents}c\n"
        f"Tier: {tier}\n"
        f"Size: ${size}\n"
        f"Prob Up: {signal_data['prob_up']:.3f}\n"
        f"Prob Down: {signal_data['prob_down']:.3f}\n"
        f"Momentum: {signal_data['momentum_strength']}\n"
        f"Hour Open: {market_state.hour_open_btc:.2f}"
    )

    sent = send_telegram_alert(cfg, alert_text)
    if sent:
        alert_cache[alert_key] = now_ts
        log(f"[ALERT] Telegram alert sent for {alert_key}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    cfg = load_config(args.config)
    poll_seconds = get_poll_seconds(cfg)

    log("🚀 BOT STARTING")
    log(f"Loading config from {args.config}")
    log(f"Mode -> {get_mode(cfg).upper()}")
    log(f"Polling every {poll_seconds} seconds")

    price_history: list[float] = []
    current_slug = None
    alert_cache = {}

    while True:
        try:
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
                signal_data=signal_data,
                market_state=market_state,
                yes_price=yes_price,
                no_price=no_price,
                cfg=cfg,
                alert_cache=alert_cache,
            )

        except Exception as e:
            slug_text = current_slug if current_slug else "unknown"
            log(f"Loop error: {e} | slug={slug_text}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
