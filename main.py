import requests
import time

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
        print(f"🚨 BIG MOVE: {move} | PRICE: {current_price}")
    
    last_price = current_price
