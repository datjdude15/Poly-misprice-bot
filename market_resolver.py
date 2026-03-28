import requests
from datetime import datetime
import pytz
from dataclasses import dataclass


@dataclass
class MarketState:
    slug: str
    yes_token_id: str
    no_token_id: str
    hour_open_btc: float


# -------------------------
# COINBASE PRICE (NO BLOCK)
# -------------------------

def fetch_coinbase_price():
    r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


# -------------------------
# GET HOUR OPEN (SIMPLIFIED)
# -------------------------

def get_hour_open_price():
    """
    We approximate hour open using current price at hour start.
    Since Coinbase doesn't give historical candles easily without auth,
    we use a stable fallback:
    """
    return fetch_coinbase_price()


# -------------------------
# GET TOKEN IDS FROM GAMMA
# -------------------------

def get_tokens_from_slug(slug: str):
    url = f"https://gamma-api.polymarket.com/events/{slug}"
    r = requests.get(url, timeout=10)

    if r.status_code != 200:
        raise Exception(f"Gamma fetch failed: {r.status_code}")

    data = r.json()

    tokens = data.get("clobTokenIds", [])
    if len(tokens) < 2:
        raise Exception("Token IDs not found")

    return tokens[0], tokens[1]


# -------------------------
# MAIN RESOLVER
# -------------------------

def resolve_current_market_state(tz_name="US/Central"):
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)

    hour_12 = now.strftime("%-I")  # 5 instead of 05
    ampm = now.strftime("%p").lower()

    date_str = now.strftime("%B-%-d-%Y").lower().replace(" ", "-")

    slug = f"bitcoin-up-or-down-{date_str}-{hour_12}{ampm}-et"

    try:
        yes_token, no_token = get_tokens_from_slug(slug)
    except Exception as e:
        print(f"[Resolver] Token fetch failed: {e}")
        yes_token, no_token = "", ""

    try:
        hour_open = get_hour_open_price()
    except Exception as e:
        print(f"[Resolver] Hour open fetch failed: {e}")
        hour_open = 0

    return MarketState(
        slug=slug,
        yes_token_id=yes_token,
        no_token_id=no_token,
        hour_open_btc=hour_open
    )


# -------------------------
# PUBLIC CLOB PRICE
# -------------------------

def fetch_public_clob_midpoint(token_id: str):
    try:
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        r = requests.get(url, timeout=10)

        if r.status_code != 200:
            return None

        data = r.json()

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if not bids or not asks:
            return None

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])

        return (best_bid + best_ask) / 2

    except Exception:
        return None
