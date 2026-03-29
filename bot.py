import time
import requests
import yaml
import datetime

# =========================
# LOAD CONFIG
# =========================
with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

BOT_TOKEN = config["telegram_bot_token"]
CHAT_ID = config["telegram_chat_id"]

POLL_SECONDS = config.get("poll_seconds", 5)
EDGE_THRESHOLD = config.get("edge_threshold", 0.08)
MIN_MOVE = config.get("min_move", 0.003)

# =========================
# TELEGRAM
# =========================
def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": message
        })
    except Exception as e:
        print(f"[ERROR] Telegram failed: {e}")

# =========================
# HELPERS
# =========================
def log(msg):
    now = datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")
    print(f"[{now}] {msg}")

# =========================
# MARKET FETCH (POLYMARKET)
# =========================
def get_market():
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets")
        data = r.json()

        for m in data:
            if "bitcoin-up-or-down" in m["slug"]:
                return m

    except Exception as e:
        log(f"[ERROR] Market fetch failed: {e}")

    return None

# =========================
# EDGE CALC
# =========================
def calculate_edge(prob):
    return prob - 0.5

# =========================
# MAIN LOOP
# =========================
def run():
    log("🚀 BOT STARTING")
    log(f"Polling every {POLL_SECONDS}s")

    while True:
        market = get_market()

        if not market:
            log("No market found")
            time.sleep(POLL_SECONDS)
            continue

        yes = float(market["outcomes"][0]["price"])
        no = float(market["outcomes"][1]["price"])

        prob_up = yes
        prob_down = no

        edge_up = calculate_edge(prob_up)
        edge_down = calculate_edge(prob_down)

        # =========================
        # LOG TICK
        # =========================
        log(f"[TICK] yes={yes:.3f} no={no:.3f} edge_up={edge_up:.3f}")

        # =========================
        # TRADE SIGNAL
        # =========================
        if edge_up > EDGE_THRESHOLD:
            msg = (
                f"🚨 TRADE SIGNAL (UP)\n"
                f"Price: {yes:.3f}\n"
                f"Edge: {edge_up:.3f}\n"
                f"Prob Up: {prob_up:.3f}"
            )
            log("[TRADE] UP triggered")
            send_telegram(msg)

        elif edge_down > EDGE_THRESHOLD:
            msg = (
                f"🚨 TRADE SIGNAL (DOWN)\n"
                f"Price: {no:.3f}\n"
                f"Edge: {edge_down:.3f}\n"
                f"Prob Down: {prob_down:.3f}"
            )
            log("[TRADE] DOWN triggered")
            send_telegram(msg)

        # =========================
        # NEAR MISS ALERT
        # =========================
        elif edge_up > EDGE_THRESHOLD * 0.7:
            msg = (
                f"⚠️ NEAR MISS (UP)\n"
                f"Edge: {edge_up:.3f}\n"
                f"Needed: {EDGE_THRESHOLD:.3f}"
            )
            log("[NEAR MISS] UP")
            send_telegram(msg)

        elif edge_down > EDGE_THRESHOLD * 0.7:
            msg = (
                f"⚠️ NEAR MISS (DOWN)\n"
                f"Edge: {edge_down:.3f}\n"
                f"Needed: {EDGE_THRESHOLD:.3f}"
            )
            log("[NEAR MISS] DOWN")
            send_telegram(msg)

        # =========================
        # PASS LOG
        # =========================
        else:
            log("[PASS] No signal")

        time.sleep(POLL_SECONDS)

# =========================
# ENTRY
# =========================
if __name__ == "__main__":
    run()
