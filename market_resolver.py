import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


GAMMA_MARKET_BY_SLUG_URL = "https://gamma-api.polymarket.com/markets/slug/{slug}"
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
    return fetch_coinbase_spot()


def fetch_market_by_slug(slug: str) -> dict:
    url = GAMMA_MARKET_BY_SLUG_URL.format(slug=slug)
    resp = requests.get(url, timeout=20)

    if resp.status_code == 404:
        raise Exception(f"Market not found for slug: {slug}")

    resp.raise_for_status()
    return resp.json()


def fetch_active_markets(limit: int = 2000) -> list[dict]:
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
    }
    resp = requests.get(GAMMA_MARKETS_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


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


def _is_matching_btc_hourly_market(market: dict, candidate_dt: datetime) -> bool:
    slug = str(market.get("slug", "")).strip().lower()
    question = str(
        market.get("question")
        or market.get("title")
        or market.get("name")
        or ""
    ).strip().lower()

    month_name = candidate_dt.strftime("%B").lower()
    day = candidate_dt.day
    year = candidate_dt.year
    hour_12, suffix = _to_12h(candidate_dt.hour)

    expected_slug = build_btc_hourly_slug(candidate_dt)

    # Exact slug match first
    if slug == expected_slug:
        return True

    # Must be BTC/Bitcoin up-down style market
    combined = f"{slug} {question}"

    if "bitcoin" not in combined and "btc" not in combined:
        return False

    if "up or down" not in combined:
        return False

    # Exclude non-hourly BTC products
    if "4 hour" in combined or "4h" in combined:
        return False
    if "5 minute" in combined or "5m" in combined:
        return False
    if "15 minute" in combined or "15m" in combined:
        return False

    # Require correct date
    if month_name not in combined:
        return False
    if str(day) not in combined:
        return False
    if str(year) not in combined:
        return False

    # Require correct hour label in either slug or title
    hour_bits = [
        f"{hour_12}{suffix}",
        f"{hour_12}:00{suffix}",
        f"{hour_12} {suffix}",
        f"{hour_12}:00 {suffix}",
        f"{hour_12}{suffix}-et",
        f"{hour_12}:00{suffix}-et",
        f"{hour_12}{suffix} et",
        f"{hour_12}:00{suffix} et",
        f"{hour_12}:00 {suffix} et",
    ]

    if any(bit in combined for bit in hour_bits):
        return True

    return False


def _resolve_from_active_market_scan(candidate_times: list[datetime]) -> tuple[dict, datetime]:
    markets = fetch_active_markets(limit=2000)

    btc_like = []
    for market in markets:
        slug = str(market.get("slug", "")).strip().lower()
        question = str(
            market.get("question")
            or market.get("title")
            or market.get("name")
            or ""
        ).strip().lower()

        combined = f"{slug} | {question}"
        if "bitcoin" in combined or "btc" in combined:
            if "up or down" in combined:
                btc_like.append(combined)

    print(f"[RESOLVER] Active BTC-like markets found: {len(btc_like)}")
    for sample in btc_like[:15]:
        print(f"[RESOLVER] BTC MARKET SAMPLE -> {sample}")

    for candidate_dt in candidate_times:
        for market in markets:
            if _is_matching_btc_hourly_market(market, candidate_dt):
                return market, candidate_dt

    raise Exception("No matching active BTC hourly market found from active market scan")


def resolve_current_market_state(tz_name: str = "US/Central") -> MarketState:
    """
    Public function expected by bot.py.

    Resolution order:
    1) Try direct slug lookup for likely hour buckets
    2) Fall back to active-market scan if slug lookup misses
    """
    now_utc = datetime.now(UTC)
    now_et = now_utc.astimezone(ET)
    base_et = now_et.replace(minute=0, second=0, microsecond=0)

    candidate_times = [
        base_et,
        base_et + timedelta(hours=1),
        base_et - timedelta(hours=1),
    ]

    last_error = None

    # First pass: direct slug lookups
    for candidate_dt in candidate_times:
        slug = build_btc_hourly_slug(candidate_dt)
        try:
            print(f"[RESOLVER] Trying slug -> {slug}")
            market = fetch_market_by_slug(slug)
            yes_token_id, no_token_id = _parse_clob_token_ids(market)
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
            print(f"[RESOLVER] Direct slug miss -> {slug} | {e}")
            last_error = e

    # Second pass: scan active markets and match the real live hourly market
    try:
        market, matched_dt = _resolve_from_active_market_scan(candidate_times)
        slug = str(market.get("slug", "")).strip()
        yes_token_id, no_token_id = _parse_clob_token_ids(market)
        hour_open_btc = fetch_hour_open_btc()
        market_hour_label = get_market_hour_label(matched_dt)

        print(f"[RESOLVER] Active-scan resolved slug -> {slug}")
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
