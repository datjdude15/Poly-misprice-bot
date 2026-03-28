from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
import yaml


def load_cfg(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


st.set_page_config(layout="wide")
st.title("PolySniper Live Dashboard")
config_path = st.sidebar.text_input("Config path", value="config.yaml")

if not Path(config_path).exists():
    st.warning("Config file not found. Edit the sidebar path.")
    st.stop()

cfg = load_cfg(config_path)
db_path = cfg["app"]["db_path"]
if not Path(db_path).exists():
    st.warning("Database not found yet. Start the bot first.")
    st.stop()

conn = sqlite3.connect(db_path)
trades = pd.read_sql_query("SELECT * FROM trades ORDER BY id DESC", conn)
signals = pd.read_sql_query("SELECT * FROM signals ORDER BY id DESC", conn)
conn.close()

closed = trades[trades["closed_at"].notna()].copy()
open_df = trades[trades["closed_at"].isna()].copy()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Signals", len(signals))
col2.metric("Closed Trades", len(closed))
col3.metric("Open Trades", len(open_df))
col4.metric("Realized PnL $", f"{closed['approx_pnl_usd'].fillna(0).sum():.2f}")

if len(closed):
    closed["opened_at"] = pd.to_datetime(closed["opened_at"])
    closed["cum_pnl"] = closed["approx_pnl_usd"].fillna(0).cumsum()
    st.plotly_chart(px.line(closed.sort_values("opened_at"), x="opened_at", y="cum_pnl", title="Cumulative Realized PnL"), use_container_width=True)

    win_mask = closed["approx_pnl_usd"].fillna(0) > 0
    st.write(
        {
            "win_rate": round(float(win_mask.mean()) * 100, 2),
            "avg_win_usd": round(float(closed.loc[win_mask, "approx_pnl_usd"].mean() or 0), 2),
            "avg_loss_usd": round(float(closed.loc[~win_mask, "approx_pnl_usd"].mean() or 0), 2),
            "expectancy_usd": round(float(closed["approx_pnl_usd"].mean() or 0), 2),
        }
    )

st.subheader("Closed Trades")
st.dataframe(closed.head(100), use_container_width=True)

st.subheader("Recent Signals")
st.dataframe(signals.head(100), use_container_width=True)
