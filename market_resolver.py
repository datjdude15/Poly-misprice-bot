import json
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
PUBLIC_CLOB_BOOK_URL = "https://clob.polymarket.com/book"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


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
    month = now_local.strftime("%B").lower()
    day = now_local.day
    year = now_local.year
    hour_12, suffix = _to_12h(now_local.hour)
    return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour_12}{suffix}-et"


def get_current_market_hour_label(now_local: datetime) -> str:
    hour_12, suffix = _to_12h(now_local.hour)
    return f"{hour_12}{suffix.upper()} ET"


def fetch_coinbase_spot() -> float:
    resp = requests.get(COINBASE_SPOT_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return float(data["data"]["amount"])


def fetch_hour_open_btc() -> float:
    """
    Temporary practical fallback:
    use current Coinbase spot as hour_open proxy so the bot can run
    without Binance geo issues.

    This is enough to get the bot unstuck and operating.
    Later, if needed, we can upgrade this to a true candle-open source.
    """
    return fetch_coinbase_spot()


def fetch_all_markets() -> list[dict]:
    markets = []
    offset = 0
    limit = 100

    while True:
        resp = requests.get(
            GAMMA_MARKETS_URL,
            params={"limit": limit, "offset": offset},
            timeout=20,
        )
        resp.raise_for_status()
        batch = resp.json()

        if not isinstance(batch, list) or not batch:
            break

        markets.extend(batch)

        if len(batch) < limit:
            break

        offset += limit

        if offset >= 1000:
            break

    return markets


def _parse_clob_token_ids(market: dict) -> tuple[str, str]:
    raw_tokens = market.get("clobTokenIds", "")
    raw_outcomes = market.get("outcomes", "")

    if not raw_tokens:
        raise ValueError("Missing clobTokenIds")

    if isinstance(raw_tokens, str):
        token_ids = json.loads(raw_tokens)
    else:
        token_ids = raw_tokens

    if len(token_ids) < 2:
        raise ValueError("Expected at least 2 token IDs")

    outcomes = []
    if raw_outcomes:
        if isinstance(raw_outcomes, str):
            outcomes = json.loads(raw_outcomes)
        elif isinstance(raw_outcomes, list):
            outcomes = raw_outcomes

    # Prefer explicit mapping for BTC hourly markets: ["Up","Down"]
    if len(outcomes) >= 2:
        first = str(outcomes[0]).strip().lower()
        second = str(outcomes[1]).strip().lower()

        if first == "up" and second == "down":
            return str(token_ids[0]), str(token_ids[1])

        if first == "yes" and second == "no":
            return str(token_ids[0]), str(token_ids[1])

    # Fallback to API order
    return str(token_ids[0]), str(token_ids[1])


def get_tokens_from_slug(slug: str) -> tuple[str, str]:
    markets = fetch_all_markets()

    exact_match = None
    partial_match = None

    for market in markets:
        market_slug = str(market.get("slug", "")).strip().lower()

        if market_slug == slug.lower():
            exact_match = market
            break

        if slug.lower() in market_slug:
            partial_match = market

    chosen = exact_match or partial_match
    if not chosen:
        raise Exception(f"Market not found for slug: {slug}")

    return _parse_clob_token_ids(chosen)


def resolve_current_market_state(tz_name: str = "US/Central") -> MarketState:
    local_tz = ZoneInfo(tz_name)
    now_local = datetime.now(local_tz)

    slug = build_current_btc_hourly_slug(now_local)
    yes_token_id, no_token_id = get_tokens_from_slug(slug)
    hour_open_btc = fetch_hour_open_btc()
    market_hour_label = get_current_market_hour_label(now_local)

    return MarketState(
        slug=slug,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        hour_open_btc=hour_open_btc,
        market_hour_label=market_hour_label,
    )


def fetch_public_clob_midpoint(token_id: str) -> float | None:
    if not token_id:
        return None

    try:
        resp = requests.get(
            PUBLIC_CLOB_BOOK_URL,
            params={"token_id": token_id},
            timeout=15,
        )
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

    except Exception:
        return None
