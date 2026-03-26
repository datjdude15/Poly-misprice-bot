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
EDGE_THRESHOLD = 0.15          # 15 cents minimum edge
MOVE_THRESHOLD = 15.0          # BTC must be at least $15 away from hour open
CHECK_SECONDS = 10             # how often to check
COOLDOWN_SECONDS = 180         # wait 3 min between alerts
NO_TRADE_MINUTES = 5           # ignore first 5 minutes of each new hour
CONFIRMATION_CHECKS = 2        # require same signal twice in a row

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
    requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message},
        timeout=15
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


def hour_to_12(h: int) -> int:
    h = h % 12
    return 12 if h == 0 else h


def ampm(h: int) -> str:
    return "am" if h < 12 else "pm"


def build_slug(dt):
    month = dt.strftime("%B").lower()
    day = dt.day
    year = dt.year
    hour = hour_to_12(dt.hour)
    suffix = ampm(dt.hour)
    return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour}{suffix}-et"


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


def in_no_trade_window(now_et: datetime) -> bool:
    if hour_started_at is None:
        return True
    seconds_since_hour_start = (now_et - hour_started_at).total_seconds()
    return seconds_since_hour_start < (NO_TRADE_MINUTES * 60)


# =========================
# FILTERED EVALUATION
# =========================
def evaluate_signal(btc: float, hour_open: float, yes: float, no: float):
    move = btc - hour_open
    abs_move = abs(move)

    # 1) Require meaningful move from hour open
    if abs_move < MOVE_THRESHOLD:
        return None, 0.0, move, "MOVE_TOO_SMALL"

    # 2) Direction filter
    if move > 0:
        edge = 0.75 - yes
        if edge >= EDGE_THRESHOLD:
            return "BUY UP", edge, move, "VALID_UP"
        return None, edge, move, "EDGE_TOO_SMALL_UP"

    if move < 0:
        edge = 0.75 - no
        if edge >= EDGE_THRESHOLD:
            return "BUY DOWN", edge, move, "VALID_DOWN"
        return None, edge, move, "EDGE_TOO_SMALL_DOWN"

    return None, 0.0, move, "NO_SIGNAL"


# =========================
# MAIN LOOP
# =========================
while True:
    try:
        btc = get_btc_price()
        now_et = datetime.now(ET)

        slug, market = find_market(now_et)

        if not market:
            print("No market found...")
            time.sleep(CHECK_SECONDS)
            continue

        # Switch to new market
        if slug != current_slug:
            current_slug = slug
            hour_open_price = btc
            hour_started_at = now_et.replace(minute=0, second=0, microsecond=0)

            last_alert_side = None
            pending_action = None
            pending_count = 0

            print("SWITCHED MARKET:", slug)
            print("Hour open:", hour_open_price)
            print("Hour started at:", hour_started_at)

            time.sleep(CHECK_SECONDS)
            continue

        yes, no = parse_prices(market["outcomePrices"])

        action, edge, move, reason = evaluate_signal(
            btc=btc,
            hour_open=hour_open_price,
            yes=yes,
            no=no
        )

        # 3) No-trade window after new hour starts
        if in_no_trade_window(now_et):
            print(
                "DEBUG |",
                "BTC:", btc,
                "OPEN:", hour_open_price,
                "MOVE:", round(move, 2),
                "YES:", yes,
                "NO:", no,
                "EDGE:", round(edge, 4),
                "ACTION:", action,
                "REASON:", "NO_TRADE_WINDOW",
                "SLUG:", slug,
            )
            pending_action = None
            pending_count = 0
            time.sleep(CHECK_SECONDS)
            continue

        # 4) Persistence confirmation
        if action:
            if action == pending_action:
                pending_count += 1
            else:
                pending_action = action
                pending_count = 1
        else:
            pending_action = None
            pending_count = 0

        confirmed = action is not None and pending_count >= CONFIRMATION_CHECKS

        print(
            "DEBUG |",
            "BTC:", btc,
            "OPEN:", hour_open_price,
            "MOVE:", round(move, 2),
            "YES:", yes,
            "NO:", no,
            "EDGE:", round(edge, 4),
            "ACTION:", action,
            "PENDING:", pending_action,
            "COUNT:", pending_count,
            "CONFIRMED:", confirmed,
            "REASON:", reason,
            "SLUG:", slug,
        )

        now_ts = time.time()

        if (
            confirmed
            and (now_ts - last_alert_time > COOLDOWN_SECONDS)
            and action != last_alert_side
        ):
            link = f"https://polymarket.com/event/{slug}"

            send_alert(
                f"🚨 MISPRICE\n"
                f"{action}\n"
                f"BTC: {btc}\n"
                f"Hour Open: {hour_open_price}\n"
                f"Move: {move:.2f}\n"
                f"YES: {yes}\n"
                f"NO: {no}\n"
                f"Edge: {edge*100:.1f}¢\n"
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
