import requests
import time
import os
import json
from datetime import datetime

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Update this to the current live hourly market slug
POLY_SLUG = "bitcoin-up-or-down-march-25-2026-4pm-et"

# Tuned settings
EDGE_THRESHOLD = 0.10        # 10 cents minimum edge
CHECK_SECONDS = 10
COOLDOWN_SECONDS = 180       # 3 minutes between alerts

hour_open_price = None
last_alert_time = 0
last_alert_side = None


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
    if isinstance(raw_value, str):
        parsed = json.loads(raw_value)
    else:
        parsed = raw_value
    return [float(parsed[0]), float(parsed[1])]


def get_current_hour_key() -> str:
    now = datetime.utcnow()
    return now.strftime("%Y-%m-%d-%H")


def evaluate_misprice(
    btc_price: float,
    reference_price: float,
    yes_price: float,
    no_price: float,
) -> tuple[str | None, float]:
    if btc_price > reference_price:
        edge = 0.75 - yes_price
        if edge > EDGE_THRESHOLD:
            return "BUY UP", edge

    elif btc_price < reference_price:
        edge = 0.75 - no_price
        if edge > EDGE_THRESHOLD:
            return "BUY DOWN", edge

    return None, 0.0


current_hour_key = None

while True:
    try:
        btc_price = get_btc_price()
        market = get_market()

        outcome_prices = parse_outcome_prices(market["outcomePrices"])
        yes_price = outcome_prices[0]
        no_price = outcome_prices[1]

        new_hour_key = get_current_hour_key()

        if current_hour_key != new_hour_key or hour_open_price is None:
            current_hour_key = new_hour_key
            hour_open_price = btc_price
            print("New hour reference price set:", hour_open_price)
            time.sleep(CHECK_SECONDS)
            continue

        action, edge = evaluate_misprice(
            btc_price,
            hour_open_price,
            yes_price,
            no_price,
        )

        print(
            "DEBUG |",
            "BTC:", btc_price,
            "HOUR_OPEN:", hour_open_price,
            "YES:", yes_price,
            "NO:", no_price,
            "EDGE:", round(edge, 4),
            "ACTION:", action,
        )

        now_ts = time.time()

        if (
            action
            and (now_ts - last_alert_time) > COOLDOWN_SECONDS
            and action != last_alert_side
        ):
            send_alert(
                f"🚨 MISPRICE\n"
                f"{action}\n"
                f"BTC: {btc_price}\n"
                f"Hour Open: {hour_open_price}\n"
                f"YES: {yes_price}\n"
                f"NO: {no_price}\n"
                f"Edge: {edge*100:.1f}¢"
            )
            last_alert_time = now_ts
            last_alert_side = action

        time.sleep(CHECK_SECONDS)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
