import requests
import time
import os
import json

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Change this each hour to the live hourly market you want to track
POLY_SLUG = "bitcoin-up-or-down-march-25-2026-2pm-et"

# Settings
MOVE_THRESHOLD = 1.0       # BTC must move at least this much vs reference
EDGE_THRESHOLD = 0.02      # 2 cents edge minimum
CHECK_SECONDS = 10
COOLDOWN_SECONDS = 60      # minimum time between alerts

last_price = None
last_alert_time = 0


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
    data = r.json()
    return float(data["data"]["amount"])


def get_market() -> dict:
    url = f"https://gamma-api.polymarket.com/markets/slug/{POLY_SLUG}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def parse_outcome_prices(raw_value) -> list[float]:
    # Sometimes Polymarket returns outcomePrices as a JSON string like "[0.52,0.48]"
    # and sometimes it may already be a list.
    if isinstance(raw_value, str):
        parsed = json.loads(raw_value)
    else:
        parsed = raw_value

    return [float(parsed[0]), float(parsed[1])]


def evaluate_misprice(
    btc_price: float,
    reference_price: float,
    yes_price: float,
    no_price: float,
) -> tuple[str | None, float]:
    if btc_price > reference_price + MOVE_THRESHOLD:
        edge = 0.75 - yes_price
        if edge > EDGE_THRESHOLD:
            return "BUY UP", edge

    elif btc_price < reference_price - MOVE_THRESHOLD:
        edge = 0.75 - no_price
        if edge > EDGE_THRESHOLD:
            return "BUY DOWN", edge

    return None, 0.0


while True:
    try:
        btc_price = get_btc_price()
        market = get_market()

        outcome_prices = parse_outcome_prices(market["outcomePrices"])
        yes_price = outcome_prices[0]
        no_price = outcome_prices[1]

        if last_price is None:
            last_price = btc_price
            print("Starting reference price:", last_price)
            time.sleep(CHECK_SECONDS)
            continue

        action, edge = evaluate_misprice(
            btc_price,
            last_price,
            yes_price,
            no_price,
        )

        print(
            "DEBUG |",
            "BTC:", btc_price,
            "REF:", last_price,
            "YES:", yes_price,
            "NO:", no_price,
            "EDGE:", round(edge, 4),
            "ACTION:", action,
        )

        now = time.time()

        if action and (now - last_alert_time) > COOLDOWN_SECONDS:
            send_alert(
                f"🚨 MISPRICE\n"
                f"{action}\n"
                f"BTC: {btc_price}\n"
                f"Reference: {last_price}\n"
                f"YES: {yes_price}\n"
                f"NO: {no_price}\n"
                f"Edge: {edge*100:.1f}¢"
            )
            last_alert_time = now

        last_price = btc_price
        time.sleep(CHECK_SECONDS)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
