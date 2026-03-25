import requests
import time
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

POLY_SLUG = "bitcoin-up-or-down-march-25-2026-2pm-et"

def send_alert(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)

def get_btc_price():
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    return float(data["data"]["amount"])

def get_market():
    url = f"https://gamma-api.polymarket.com/markets/slug/{POLY_SLUG}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

def evaluate_misprice(btc_price, reference_price, yes_price, no_price):
    if btc_price > reference_price + 0.25:
        edge = 0.75 - yes_price
        if edge > 0:
            return "BUY UP", edge

    elif btc_price < reference_price - 0.25:
        edge = 0.75 - no_price
        if edge > 0:
            return "BUY DOWN", edge

    return None, 0

send_alert("🚀 Polymarket bot live")
last_price = None
while True:
    try:
        btc_price = get_btc_price()
        market = get_market()

        yes_price = float(market["outcomePrices"][0])
        no_price = float(market["outcomePrices"][1])

        if last_price is None:
            last_price = btc_price
            time.sleep(10)
            continue

        action, edge = evaluate_misprice(
            btc_price, last_price, yes_price, no_price
        )

        print(
            "DEBUG | BTC:", btc_price,
            "REF:", last_price,
            "YES:", yes_price,
            "NO:", no_price,
            "EDGE:", edge,
            "ACTION:", action
        )

        if action:
            send_alert(
                f"🚨 MISPRICE\n"
                f"{action}\n"
                f"BTC: {btc_price}\n"
                f"Reference: {last_price}\n"
                f"Edge: {edge*100:.1f}¢"
            )

        last_price = btc_price
        time.sleep(10)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
