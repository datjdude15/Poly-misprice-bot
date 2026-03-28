import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
PUBLIC_CLOB_BOOK_URL = "https://clob.polymarket.com/book"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")


@dataclass
class MarketState:
    slug: str
    yes_token_id: str
    no_token_id: str
    hour_open_btc: float
    market_hour_label: str


def _to_12h(hour: int) -> tuple[int, str]:
    if hour == 0:
        return 12, "am"
    if hour < 12:
        return hour, "am"
    if hour == 12:
        return 12, "pm"
    return hour - 12, "pm"


def build_btc_hourly_slug(dt_et: datetime) -> str:
    month = dt_et.strftime("%B").lower()
    day = dt_et.day
    year = dt_et.year
    hour_12, suffix = _to_12h(dt_et.hour)
    return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour_12}{suffix}-et"


def get_market_hour_label(dt_et: datetime) -> str:
    hour_12, suffix = _to_12h(dt_et.hour)
    return f"{hour_12}{suffix.upper()} ET"


def fetch_coinbase_spot() -> float:
    resp = requests.get(COINBASE_SPOT_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return float(data["data"]["amount"])


def fetch_hour_open_btc() -> float:
    """
    Temporary fallback so the bot can run without Binance.
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

        if offset >= 2000:
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

    if len(outcomes) >= 2:
        first = str(outcomes[0]).strip().lower()
        second = str(outcomes[1]).strip().lower()

        if first in ("up", "yes") and second in ("down", "no"):
            return str(token_ids[0]), str(token_ids[1])

        if first in ("down", "no") and second in ("up", "yes"):
            return str(token_ids[1]), str(token_ids[0])

    return str(token_ids[0]), str(token_ids[1])


def get_tokens_from_slug(slug: str, markets: list[dict]) -> tuple[str, str]:
    for market in markets:
        market_slug = str(market.get("slug", "")).strip().lower()
        if market_slug == slug.lower():
            return _parse_clob_token_ids(market)

    raise Exception(f"Market not found for slug: {slug}")


def resolve_current_market_state(tz_name: str = "US/Central") -> MarketState:
    """
    Public function expected by bot.py.
    Uses UTC -> ET conversion to avoid double-offset timezone bugs.
    Floors to the current ET hour so 6:55pm ET resolves 6pm ET market.
    Tries current hour first, then previous hour as fallback.
    """
    now_utc = datetime.now(UTC)
    now_et = now_utc.astimezone(ET)
    now_et = now_et.replace(minute=0, second=0, microsecond=0)

    markets = fetch_all_markets()

    candidate_times = [
        now_et,
        now_et - timedelta(hours=1),
    ]

    last_error = None

    for candidate_dt in candidate_times:
        slug = build_btc_hourly_slug(candidate_dt)

        try:
            yes_token_id, no_token_id = get_tokens_from_slug(slug, markets)
            hour_open_btc = fetch_hour_open_btc()
            market_hour_label = get_market_hour_label(candidate_dt)

            print(f"[RESOLVER] Resolved slug -> {slug}")
            print(f"[RESOLVER] YES token -> {yes_token_id}")
            print(f"[RESOLVER] NO token -> {no_token_id}")
            print(f"[RESOLVER] Hour open BTC -> {hour_open_btc}")

            return MarketState(
                slug=slug,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                hour_open_btc=hour_open_btc,
                market_hour_label=market_hour_label,
            )
        except Exception as e:
            last_error = e

    raise Exception(str(last_error) if last_error else "Unable to resolve market state")


def fetch_public_clob_midpoint(token_id: str) -> float | None:
    """
    Public function expected by bot.py.
    """
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
            bid_prices = [float(x["price"]) for x in bids if "price" in x]
            if bid_prices:
                best_bid = max(bid_prices)

        if asks:
            ask_prices = [float(x["price"]) for x in asks if "price" in x]
            if ask_prices:
                best_ask = min(ask_prices)

        if best_bid is not None and best_ask is not None:
            return round((best_bid + best_ask) / 2, 4)

        if best_bid is not None:
            return round(best_bid, 4)

        if best_ask is not None:
            return round(best_ask, 4)

        return None

    except Exception:
        return None
