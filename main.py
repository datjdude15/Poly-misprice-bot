import requests
import time
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

EDGE_THRESHOLD = 0.10
CHECK_SECONDS = 10
COOLDOWN_SECONDS = 180

ET = ZoneInfo("America/New_York")

hour_open_price = None
last_alert_time = 0
last_alert_side = None
current_slug = None


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
    candidates = [
        now,
        now - timedelta(hours=1),
        now + timedelta(hours=1),
    ]

    for dt in candidates:
        slug = build_slug(dt)
        market = get_market(slug)

        if market:
            return slug, market

    return None, None


def evaluate(btc, ref, yes, no):
    if btc > ref:
        edge = 0.75 - yes
        if edge > EDGE_THRESHOLD:
            return "BUY UP", edge

    elif btc < ref:
        edge = 0.75 - no
        if edge > EDGE_THRESHOLD:
            return "BUY DOWN", edge

    return None, 0


# ------------------------
# MAIN LOOP
# ------------------------
while True:
    try:
        btc = get_btc_price()
        now = datetime.now(ET)

        slug, market = find_market(now)

        if not market:
            print("No market found...")
            time.sleep(CHECK_SECONDS)
            continue

        if slug != current_slug:
            current_slug = slug
            hour_open_price = btc
            last_alert_side = None

            print("SWITCHED MARKET:", slug)
            print("Hour open:", hour_open_price)

            time.sleep(CHECK_SECONDS)
            continue

        yes, no = parse_prices(market["outcomePrices"])

        action, edge = evaluate(btc, hour_open_price, yes, no)

        print(
            "DEBUG:",
            btc,
            hour_open_price,
            yes,
            no,
            edge,
            action,
            slug,
        )

        now_ts = time.time()

        if (
            action
            and (now_ts - last_alert_time > COOLDOWN_SECONDS)
            and action != last_alert_side
        ):
            link = f"https://polymarket.com/event/{slug}"

            send_alert(
                f"🚨 MISPRICE\n"
                f"{action}\n"
                f"BTC: {btc}\n"
                f"Hour Open: {hour_open_price}\n"
                f"YES: {yes}\n"
                f"NO: {no}\n"
                f"Edge: {edge*100:.1f}¢\n"
                f"{link}"
            )

            last_alert_time = now_ts
            last_alert_side = action

        time.sleep(CHECK_SECONDS)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
