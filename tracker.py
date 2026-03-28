from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    side TEXT NOT NULL,
    setup TEXT NOT NULL,
    btc_price REAL NOT NULL,
    hour_open REAL NOT NULL,
    move REAL NOT NULL,
    yes_price REAL NOT NULL,
    no_price REAL NOT NULL,
    entry_price REAL NOT NULL,
    edge_cents REAL NOT NULL,
    momentum_score REAL NOT NULL,
    blocked INTEGER NOT NULL,
    blocked_by TEXT,
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    side TEXT NOT NULL,
    setup TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    pnl_per_share REAL,
    cash_size_usd REAL NOT NULL,
    approx_pnl_usd REAL,
    tp_target REAL NOT NULL,
    sl_target REAL NOT NULL,
    time_stop_min INTEGER NOT NULL,
    max_favorable REAL DEFAULT 0,
    max_adverse REAL DEFAULT 0,
    live_order_id TEXT,
    notes TEXT,
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);
"""


@contextmanager
def db_conn(path: str):
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_signal(path: str, record: Dict[str, Any]) -> int:
    with db_conn(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO signals (
                created_at, side, setup, btc_price, hour_open, move, yes_price, no_price,
                entry_price, edge_cents, momentum_score, blocked, blocked_by, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                record["side"],
                record["setup"],
                record["btc_price"],
                record["hour_open"],
                record["move"],
                record["yes_price"],
                record["no_price"],
                record["entry_price"],
                record["edge_cents"],
                record["momentum_score"],
                1 if record.get("blocked") else 0,
                record.get("blocked_by"),
                record.get("meta_json", "{}"),
            ),
        )
        return int(cur.lastrowid)


def open_trade(path: str, trade: Dict[str, Any]) -> int:
    with db_conn(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                signal_id, opened_at, side, setup, status, entry_price, cash_size_usd,
                tp_target, sl_target, time_stop_min, live_order_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.get("signal_id"),
                utc_now_iso(),
                trade["side"],
                trade["setup"],
                trade["status"],
                trade["entry_price"],
                trade["cash_size_usd"],
                trade["tp_target"],
                trade["sl_target"],
                trade["time_stop_min"],
                trade.get("live_order_id"),
                trade.get("notes"),
            ),
        )
        return int(cur.lastrowid)


def update_trade_mark(path: str, trade_id: int, max_favorable: float, max_adverse: float) -> None:
    with db_conn(path) as conn:
        conn.execute(
            "UPDATE trades SET max_favorable=?, max_adverse=? WHERE id=?",
            (max_favorable, max_adverse, trade_id),
        )


def close_trade(path: str, trade_id: int, *, exit_price: float, pnl_per_share: float, approx_pnl_usd: float, status: str) -> None:
    with db_conn(path) as conn:
        conn.execute(
            """
            UPDATE trades
            SET closed_at=?, exit_price=?, pnl_per_share=?, approx_pnl_usd=?, status=?
            WHERE id=?
            """,
            (utc_now_iso(), exit_price, pnl_per_share, approx_pnl_usd, status, trade_id),
        )


def get_open_trades(path: str) -> list[sqlite3.Row]:
    with db_conn(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades WHERE closed_at IS NULL").fetchall()
        return rows


def get_daily_realized_pnl(path: str) -> float:
    with db_conn(path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(approx_pnl_usd), 0) FROM trades WHERE closed_at IS NOT NULL AND DATE(closed_at)=DATE('now')"
        ).fetchone()
        return float(row[0] or 0)
