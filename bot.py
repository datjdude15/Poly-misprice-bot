from __future__ import annotations

import argparse
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional

import requests
import yaml

from execution import ExecutionEngine
from strategy import MomentumIndicator, Tick, evaluate_signal, kelly_cash_size
from tracker import (
    close_trade,
    get_daily_realized_pnl,
    get_open_trades,
    log_signal,
    open_trade,
)

CLOB_BOOK_URL = "https://clob.polymarket.com/book"


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cfg(path: str) -> Dict:
    log(f"Loading config from {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    log("Config loaded successfully")
    return cfg


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def fetch_public_clob_midpoint(cfg: Dict) -> Optional[Tick]:
    market_cfg = cfg.get("market", {})
    yes_token_id = market_cfg.get("yes_token_id")
    no_token_id = market_cfg.get("no_token_id")
    hour_open = market_cfg.get("hour_open_btc")

    if not yes_token_id or not no_token_id:
        log("Public CLOB mode enabled but yes_token_id / no_token_id missing")
        return None

    if hour_open is None:
        log("Public CLOB mode enabled but hour_open_btc missing")
        return None

    try:
        yes_resp = requests.get(
            CLOB_BOOK_URL,
            params={"token_id": yes_token_id},
            timeout=5,
        )
        yes_resp.raise_for_status()
        yes_book = yes_resp.json()

        no_resp = requests.get(
            CLOB_BOOK_URL,
            params={"token_id": no_token_id},
            timeout=5,
        )
        no_resp.raise_for_status()
        no_book = no_resp.json()

        def midpoint(book: Dict) -> Optional[float]:
            bids = book.get("bids", []) or []
            asks = book.get("asks", []) or []

            best_bid = _safe_float(bids[0]["price"]) if bids else None
            best_ask = _safe_float(asks[0]["price"]) if asks else None

            if best_bid is not None and best_ask is not None:
                return round((best_bid + best_ask) / 2.0, 3)
            if best_bid is not None:
                return round(best_bid, 3)
            if best_ask is not None:
                return round(best_ask, 3)
            return None

        yes_price = midpoint(yes_book)
        no_price = midpoint(no_book)

        if yes_price is None or no_price is None:
            log("Public CLOB returned empty book on one or both sides")
            return None

        btc_price = _safe_float(market_cfg.get("btc_spot_fallback"), _safe_float(hour_open))

        tick = Tick(
            btc_price=btc_price,
            yes_price=yes_price,
            no_price=no_price,
            hour_open=_safe_float(hour_open),
            ts=datetime.now(timezone.utc),
        )

        log(
            f"Public CLOB Tick → BTC: {tick.btc_price} | YES: {tick.yes_price} | "
            f"NO: {tick.no_price} | HOUR_OPEN: {tick.hour_open}"
        )
        return tick

    except Exception as e:
        log(f"Public CLOB fetch failed: {e}")
        return None


def fetch_external_price_data(cfg: Dict) -> Optional[Tick]:
    url = cfg.get("market", {}).get("price_source_url")

    if not url:
        return None

    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        tick = Tick(
            btc_price=float(data["btc_price"]),
            yes_price=float(data["yes_price"]),
            no_price=float(data["no_price"]),
            hour_open=float(data["hour_open"]),
            ts=datetime.now(timezone.utc),
        )

        log(
            f"External Tick → BTC: {tick.btc_price} | YES: {tick.yes_price} | "
            f"NO: {tick.no_price} | HOUR_OPEN: {tick.hour_open}"
        )
        return tick

    except Exception as e:
        log(f"External price fetch failed: {e}")
        return None


def fetch_price_data(cfg: Dict) -> Optional[Tick]:
    market_cfg = cfg.get("market", {})
    use_public = bool(market_cfg.get("use_public_clob_midpoint", False))

    if use_public:
        tick = fetch_public_clob_midpoint(cfg)
        if tick is not None:
            return tick
        log("Public CLOB mode active but no tick returned")

    tick = fetch_external_price_data(cfg)
    if tick is not None:
        return tick

    if use_public:
        log("No usable public CLOB data and no external price_source_url fallback")
    else:
        log("No price_source_url set — waiting...")

    return None


def tp_sl_for_tier(cfg: Dict, tier: str):
    execution_cfg = cfg["execution"]
    if tier == "LARGE":
        return (
            execution_cfg["tp_large"],
            execution_cfg["sl_large"],
            execution_cfg["time_stop_large_min"],
        )
    if tier == "MEDIUM":
        return (
            execution_cfg["tp_medium"],
            execution_cfg["sl_medium"],
            execution_cfg["time_stop_medium_min"],
        )
    return (
        execution_cfg["tp_small"],
        execution_cfg["sl_small"],
        execution_cfg["time_stop_small_min"],
    )


def main():
    log("🚀 BOT STARTING...")

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    cfg = load_cfg(args.config)

    app_cfg = cfg["app"]
    strategy_cfg = cfg["strategy"]
    risk_cfg = cfg["risk"]

    log(f"Mode → LIVE: {app_cfg['live_mode']} | PAPER: {app_cfg['paper_mode']}")
    log(f"Polling every {app_cfg['poll_interval_seconds']} seconds")

    db_path = app_cfg["db_path"]
    exec_engine = ExecutionEngine(live_mode=bool(app_cfg.get("live_mode", False)))

    up_momentum = MomentumIndicator(strategy_cfg["momentum_window"])
    down_momentum = MomentumIndicator(strategy_cfg["momentum_window"])
    recent_moves = deque(maxlen=max(3, strategy_cfg["reversal_confirmation_ticks"]))

    while True:
        try:
            log("Heartbeat... bot running")

            tick = fetch_price_data(cfg)
            if tick is None:
                time.sleep(app_cfg["poll_interval_seconds"])
                continue

            move = tick.btc_price - tick.hour_open
            recent_moves.append(move)

            up_score = up_momentum.update(move)
            down_score = down_momentum.update(-move)

            reversal_ok = (
                len(recent_moves) >= 2
                and recent_moves[-1] * recent_moves[-2] > 0
            )

            open_rows = get_open_trades(db_path)
            open_exposure = sum(float(r["cash_size_usd"]) for r in open_rows)
            daily_realized = get_daily_realized_pnl(db_path)

            log(
                f"Open trades: {len(open_rows)} | "
                f"Exposure: {open_exposure} | Daily PnL: {daily_realized}"
            )

            for side, m_score in (("BUY_UP", up_score), ("BUY_DOWN", down_score)):
                signal = evaluate_signal(
                    tick=tick,
                    direction=side,
                    momentum_score=m_score,
                    cfg=cfg,
                    reversal_ok=reversal_ok,
                )

                log(
                    f"Signal {side} → BLOCKED: {signal['blocked']} | "
                    f"EDGE: {signal.get('edge')} | "
                    f"ENTRY: {signal.get('entry_price')} | "
                    f"TIER: {signal.get('tier')}"
                )

                signal_id = log_signal(db_path, signal)

                if signal["blocked"]:
                    continue

                if len(open_rows) >= risk_cfg["max_concurrent_positions"]:
                    log("Blocked: max positions reached")
                    continue

                if open_exposure >= risk_cfg["max_open_exposure_usd"]:
                    log("Blocked: max exposure reached")
                    continue

                if (
                    risk_cfg["panic_stop_enabled"]
                    and daily_realized <= -abs(risk_cfg["panic_stop_daily_loss_usd"])
                ):
                    log("Blocked: panic stop triggered")
                    continue

                tp_target, sl_target, time_stop_min = tp_sl_for_tier(cfg, signal["tier"])

                cash_size_usd = kelly_cash_size(
                    bankroll_usd=risk_cfg["bankroll_usd"],
                    tier=signal["tier"],
                    kelly_fraction=risk_cfg["kelly_fraction"],
                    min_order_usd=risk_cfg["min_order_usd"],
                    max_order_usd=risk_cfg["max_order_usd"],
                )

                log(f"EXECUTING TRADE → {side} | Size: ${cash_size_usd}")

                order = {
                    "signal_id": signal_id,
                    "side": signal["side"],
                    "setup": signal["setup"],
                    "entry_price": signal["entry_price"],
                    "cash_size_usd": cash_size_usd,
                    "tp_target": tp_target,
                    "sl_target": sl_target,
                    "time_stop_min": time_stop_min,
                }

                accepted = exec_engine.submit(order)

                if not accepted.accepted:
                    log(f"Order rejected: {accepted.notes}")
                    continue

                log(f"Order accepted → ID: {accepted.order_id}")

                open_trade(
                    db_path,
                    {
                        **order,
                        "status": "OPEN",
                        "live_order_id": accepted.order_id,
                        "notes": accepted.notes,
                    },
                )

            for row in get_open_trades(db_path):
                side = row["side"]
                entry = float(row["entry_price"])

                mark = tick.yes_price if side == "BUY_UP" else tick.no_price
                pnl_per_share = round(mark - entry, 3)

                if pnl_per_share >= float(row["tp_target"]):
                    log(f"TP HIT → Trade {row['id']}")
                    close_trade(db_path, int(row["id"]), mark, pnl_per_share, 0, "TP_HIT")

                elif pnl_per_share <= -float(row["sl_target"]):
                    log(f"SL HIT → Trade {row['id']}")
                    close_trade(db_path, int(row["id"]), mark, pnl_per_share, 0, "SL_HIT")

            time.sleep(app_cfg["poll_interval_seconds"])

        except Exception as e:
            log(f"🔥 CRASH: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
