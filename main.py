import requests
import time
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# SETTINGS
EDGE_THRESHOLD = 0.15
MOVE_THRESHOLD = 15.0
CHECK_SECONDS = 1
COOLDOWN_SECONDS = 180
NO_TRADE_MINUTES = 5
CONFIRMATION_CHECKS = 2

# Pullback
PULLBACK_GRACE_SECONDS = 600
PULLBACK_REENTRY_BUFFER = 0.02
LATE_PRICE_BUFFER = 0.07

# Simulation
SIM_MODE = True
SIM_TP = 0.10
SIM_SL = 0.05
SIM_TIME_STOP = 900  # 15 min

ET = ZoneInfo("America/New_York")

# STATE
hour_open_price = None
hour_started_at = None
last_alert_time = 0
last_alert_side = None
current_slug = None

pending_action = None
pending_count = 0

sim_trade = None


# =========================
# HELPERS
# =========================

def send_alert(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)


def get_btc_price():
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


def parse_prices(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    return float(raw[0]), float(raw[1])


def hour_to_12(h):
    h = h % 12
    return 12 if h == 0 else h


def ampm(h):
    return "am" if h < 12 else "pm"


def build_slug(dt):
    month = dt.strftime("%B").lower()
    day = dt.day
    year = dt.year
    hour = hour_to_12(dt.hour)
    suffix = ampm(dt.hour)
    return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour}{suffix}-et"


def get_market(slug):
    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        r = requests.get(url, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except:
        return None


def find_market(now):
    for dt in [now, now - timedelta(hours=1), now + timedelta(hours=1)]:
        slug = build_slug(dt)
        market = get_market(slug)
        if market:
            return slug, market
    return None, None


def in_no_trade_window(now):
    if hour_started_at is None:
        return True
    return (now - hour_started_at).total_seconds() < NO_TRADE_MINUTES * 60


def evaluate_signal(btc, open_price, yes, no):
    move = btc - open_price

    if abs(move) < MOVE_THRESHOLD:
        return None, 0.0, move

    if move > 0:
        edge = 0.75 - yes
        if edge >= EDGE_THRESHOLD:
            return "BUY UP", edge, move

    if move < 0:
        edge = 0.75 - no
        if edge >= EDGE_THRESHOLD:
            return "BUY DOWN", edge, move

    return None, 0.0, move


def build_trade_plan(edge, action, yes, no):
    edge_cents = edge * 100

    if edge_cents < 25:
        tier = "SMALL"
        unit = "0.5u"
        tp = "+6 to +8c"
        sl = "-4 to -5c"
        entry_slip = 0.01
    elif edge_cents < 40:
        tier = "MEDIUM"
        unit = "1u"
        tp = "+8 to +12c"
        sl = "-5 to -7c"
        entry_slip = 0.02
    else:
        tier = "LARGE"
        unit = "1.5u"
        tp = "+10 to +20c"
        sl = "-6 to -8c"
        entry_slip = 0.03

    base_price = yes if action == "BUY UP" else no
    entry_min = round(base_price, 3)
    entry_max = round(base_price + entry_slip, 3)

    return tier, unit, tp, sl, entry_min, entry_max


# =========================
# SIMULATION
# =========================

def start_sim_trade(action, entry_price, tier, now_ts):
    global sim_trade

    sim_trade = {
        "action": action,
        "entry": entry_price,
        "start": now_ts,
        "active": True
    }

    send_alert(
        f"SIM START\n"
        f"{action}\n"
        f"Entry: {entry_price}\n"
        f"Tier: {tier}"
    )


def update_sim_trade(yes, no, now_ts):
    global sim_trade

    if not sim_trade or not sim_trade["active"]:
        return

    price = yes if sim_trade["action"] == "BUY UP" else no
    pnl = price - sim_trade["entry"]

    if pnl >= SIM_TP:
        send_alert(f"SIM RESULT\nTP HIT\nPnL: {pnl:.3f}")
        sim_trade["active"] = False

    elif pnl <= -SIM_SL:
        send_alert(f"SIM RESULT\nSL HIT\nPnL: {pnl:.3f}")
        sim_trade["active"] = False

    elif now_ts - sim_trade["start"] > SIM_TIME_STOP:
        send_alert(f"SIM RESULT\nTIME EXIT\nPnL: {pnl:.3f}")
        sim_trade["active"] = False


# =========================
# MAIN LOOP
# =========================

while True:
    try:
        btc = get_btc_price()
        now = datetime.now(ET)
        now_ts = time.time()

        slug, market = find_market(now)

        if not market:
            time.sleep(CHECK_SECONDS)
            continue

        if slug != current_slug:
            current_slug = slug
            hour_open_price = btc
            hour_started_at = now.replace(minute=0, second=0, microsecond=0)

            pending_action = None
            pending_count = 0
            last_alert_side = None
            continue

        yes, no = parse_prices(market["outcomePrices"])

        update_sim_trade(yes, no, now_ts)

        action, edge, move = evaluate_signal(btc, hour_open_price, yes, no)

        if in_no_trade_window(now):
            continue

        if action:
            if action == pending_action:
                pending_count += 1
            else:
                pending_action = action
                pending_count = 1
        else:
            pending_action = None
            pending_count = 0

        confirmed = pending_count >= CONFIRMATION_CHECKS

        if confirmed and (now_ts - last_alert_time > COOLDOWN_SECONDS):

            tier, unit, tp, sl, entry_min, entry_max = build_trade_plan(edge, action, yes, no)

            send_alert(
                f"MISPRICE\n"
                f"{action}\n"
                f"BTC: {btc}\n"
                f"Move: {move:.2f}\n"
                f"YES: {yes}\n"
                f"NO: {no}\n"
                f"Edge: {edge*100:.1f}c\n\n"
                f"ENTRY\n"
                f"{entry_min} - {entry_max}\n\n"
                f"{tier} | {unit}\n"
                f"TP: {tp}\n"
                f"SL: {sl}"
            )

            if SIM_MODE and (not sim_trade or not sim_trade["active"]):
                start_sim_trade(action, entry_max, tier, now_ts)

            last_alert_time = now_ts
            last_alert_side = action

        time.sleep(CHECK_SECONDS)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
