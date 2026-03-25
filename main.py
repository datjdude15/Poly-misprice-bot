import requests
import time
import os
import json
from datetime import datetime

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Settings
EDGE_THRESHOLD = 0.10
CHECK_SECONDS = 10
COOLDOWN_SECONDS = 180

hour_open_price = None
last_alert_time = 0
last_alert_side = None
current_slug = None


def send_alert(message: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message},
        timeout=15,
    )


def get_btc_price() -> float:
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


def build_slug():
    now = datetime.utcnow()

    hour_12 = now.hour % 12
    if hour_12 == 0:
        hour_12 = 12

    am_pm = "am" if now.hour < 12 else "pm"

    return f"bitcoin-up-or-down-{now.strftime('%B').lower()}-{now.day}-{now.year}-{hour_12}{am_pm}-et"


def get_market(slug):
    url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def parse_prices(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    return float(raw[0]), float(raw[1])


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

        slug = build_slug()

        # Detect new hour / new market
        if slug != current_slug:
            current_slug = slug
            hour_open_price = btc
            print("NEW MARKET:", slug)
            print("Hour open set:", hour_open_price)
            time.sleep(CHECK_SECONDS)
            continue

        market = get_market(slug)
        yes, no = parse_prices(market["outcomePrices"])

        action, edge = evaluate(btc, hour_open_price, yes, no)

        print("DEBUG | BTC:", btc, "OPEN:", hour_open_price, "YES:", yes, "NO:", no, "EDGE:", edge, "ACTION:", action)

        now_ts = time.time()

        if action and (now_ts - last_alert_time > COOLDOWN_SECONDS) and action != last_alert_side:
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
