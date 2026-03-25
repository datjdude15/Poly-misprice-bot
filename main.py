import requests
import time
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

POLY_SLUG = "bitcoin-up-or-down-march-25-2026-4am-et"

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

def evaluate_misprice(btc_price, start_price, yes_price, no_price):
    if btc_price > start_price + 5:
        edge = 0.75 - yes_price
        if edge > 0.05:
            return "BUY UP", edge

    elif btc_price < start_price - 5:
        edge = 0.75 - no_price
        if edge > 0.05:
            return "BUY DOWN", edge

    return None, 0

send_alert("🚀 Polymarket bot live")

while True:
    try:
        btc_price = get_btc_price()
        market = get_market()

        # Extract prices
        yes_price = float(market["outcomePrices"][0])
        no_price = float(market["outcomePrices"][1])

        # Approximate starting price
        start_price = float(market["initialValue"])

        action, edge = evaluate_misprice(
            btc_price, start_price, yes_price, no_price
        )

        if action:
            send_alert(
                f"🚨 MISPRICE\n{action}\nBTC: {btc_price}\nEdge: {edge*100:.1f}¢"
            )

        time.sleep(10)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
