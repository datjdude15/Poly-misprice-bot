import json
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


GAMMA_SLUG_URL = "https://gamma-api.polymarket.com/markets/slug/{slug}"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
PUBLIC_CLOB_BOOK_URL = "https://clob.polymarket.com/book"


@dataclass
class MarketState:
    slug: str
    yes_token_id: str
    no_token_id: str
    hour_open_btc: float
    market_hour_label: str


def _to_12h(hour_24: int) -> tuple[int, str]:
    if hour_24 == 0:
        return 12, "am"
    if 1 <= hour_24 < 12:
        return hour_24, "am"
    if hour_24 == 12:
        return 12, "pm"
    return hour_24 - 12, "pm"


def build_current_btc_hourly_slug(now_local: datetime) -> str:
    month = now_local.strftime("%B")
    day = now_local.day
    year = now_local.year
    hour_12, suffix = _to_12h(now_local.hour)
    return f"bitcoin-up-or-down-{month.lower()}-{day}-{year}-{hour_12}{suffix}-et"


def get_current_market_hour_label(now_local: datetime) -> str:
    hour_12, suffix = _to_12h(now_local.hour)
    return f"{hour_12}{suffix.upper()} ET"


def fetch_market_by_slug(slug: str) -> dict:
    url = GAMMA_SLUG_URL.format(slug=slug)
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_clob_token_ids(raw: dict) -> tuple[str, str]:
    clob_token_ids_raw = raw.get("clobTokenIds", "")
    outcomes_raw = raw.get("outcomes", "")

    if not clob_token_ids_raw:
        raise ValueError("Missing clobTokenIds in Polymarket response.")

    token_ids = json.loads(clob_token_ids_raw)
    outcomes = json.loads(outcomes_raw) if outcomes_raw else []

    if len(token_ids) != 2:
        raise ValueError(f"Expected 2 token IDs, got {len(token_ids)}.")

    if outcomes and len(outcomes) == 2:
        # For BTC hourly markets, outcomes are typically ["Up", "Down"].
        # First token maps to first outcome; second token maps to second outcome.
        first_outcome = str(outcomes[0]).strip().lower()
        second_outcome = str(outcomes[1]).strip().lower()

        if first_outcome == "up" and second_outcome == "down":
            return token_ids[0], token_ids[1]

        if first_outcome == "yes" and second_outcome == "no":
            return token_ids[0], token_ids[1]

    # Fallback: preserve API order
    return token_ids[0], token_ids[1]


def fetch_binance_hour_open_btc(now_utc: datetime) -> float:
    # Start of current UTC hour
    hour_start = now_utc.replace(minute=0, second=0, microsecond=0)
    start_ms = int(hour_start.timestamp() * 1000)

    params = {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "startTime": start_ms,
        "limit": 1,
    }

    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if not data or not isinstance(data, list):
        raise ValueError("No Binance kline data returned.")

    kline = data[0]
    open_price = float(kline[1])
    return open_price


def resolve_current_market_state(tz_name: str = "US/Central") -> MarketState:
    local_tz = ZoneInfo(tz_name)
    now_local = datetime.now(local_tz)
    now_utc = datetime.now(timezone.utc)

    slug = build_current_btc_hourly_slug(now_local)
    raw = fetch_market_by_slug(slug)
    yes_token_id, no_token_id = parse_clob_token_ids(raw)
    hour_open_btc = fetch_binance_hour_open_btc(now_utc)
    market_hour_label = get_current_market_hour_label(now_local)

    return MarketState(
        slug=slug,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        hour_open_btc=hour_open_btc,
        market_hour_label=market_hour_label,
    )


def fetch_public_clob_midpoint(token_id: str) -> float | None:
    params = {"token_id": token_id}
    resp = requests.get(PUBLIC_CLOB_BOOK_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    bids = data.get("bids", [])
    asks = data.get("asks", [])

    best_bid = None
    best_ask = None

    if bids:
        best_bid = max(float(x["price"]) for x in bids if "price" in x)

    if asks:
        best_ask = min(float(x["price"]) for x in asks if "price" in x)

    if best_bid is not None and best_ask is not None:
        return round((best_bid + best_ask) / 2, 4)

    if best_bid is not None:
        return round(best_bid, 4)

    if best_ask is not None:
        return round(best_ask, 4)

    return None
