import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
PUBLIC_CLOB_BOOK_URL = "https://clob.polymarket.com/book"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

# ALWAYS use UTC → convert to ET (prevents double offset bug)
UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")


@dataclass
class MarketState:
    slug: str
    yes_token_id: str
    no_token_id: str
    hour_open_btc: float
    market_hour_label: str


def _to_12h(hour):
    if hour == 0:
        return 12, "am"
    if hour < 12:
        return hour, "am"
    if hour == 12:
        return 12, "pm"
    return hour - 12, "pm"


def build_slug(dt_et):
    month = dt_et.strftime("%B").lower()
    day = dt_et.day
    year = dt_et.year
    hour_12, suffix = _to_12h(dt_et.hour)

    return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour_12}{suffix}-et"


def fetch_coinbase_price():
    r = requests.get(COINBASE_SPOT_URL, timeout=10)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


def fetch_hour_open():
    # simple stable fallback
    return fetch_coinbase_price()


def fetch_all_markets():
    markets = []
    offset = 0

    while True:
        r = requests.get(
            GAMMA_MARKETS_URL,
            params={"limit": 100, "offset": offset},
            timeout=15
        )
        r.raise_for_status()

        batch = r.json()
        if not batch:
            break

        markets.extend(batch)

        if len(batch) < 100:
            break

        offset += 100

    return markets


def parse_tokens(market):
    tokens = json.loads(market["clobTokenIds"])
    outcomes = json.loads(market["outcomes"])

    # ensure correct mapping
    if outcomes[0].lower() in ["up", "yes"]:
        return tokens[0], tokens[1]
    else:
        return tokens[1], tokens[0]


def find_market(slug, markets):
    for m in markets:
        if m.get("slug") == slug:
            return m

    raise Exception(f"Market not found for slug: {slug}")


def resolve_market():
    # 🔥 FIX: ALWAYS start from UTC, then convert to ET
    now_utc = datetime.now(UTC)
    now_et = now_utc.astimezone(ET)

    # 🔥 FIX: floor to current hour
    now_et = now_et.replace(minute=0, second=0, microsecond=0)

    markets = fetch_all_markets()

    # try current, previous hour
    candidates = [
        now_et,
        now_et - timedelta(hours=1),
    ]

    for dt in candidates:
        slug = build_slug(dt)

        try:
            market = find_market(slug, markets)
            yes, no = parse_tokens(market)

            print(f"[RESOLVED] Using slug: {slug}")

            return MarketState(
                slug=slug,
                yes_token_id=yes,
                no_token_id=no,
                hour_open_btc=fetch_hour_open(),
                market_hour_label=f"{_to_12h(dt.hour)[0]}{_to_12h(dt.hour)[1].upper()} ET"
            )

        except Exception:
            continue

    raise Exception("No valid market found")


def fetch_midpoint(token_id):
    try:
        r = requests.get(
            PUBLIC_CLOB_BOOK_URL,
            params={"token_id": token_id},
            timeout=10
        )
        r.raise_for_status()

        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if bids and asks:
            bid = max(float(b["price"]) for b in bids)
            ask = min(float(a["price"]) for a in asks)
            return round((bid + ask) / 2, 4)

        if bids:
            return max(float(b["price"]) for b in bids)

        if asks:
            return min(float(a["price"]) for a in asks)

        return None

    except:
        return None
