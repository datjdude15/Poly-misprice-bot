import requests
import time
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_alert(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)
    print("Telegram:", r.status_code, r.text)

def get_price():
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    return float(data["data"]["amount"])

send_alert("✅ Bot is live and connected.")

last_price = None

while True:
    try:
        if last_price is None:
            last_price = get_price()
            print("Starting price:", last_price)
            time.sleep(5)
            continue

        current_price = get_price()
        move = current_price - last_price
        print("Move:", move, "Current:", current_price, "Last:", last_price)

        if abs(move) > 0:
            send_alert(f"🚨 BTC MOVE: {move:.2f} | PRICE: {current_price}")

        last_price = current_price
        time.sleep(5)

    except Exception as e:
        print("MAIN LOOP ERROR:", repr(e))
        time.sleep(10)
