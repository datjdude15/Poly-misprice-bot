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


def parse_prices(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    return float(raw[0]), float(raw[1])


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("  ", " ")
    return text.strip()


def hour_to_12(hour_24: int) -> int:
    hour_12 = hour_24 % 12
    return 12 if hour_12 == 0 else hour_12


def am_pm(hour_24: int) -> str:
    return "am" if hour_24 < 12 else "pm"


def build_slug(dt_et: datetime) -> str:
    month = dt_et.strftime("%B").lower()
    day = dt_et.day
    year = dt_et.year
    start_hour = hour_to_12(dt_et.hour)
    suffix = am_pm(dt_et.hour)
    return f"bitcoin-up-or-down-{month}-{day}-{year}-{start_hour}{suffix}-et"


def build_candidate_times(now_et: datetime):
    return [
        now_et,
        now_et - timedelta(hours=1),
        now_et + timedelta(hours=1),
    ]


def get_market_by_slug(slug: str):
    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        r = requests.get(url, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def market_text_blob(market: dict) -> str:
    parts = [
        market.get("question", ""),
        market.get("title", ""),
        market.get("description", ""),
        market.get("slug", ""),
    ]
    return normalize_text(" | ".join(str(p) for p in parts if p))


def expected_window_variants(now_et: datetime):
    start_hour_24 = now_et.hour
    end_hour_24 = (now_et.hour + 1) % 24

    start_12 = hour_to_12(start_hour_24)
    end_12 = hour_to_12(end_hour_24)

    # Polymarket usually words it like "7-8pm ET"
    # We intentionally keep the suffix on the END hour only.
    end_suffix = am_pm(end_hour_24)

    month_name = normalize_text(now_et.strftime("%B"))
    day_num = str(now_et.day)

    window_core_1 = f"{start_12}-{end_12}{end_suffix} et"
    window_core_2 = f"{start_12} - {end_12}{end_suffix} et"
    window_core_3 = f"{start_12}-{end_12} {end_suffix} et"
    window_core_4 = f"{start_12} - {end_12} {end_suffix} et"

    date_core_1 = f"{month_name} {day_num}"
    date_core_2 = f"{month_name} {day_num},"

    return {
        "window_variants": [
            window_core_1,
            window_core_2,
            window_core_3,
            window_core_4,
        ],
        "date_variants": [
            date_core_1,
            date_core_2,
        ],
    }


def validate_market_for_current_hour(market: dict, now_et: datetime):
    blob = market_text_blob(market)
    expected = expected_window_variants(now_et)

    has_identity = (
        "bitcoin up or down" in blob
        or "bitcoin-up-or-down" in blob
    ) and "hourly" in blob

    has_date = any(d in blob for d in expected["date_variants"])
    has_window = any(w in blob for w in expected["window_variants"])

    return has_identity and has_date and has_window


def find_valid_market(now_et: datetime):
    candidate_times = build_candidate_times(now_et)

    # Try current, previous, next slug — but validate against CURRENT hour wording
    for dt_candidate in candidate_times:
        slug = build_slug(dt_candidate)
        market = get_market_by_slug(slug)
        if not market:
            continue

        if validate_market_for_current_hour(market, now_et):
            return slug, market

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
        now_et = datetime.now(ET)

        slug, market = find_valid_market(now_et)

        if not market or not slug:
            print("No validated current-hour market found...")
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
            "EDGE:", round(edge, 4),
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
