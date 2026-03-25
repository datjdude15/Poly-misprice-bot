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

hour_open_price = None
last_alert_time = 0
last_alert_side = None
current_slug = None


def send_alert(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)


def get_btc_price():
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


def try_get_market(slug):
    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        r = requests.get(url, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except:
        return None


def parse_prices(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    return float(raw[0]), float(raw[1])


def format_slug(dt):
    hour_12 = dt.hour % 12
    if hour_12 == 0:
        hour_12 = 12

    am_pm = "am" if dt.hour < 12 else "pm"
    month = dt.strftime("%B").lower()
    day = dt.day
    year = dt.year

    return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour_12}{am_pm}-et"


def get_valid_market():
    et_now = datetime.now(ZoneInfo("America/New_York"))

    current_slug = format_slug(et_now)
    market = try_get_market(current_slug)

    if market:
        return current_slug, market

    # fallback to previous hour
    prev_time = et_now - timedelta(hours=1)
    prev_slug = format_slug(prev_time)
    market = try_get_market(prev_slug)

    if market:
        return prev_slug, market

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


while True:
    try:
        btc = get_btc_price()

        slug, market = get_valid_market()

        if not market:
            print("No active market yet...")
            time.sleep(10)
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
            "DEBUG | BTC:", btc,
            "OPEN:", hour_open_price,
            "YES:", yes,
            "NO:", no,
            "EDGE:", edge,
            "ACTION:", action,
            "SLUG:", slug
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
