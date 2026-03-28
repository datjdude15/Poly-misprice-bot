from __future__ import annotations

import argparse
import json
import math
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import requests
import yaml

from execution import ExecutionEngine
from strategy import MomentumIndicator, Tick, evaluate_signal, kelly_cash_size
from tracker import close_trade, get_daily_realized_pnl, get_open_trades, log_signal, open_trade, update_trade_mark


def load_cfg(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_price_data(cfg: Dict) -> Optional[Tick]:
    # Replace this with a direct Polymarket public market feed or websocket for production.
    # For now, supports either a custom JSON endpoint or a fallback shape:
    # {"btc_price": ..., "hour_open": ..., "yes_price": ..., "no_price": ...}
    url = cfg["market"].get("price_source_url")
    if not url:
        return None
    try:
        data = requests.get(url, timeout=3).json()
        return Tick(
            btc_price=float(data["btc_price"]),
            yes_price=float(data["yes_price"]),
            no_price=float(data["no_price"]),
            hour_open=float(data["hour_open"]),
            ts=datetime.now(timezone.utc),
        )
    except Exception:
        return None


def tp_sl_for_tier(cfg: Dict, tier: str) -> tuple[float, float, int]:
    if tier == "LARGE":
        return cfg["execution"]["tp_large"], cfg["execution"]["sl_large"], cfg["execution"]["time_stop_large_min"]
    if tier == "MEDIUM":
        return cfg["execution"]["tp_medium"], cfg["execution"]["sl_medium"], cfg["execution"]["time_stop_medium_min"]
    return cfg["execution"]["tp_small"], cfg["execution"]["sl_small"], cfg["execution"]["time_stop_small_min"]


def approx_pnl_usd(cash_size_usd: float, pnl_per_share: float, entry_price: float) -> float:
    shares = cash_size_usd / max(entry_price, 0.01)
    return round(shares * pnl_per_share, 2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    cfg = load_cfg(args.config)
    db_path = cfg["app"]["db_path"]
    exec_engine = ExecutionEngine(live_mode=bool(cfg["app"].get("live_mode", False)))

    up_momentum = MomentumIndicator(cfg["strategy"]["momentum_window"])
    down_momentum = MomentumIndicator(cfg["strategy"]["momentum_window"])
    recent_moves = deque(maxlen=max(3, cfg["strategy"]["reversal_confirmation_ticks"]))

    while True:
        tick = fetch_price_data(cfg)
        if tick is None:
            time.sleep(cfg["app"]["poll_interval_seconds"])
            continue

        move = tick.btc_price - tick.hour_open
        recent_moves.append(move)
        up_score = up_momentum.update(move)
        down_score = down_momentum.update(-move)
        reversal_ok = len(recent_moves) >= 2 and recent_moves[-1] * recent_moves[-2] > 0

        open_rows = get_open_trades(db_path)
        open_exposure = sum(float(r["cash_size_usd"]) for r in open_rows)
        daily_realized = get_daily_realized_pnl(db_path)

        for side, m_score in (("BUY_UP", up_score), ("BUY_DOWN", down_score)):
            signal = evaluate_signal(tick, direction=side, momentum_score=m_score, cfg=cfg, reversal_ok=reversal_ok)
            signal["meta_json"] = json.dumps({"daily_realized": daily_realized, "open_exposure": open_exposure})
            signal_id = log_signal(db_path, signal)

            if signal["blocked"]:
                continue

            if len(open_rows) >= cfg["risk"]["max_concurrent_positions"]:
                continue
            if open_exposure >= cfg["risk"]["max_open_exposure_usd"]:
                continue
            if cfg["risk"]["panic_stop_enabled"] and daily_realized <= -abs(cfg["risk"]["panic_stop_daily_loss_usd"]):
                continue

            tp_target, sl_target, time_stop_min = tp_sl_for_tier(cfg, signal["tier"])
            cash_size_usd = kelly_cash_size(
                cfg["risk"]["bankroll_usd"],
                signal["tier"],
                cfg["risk"]["kelly_fraction"],
                cfg["risk"]["min_order_usd"],
                cfg["risk"]["max_order_usd"],
            )
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
                continue
            trade_id = open_trade(
                db_path,
                {
                    **order,
                    "status": "OPEN",
                    "live_order_id": accepted.order_id,
                    "notes": accepted.notes,
                },
            )
            open_rows = get_open_trades(db_path)
            open_exposure = sum(float(r["cash_size_usd"]) for r in open_rows)

        # manage open trades using latest yes/no price for the relevant side
        for row in get_open_trades(db_path):
            side = row["side"]
            entry = float(row["entry_price"])
            mark = tick.yes_price if side == "BUY_UP" else tick.no_price
            pnl_per_share = round(mark - entry, 3)
            max_fav = max(float(row["max_favorable"]), pnl_per_share)
            max_adv = min(float(row["max_adverse"]), pnl_per_share)
            update_trade_mark(db_path, int(row["id"]), max_fav, max_adv)

            tp = float(row["tp_target"])
            sl = float(row["sl_target"])
            opened_at = datetime.fromisoformat(row["opened_at"])
            age_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0
            status = None
            if pnl_per_share >= tp:
                status = "TP_HIT"
            elif pnl_per_share <= -sl:
                status = "SL_HIT"
            elif age_min >= int(row["time_stop_min"]):
                status = "TIME_EXIT"

            if status:
                approx = approx_pnl_usd(float(row["cash_size_usd"]), pnl_per_share, entry)
                close_trade(db_path, int(row["id"]), exit_price=mark, pnl_per_share=pnl_per_share, approx_pnl_usd=approx, status=status)

        time.sleep(cfg["app"]["poll_interval_seconds"])


if __name__ == "__main__":
    main()
