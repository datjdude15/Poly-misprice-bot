import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
import yaml

from market_resolver import resolve_current_market_state, fetch_public_clob_midpoint


@dataclass
class Position:
    side: str
    setup: str
    entry_price: float
    entry_time: float
    size_usd: float
    tp_target: float
    sl_target: float
    time_stop_minutes: int
    slug: str
    yes_price_at_entry: float
    no_price_at_entry: float
    hour_open_btc: float
    btc_at_entry: float


class PolyBot:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self.load_config(config_path)

        self.mode = self.config.get("mode", "paper")
        self.poll_seconds = int(self.config.get("poll_seconds", 5))

        self.market_slug: Optional[str] = None
        self.yes_token_id: Optional[str] = None
        self.no_token_id: Optional[str] = None
        self.hour_open_btc: Optional[float] = None

        self.current_position: Optional[Position] = None
        self.last_heartbeat_minute: Optional[int] = None
        self.last_market_refresh_hour_key: Optional[str] = None

        self.telegram_bot_token = self.config.get("telegram_bot_token", "").strip()
        self.telegram_chat_id = self.config.get("telegram_chat_id", "").strip()

        self.db_path = self.config.get("database_path", "trades.db")
        self.init_db()

        self.print_log("🚀 BOT STARTING")
        self.print_log(f"Loading config from {self.config_path}")
        self.print_log(f"Mode -> {self.mode.upper()}")
        self.print_log(f"Polling every {self.poll_seconds} seconds")

    @staticmethod
    def load_config(path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            slug TEXT,
            side TEXT,
            setup TEXT,
            entry_price REAL,
            exit_price REAL,
            pnl_points REAL,
            size_usd REAL,
            approx_pnl_usd REAL,
            result TEXT,
            btc_entry REAL,
            btc_exit REAL,
            hour_open_btc REAL
        )
        """)

        conn.commit()
        conn.close()

    @staticmethod
    def now_ts() -> float:
        return time.time()

    @staticmethod
    def now_utc_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def print_log(self, message: str):
        now = datetime.now().strftime("[%H:%M:%S]")
        print(f"{now} {message}", flush=True)

    def telegram_send(self, message: str):
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return

        try:
            requests.post(
                f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": self.telegram_chat_id,
                    "text": message
                },
                timeout=10
            )
        except Exception as e:
            self.print_log(f"Telegram send failed: {e}")

    def save_trade(
        self,
        slug: str,
        side: str,
        setup: str,
        entry_price: float,
        exit_price: float,
        pnl_points: float,
        size_usd: float,
        approx_pnl_usd: float,
        result: str,
        btc_entry: float,
        btc_exit: float,
        hour_open_btc: float,
    ):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO trades (
            created_at, slug, side, setup, entry_price, exit_price, pnl_points,
            size_usd, approx_pnl_usd, result, btc_entry, btc_exit, hour_open_btc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.now_utc_str(),
            slug,
            side,
            setup,
            entry_price,
            exit_price,
            pnl_points,
            size_usd,
            approx_pnl_usd,
            result,
            btc_entry,
            btc_exit,
            hour_open_btc,
        ))
        conn.commit()
        conn.close()

    # -------------------------
    # Market Data
    # -------------------------

    def fetch_btc_spot(self) -> Optional[float]:
        """
        Coinbase spot endpoint avoids the Binance 451 geo-block issue.
        """
        try:
            r = requests.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            return float(data["data"]["amount"])
        except Exception as e:
            self.print_log(f"BTC fetch failed: {e}")
            return None

    def refresh_hourly_market_if_needed(self):
        market_cfg = self.config.get("market", {})
        auto_switch = bool(market_cfg.get("auto_switch_hourly", True))
        tz_name = market_cfg.get("timezone", "US/Central")

        if not auto_switch:
            self.market_slug = market_cfg.get("slug", "")
            self.yes_token_id = market_cfg.get("yes_token_id", "")
            self.no_token_id = market_cfg.get("no_token_id", "")
            self.hour_open_btc = float(market_cfg.get("hour_open_btc", 0))
            return

        state = resolve_current_market_state(tz_name=tz_name)

        hour_key = f"{state.slug}|{int(state.hour_open_btc)}"
        if hour_key != self.last_market_refresh_hour_key:
            self.market_slug = state.slug
            self.yes_token_id = state.yes_token_id
            self.no_token_id = state.no_token_id
            self.hour_open_btc = state.hour_open_btc
            self.last_market_refresh_hour_key = hour_key

            self.print_log(f"[AUTO] Switched market -> {self.market_slug}")
            self.print_log(f"[AUTO] YES token -> {self.yes_token_id}")
            self.print_log(f"[AUTO] NO token -> {self.no_token_id}")
            self.print_log(f"[AUTO] Hour open BTC -> {self.hour_open_btc}")

            self.telegram_send(
                f"🔄 Market switched\n"
                f"Slug: {self.market_slug}\n"
                f"Hour Open BTC: {self.hour_open_btc}"
            )

    # -------------------------
    # Signal Logic
    # -------------------------

    def minutes_left_in_hour(self) -> int:
        now = datetime.now()
        return 59 - now.minute

    def calculate_expected_up_probability(self, btc_spot: float, hour_open_btc: float, min_move_abs: float) -> float:
        move = btc_spot - hour_open_btc
        if min_move_abs <= 0:
            min_move_abs = 30.0

        scaled = move / min_move_abs
        p_up = 0.50 + (0.12 * scaled)
        p_up = max(0.05, min(0.95, p_up))
        return p_up

    @staticmethod
    def midpoint_to_edge_cents(fair_prob: float, market_price: float) -> float:
        return round((fair_prob - market_price) * 100, 2)

    def compute_setup_and_size(self, edge_cents: float) -> tuple[str, float, float, float, int]:
        risk_cfg = self.config.get("risk", {})
        exec_cfg = self.config.get("execution", {})

        min_order = float(risk_cfg.get("min_order_usd", 15))
        max_order = float(risk_cfg.get("max_order_usd", 60))
        bankroll = float(risk_cfg.get("bankroll_usd", 1000))

        if edge_cents >= 50:
            tier = "LARGE"
            cash_size = min(max_order, max(min_order, bankroll * 0.06))
            tp_target = float(exec_cfg.get("tp_large", 0.15))
            sl_target = float(exec_cfg.get("sl_large", 0.07))
            time_stop = int(exec_cfg.get("time_stop_large_minutes", 20))
        else:
            tier = "MEDIUM"
            cash_size = min(max_order, max(min_order, bankroll * 0.04))
            tp_target = float(exec_cfg.get("tp_medium", 0.10))
            sl_target = float(exec_cfg.get("sl_medium", 0.06))
            time_stop = int(exec_cfg.get("time_stop_medium_minutes", 15))

        return tier, cash_size, tp_target, sl_target, time_stop

    def entry_filters_pass(
        self,
        yes_mid: float,
        no_mid: float,
        edge_up_cents: float,
        edge_down_cents: float,
        btc_spot: float,
        hour_open_btc: float
    ) -> tuple[bool, Optional[str], Optional[str], Optional[float], Optional[str]]:
        strategy = self.config.get("strategy", {})

        min_edge_cents = float(strategy.get("min_edge_cents", 35))
        min_move_abs = float(strategy.get("min_move_abs", 30))
        min_entry_price = float(strategy.get("min_entry_price", 0.25))
        max_entry_price = float(strategy.get("max_entry_price", 0.60))
        no_trade_min_minutes_left = int(strategy.get("no_trade_min_minutes_left", 5))
        no_trade_max_minutes_left = int(strategy.get("no_trade_max_minutes_left", 55))

        minutes_left = self.minutes_left_in_hour()
        if minutes_left <= no_trade_min_minutes_left or minutes_left >= no_trade_max_minutes_left:
            return False, None, None, None, "FAILED_NO_TRADE_WINDOW"

        move = btc_spot - hour_open_btc
        if abs(move) < min_move_abs:
            return False, None, None, None, "FAILED_MIN_MOVE"

        buy_up_allowed = bool(strategy.get("allow_direction", {}).get("buy_up", True))
        buy_down_allowed = bool(strategy.get("allow_direction", {}).get("buy_down", True))

        candidates = []

        if buy_up_allowed and min_entry_price <= yes_mid <= max_entry_price and edge_up_cents >= min_edge_cents:
            candidates.append(("BUY_UP", "CORE", yes_mid, edge_up_cents))

        if buy_down_allowed and min_entry_price <= no_mid <= max_entry_price and edge_down_cents >= min_edge_cents:
            candidates.append(("BUY_DOWN", "CORE", no_mid, edge_down_cents))

        if not candidates:
            return False, None, None, None, "FAILED_MIN_EDGE_OR_ENTRY"

        side, setup, entry_price, _ = max(candidates, key=lambda x: x[3])
        return True, side, setup, entry_price, None

    # -------------------------
    # Paper Position Management
    # -------------------------

    def maybe_open_position(self, yes_mid: float, no_mid: float, btc_spot: float):
        if self.current_position is not None:
            return

        strategy = self.config.get("strategy", {})
        min_move_abs = float(strategy.get("min_move_abs", 30))

        p_up = self.calculate_expected_up_probability(
            btc_spot=btc_spot,
            hour_open_btc=self.hour_open_btc,
            min_move_abs=min_move_abs
        )
        p_down = 1.0 - p_up

        edge_up_cents = self.midpoint_to_edge_cents(p_up, yes_mid)
        edge_down_cents = self.midpoint_to_edge_cents(p_down, no_mid)

        passed, side, setup, entry_price, reason = self.entry_filters_pass(
            yes_mid=yes_mid,
            no_mid=no_mid,
            edge_up_cents=edge_up_cents,
            edge_down_cents=edge_down_cents,
            btc_spot=btc_spot,
            hour_open_btc=self.hour_open_btc
        )

        self.print_log(
            f"[TICK] slug={self.market_slug} "
            f"btc={btc_spot:.2f} open={self.hour_open_btc:.2f} "
            f"yes={yes_mid:.3f} no={no_mid:.3f} "
            f"edge_up={edge_up_cents:.1f}c edge_down={edge_down_cents:.1f}c"
        )

        if not passed:
            return

        tier, cash_size, tp_target, sl_target, time_stop = self.compute_setup_and_size(
            edge_up_cents if side == "BUY_UP" else edge_down_cents
        )

        self.current_position = Position(
            side=side,
            setup=setup,
            entry_price=entry_price,
            entry_time=self.now_ts(),
            size_usd=cash_size,
            tp_target=tp_target,
            sl_target=sl_target,
            time_stop_minutes=time_stop,
            slug=self.market_slug,
            yes_price_at_entry=yes_mid,
            no_price_at_entry=no_mid,
            hour_open_btc=self.hour_open_btc,
            btc_at_entry=btc_spot
        )

        msg = (
            f"SIM START\n"
            f"{side}\n"
            f"Setup: {setup}\n"
            f"Entry: {entry_price:.3f}\n"
            f"Tier: {tier}\n"
            f"Cash Size: ${cash_size:.0f}\n"
            f"TP Target: +{tp_target:.3f}\n"
            f"SL Target: -{sl_target:.3f}\n"
            f"Time Stop: {time_stop} min\n"
            f"Slug: {self.market_slug}"
        )
        self.print_log(msg.replace("\n", " | "))
        self.telegram_send(msg)

    def maybe_close_position(self, yes_mid: float, no_mid: float, btc_spot: float):
        pos = self.current_position
        if pos is None:
            return

        live_price = yes_mid if pos.side == "BUY_UP" else no_mid
        pnl_points = round(live_price - pos.entry_price, 3)
        approx_pnl_usd = round(pnl_points * pos.size_usd, 2)

        elapsed_minutes = (self.now_ts() - pos.entry_time) / 60.0

        result = None

        if pnl_points >= pos.tp_target:
            result = "TP HIT"
        elif pnl_points <= -pos.sl_target:
            result = "SL HIT"
        elif elapsed_minutes >= pos.time_stop_minutes:
            result = "TIME EXIT"

        if result is None:
            return

        msg = (
            f"SIM RESULT\n"
            f"{result}\n"
            f"Action: {pos.side}\n"
            f"Setup: {pos.setup}\n"
            f"Entry: {pos.entry_price:.3f}\n"
            f"Exit: {live_price:.3f}\n"
            f"PnL: {pnl_points:.3f}\n"
            f"Cash Size: ${pos.size_usd:.0f}\n"
            f"Approx $PnL: ${approx_pnl_usd:.2f}"
        )

        self.print_log(msg.replace("\n", " | "))
        self.telegram_send(msg)

        self.save_trade(
            slug=pos.slug,
            side=pos.side,
            setup=pos.setup,
            entry_price=pos.entry_price,
            exit_price=live_price,
            pnl_points=pnl_points,
            size_usd=pos.size_usd,
            approx_pnl_usd=approx_pnl_usd,
            result=result,
            btc_entry=pos.btc_at_entry,
            btc_exit=btc_spot,
            hour_open_btc=pos.hour_open_btc,
        )

        self.current_position = None

    # -------------------------
    # Main loop
    # -------------------------

    def heartbeat(self):
        now = datetime.now()
        if self.last_heartbeat_minute == now.minute:
            return

        self.last_heartbeat_minute = now.minute
        self.print_log("Heartbeat... bot running")

    def run(self):
        while True:
            try:
                self.refresh_hourly_market_if_needed()

                if not self.yes_token_id or not self.no_token_id:
                    self.print_log("Missing token IDs - waiting...")
                    time.sleep(self.poll_seconds)
                    continue

                if not self.hour_open_btc or self.hour_open_btc <= 0:
                    self.print_log("Missing hour_open_btc - waiting...")
                    time.sleep(self.poll_seconds)
                    continue

                yes_mid = fetch_public_clob_midpoint(self.yes_token_id)
                no_mid = fetch_public_clob_midpoint(self.no_token_id)
                btc_spot = self.fetch_btc_spot()

                self.heartbeat()

                if yes_mid is None or no_mid is None:
                    self.print_log("Public CLOB mode active but no tick returned")
                    time.sleep(self.poll_seconds)
                    continue

                if btc_spot is None:
                    self.print_log("BTC spot missing - waiting...")
                    time.sleep(self.poll_seconds)
                    continue

                if self.mode == "paper":
                    self.maybe_open_position(yes_mid=yes_mid, no_mid=no_mid, btc_spot=btc_spot)
                    self.maybe_close_position(yes_mid=yes_mid, no_mid=no_mid, btc_spot=btc_spot)
                else:
                    self.print_log("LIVE mode not enabled in this bot file yet. Use paper mode for now.")

                time.sleep(self.poll_seconds)

            except KeyboardInterrupt:
                self.print_log("Bot stopped by user.")
                sys.exit(0)
            except Exception as e:
                self.print_log(f"Loop error: {e}")
                time.sleep(self.poll_seconds)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to config YAML")
    args = parser.parse_args()

    bot = PolyBot(args.config)
    bot.run()


if __name__ == "__main__":
    main()
