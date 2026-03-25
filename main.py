import requests
import time
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

EDGE_THRESHOLD = 0.10        # 10 cents minimum edge
CHECK_SECONDS = 10
COOLDOWN_SECONDS = 180       # 3 minutes between alerts
MARKET_FETCH_LIMIT = 200

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


def list_active_markets() -> list:
    url = (
        "https://gamma-api.polymarket.com/markets"
        f"?active=true&closed=false&limit={MARKET_FETCH_LIMIT}"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def parse_prices(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    return float(raw[0]), float(raw[1])


def format_hourly_slug(dt_et: datetime) -> str:
    hour_12 = dt_et.hour % 12
    if hour_12 == 0:
        hour_12 = 12

    am_pm = "am" if dt_et.hour < 12 else "pm"
    month = dt_et.strftime("%B").lower()
    day = dt_et.day
    year = dt_et.year

    return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour_12}{am_pm}-et"


def get_candidate_slugs() -> list[str]:
    et_now = datetime.now(ZoneInfo("America/New_York"))
    prev_hour = et_now - timedelta(hours=1)
    next_hour = et_now + timedelta(hours=1)

    # Prefer current hour, then previous hour, then next hour if that's all that's live
    return [
        format_hourly_slug(et_now),
        format_hourly_slug(prev_hour),
        format_hourly_slug(next_hour),
    ]


def choose_market(markets: list):
    candidate_slugs = get_candidate_slugs()
    market_by_slug = {}

    for m in markets:
        slug = m.get("slug")
        if slug:
            market_by_slug[slug] = m

    for slug in candidate_slugs:
        if slug in market_by_slug:
            return slug, market_by_slug[slug]

    # Fallback: look for any active Bitcoin hourly market if exact match isn't found
    for m in markets:
        slug = (m.get("slug") or "").lower()
        question = (m.get("question") or "").lower()
        title = (m.get("title") or "").lower()

        if (
            "bitcoin-up-or-down" in slug
            or ("bitcoin" in question and "hourly" in question)
            or ("bitcoin" in title and "hourly" in title)
        ):
            return m.get("slug"), m

    return None, None


def evaluate(btc: float, ref: float, yes: float, no: float):
    if btc > ref:
        edge = 0.75 - yes
        if edge > EDGE_THRESHOLD:
            return "BUY UP", edge

    elif btc < ref:
        edge = 0.75 - no
        if edge > EDGE_THRESHOLD:
            return "BUY DOWN", edge

    return None, 0.0


while True:
    try:
        btc = get_btc_price()
        markets = list_active_markets()
        slug, market = choose_market(markets)

        if not market or not slug:
            print("No active market found...")
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
            "DEBUG |",
            "BTC:", btc,
            "OPEN:", hour_open_price,
            "YES:", yes,
            "NO:", no,
            "EDGE:", edge,
            "ACTION:", action,
            "SLUG:", slug,
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
