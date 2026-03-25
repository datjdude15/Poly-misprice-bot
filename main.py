import requests
import time

BOT_TOKEN = "PASTE_YOUR_TOKEN_HERE"
CHAT_ID = "PASTE_YOUR_CHAT_ID_HERE"

def send_alert(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message})

def get_price():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    data = requests.get(url).json()
    return data["bitcoin"]["usd"]

last_price = get_price()

while True:
    time.sleep(5)
    current_price = get_price()
    
    move = current_price - last_price

    if abs(move) > 50:
        send_alert(f"🚨 BTC MOVE: {move} | PRICE: {current_price}")
    
    last_price = current_price
