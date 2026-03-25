import requests
import time
import os
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


# ----------------------------
# BUILD HOURLY SLUG
# ----------------------------
def build_slug(dt):
    hour = dt.hour % 12
    if hour == 0:
        hour = 12

    ampm = "am" if dt.hour < 12 else "pm"
    month = dt.strftime("%B").lower()
    day = dt.day
    year = dt.year

    return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour}{ampm}-et"


# ----------------------------
# TRY DIRECT SLUG FIRST
# ----------------------------
def get_market_by_slug(slug):
    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except:
        return None


# ----------------------------
# FALLBACK DISCOVERY
# ----------------------------
def discover_market():
    try:
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        markets = r.json()

        for m in markets:
            slug = (m.get("slug") or "").lower()
            if "bitcoin-up-or-down" in slug:
                return m

    except Exception as e:
        print("DISCOVERY ERROR:", e)

    return None


# ----------------------------
# PARSE PRICES SAFELY
# ----------------------------
def parse_prices(raw):
    if isinstance(raw, str):
        import json
        raw = json.loads(raw)

    return float(raw[0]), float(raw[1])


# ----------------------------
# EDGE LOGIC
# ----------------------------
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


# ----------------------------
# MAIN LOOP
# ----------------------------
while True:
    try:
        btc = get_btc_price()

        et_now = datetime.now(ZoneInfo("America/New_York"))

        candidate_slugs = [
            build_slug(et_now),
            build_slug(et_now - timedelta(hours=1)),
            build_slug(et_now + timedelta(hours=1)),
        ]

        market = None
        slug = None

        # 🔥 STEP 1: Try direct slugs
        for s in candidate_slugs:
            m = get_market_by_slug(s)
            if m:
                market = m
                slug = s
                break

        # 🔥 STEP 2: fallback to discovery
        if not market:
            print("Falling back to discovery...")
            m = discover_market()
            if m:
                market = m
                slug = m.get("slug")

        if not market:
            print("No active market found...")
            time.sleep(CHECK_SECONDS)
            continue

        # 🔥 MARKET SWITCH DETECTED
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
            "DEBUG |",
            "BTC:", btc,
            "OPEN:", hour_open_price,
            "YES:", yes,
            "NO:", no,
            "EDGE:", edge,
            "ACTION:", action,
        )

        now = time.time()

        if (
            action
            and (now - last_alert_time > COOLDOWN_SECONDS)
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

            last_alert_time = now
            last_alert_side = action

        time.sleep(CHECK_SECONDS)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
