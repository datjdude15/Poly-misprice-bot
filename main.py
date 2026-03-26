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
CHECK_SECONDS = 10
COOLDOWN_SECONDS = 180
NO_TRADE_MINUTES = 5
CONFIRMATION_CHECKS = 2

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


# =========================
# HELPERS
# =========================
def send_alert(message: str):
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
    return f"bitcoin-up-or-down-{dt.strftime('%B').lower()}-{dt.day}-{dt.year}-{hour_to_12(dt.hour)}{ampm(dt.hour)}-et"


def get_market(slug: str):
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


# =========================
# TRADE PLAN LOGIC
# =========================
def build_trade_plan(edge, action, yes_price, no_price):
    edge_cents = edge * 100

    if edge_cents < 25:
        tier = "SMALL"
        unit = "0.5u"
        tp = "+6 to +8¢"
        sl = "-4 to -5¢"
        time_stop = "10–12 min"
        entry_slippage = 0.01
    elif edge_cents < 40:
        tier = "MEDIUM"
        unit = "1u"
        tp = "+8 to +12¢"
        sl = "-5 to -7¢"
        time_stop = "15–20 min"
        entry_slippage = 0.02
    else:
        tier = "LARGE"
        unit = "1.5u"
        tp = "Scale: +10¢ / +15–20¢"
        sl = "-6 to -8¢"
        time_stop = "20–30 min"
        entry_slippage = 0.03

    if action == "BUY UP":
        base_price = yes_price
    else:
        base_price = no_price

    entry_min = round(base_price, 3)
    entry_max = round(base_price + entry_slippage, 3)

    return tier, unit, tp, sl, time_stop, entry_min, entry_max


# =========================
# SIGNAL LOGIC
# =========================
def evaluate_signal(btc, open_price, yes, no):
    move = btc - open_price
    abs_move = abs(move)

    if abs_move < MOVE_THRESHOLD:
        return None, 0, move

    if move > 0:
        edge = 0.75 - yes
        if edge >= EDGE_THRESHOLD:
            return "BUY UP", edge, move

    if move < 0:
        edge = 0.75 - no
        if edge >= EDGE_THRESHOLD:
            return "BUY DOWN", edge, move

    return None, 0, move


# =========================
# MAIN LOOP
# =========================
while True:
    try:
        btc = get_btc_price()
        now = datetime.now(ET)

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

            time.sleep(CHECK_SECONDS)
            continue

        yes, no = parse_prices(market["outcomePrices"])
        action, edge, move = evaluate_signal(btc, hour_open_price, yes, no)

        if in_no_trade_window(now):
            pending_action = None
            pending_count = 0
            time.sleep(CHECK_SECONDS)
            continue

        # confirmation logic
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

        now_ts = time.time()

        if confirmed and (now_ts - last_alert_time > COOLDOWN_SECONDS) and action != last_alert_side:

            tier, unit, tp, sl, time_stop, entry_min, entry_max = build_trade_plan(edge, action, yes, no)

            link = f"https://polymarket.com/event/{slug}"

            send_alert(
                f"🚨 MISPRICE\n"
                f"{action}\n"
                f"BTC: {btc}\n"
                f"Hour Open: {hour_open_price}\n"
                f"Move: {move:.2f}\n"
                f"YES: {yes}\n"
                f"NO: {no}\n"
                f"Edge: {edge*100:.1f}¢\n\n"
                f"📍 ENTRY ZONE\n"
                f"{entry_min} – {entry_max}\n"
                f"Do Not Chase Above: {entry_max}\n\n"
                f"📊 TRADE PLAN\n"
                f"Tier: {tier}\n"
                f"Size: {unit}\n"
                f"Take Profit: {tp}\n"
                f"Stop Loss: {sl}\n"
                f"Time Stop: {time_stop}\n\n"
                f"{link}"
            )

            last_alert_time = now_ts
            last_alert_side = action
            pending_action = None
            pending_count = 0

        time.sleep(CHECK_SECONDS)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
