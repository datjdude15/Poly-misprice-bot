import requests
import time
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# =========================
# SETTINGS
# =========================
EDGE_THRESHOLD = 0.15
MOVE_THRESHOLD = 15.0
CHECK_SECONDS = 1
COOLDOWN_SECONDS = 180
NO_TRADE_MINUTES = 5
CONFIRMATION_CHECKS = 2

# Pullback settings
PULLBACK_GRACE_SECONDS = 600
PULLBACK_REENTRY_BUFFER = 0.02
LATE_PRICE_BUFFER = 0.07
PULLBACK_MIN_EXTENSION = 0.03

# Simulation settings
SIM_MODE = True
SIM_TP_SMALL = 0.08
SIM_TP_MED = 0.10
SIM_TP_LARGE = 0.15
SIM_SL = 0.05
SIM_TIME_STOP = 900  # 15 minutes

ET = ZoneInfo("America/New_York")

# =========================
# STATE
# =========================
hour_open_price = None
hour_started_at = None
last_alert_time = 0
last_alert_side = None
current_slug = None

pending_action = None
pending_count = 0

sim_trade = None


def reset_pullback_watch():
    return {
        "active": False,
        "alert_sent": False,
        "created_ts": 0,
        "action": None,
        "edge": 0.0,
        "tier": None,
        "unit": None,
        "tp": None,
        "sl": None,
        "time_stop": None,
        "entry_min": None,
        "entry_max": None,
        "extended_price": None,
        "slug": None,
    }


pullback_watch = reset_pullback_watch()

# =========================
# HELPERS
# =========================
def send_alert(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)


def get_btc_price():
    url = "https://api.coinbase.com/v2/pr
