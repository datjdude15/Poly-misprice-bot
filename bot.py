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
        r = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message,
            },
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Telegram failed: {e}", flush=True)

# =========================
# HELPERS
# =========================
def log(msg):
    now = datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

# =========================
# MARKET FETCH (STRONGER VERSION)
# =========================
def get_market():
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets", timeout=20)
        r.raise_for_status()
        data = r.json()

        btc_markets = []
        for m in data:
            slug = (m.get("slug") or "").lower()
            if "bitcoin" in slug and "up-or-down" in slug:
                btc_markets.append(m)

        if not btc_markets:
            return None

        def sort_key(m):
            return (
                m.get("active", False),
                m.get("endDate") or "",
                m.get("startDate") or "",
                m.get("createdAt") or "",
            )

        btc_markets.sort(key=sort_key, reverse=True)

        chosen = btc_markets[0]
        log(f"[MARKET] Using slug={chosen.get('slug', '')}")
        return chosen

    except Exception as e:
        log(f"[ERROR] Market fetch failed: {e}")

    return None

# =========================
# OUTCOME PRICE HELPERS
# =========================
def extract_yes_no_prices(market):
    outcomes = market.get("outcomes", [])

    yes_price = None
    no_price = None

    # Common case: outcomes is a list of dicts
    for outcome in outcomes:
        name = str(outcome.get("name", "")).strip().lower()
        price = outcome.get("price", None)

        if price is None:
            continue

        try:
            price = float(price)
        except Exception:
            continue

        if name == "yes" or name == "up":
            yes_price = price
        elif name == "no" or name == "down":
            no_price = price

    # Fallback: sometimes order is simply [yes, no]
    if (yes_price is None or no_price is None) and len(outcomes) >= 2:
        try:
            first_price = float(outcomes[0].get("price"))
            second_price = float(outcomes[1].get("price"))
            if yes_price is None:
                yes_price = first_price
            if no_price is None:
                no_price = second_price
        except Exception:
            pass

    return yes_price, no_price

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
    log(f"Loading config from config.yaml")
    log(f"Mode -> {str(config.get('mode', 'paper')).upper()}")
    log(f"Polling every {POLL_SECONDS} seconds")

    while True:
        market = get_market()

        if not market:
            log("No market found")
            time.sleep(POLL_SECONDS)
            continue

        yes, no = extract_yes_no_prices(market)

        if yes is None or no is None:
            log("[PASS] Could not extract YES/NO prices")
            time.sleep(POLL_SECONDS)
            continue

        prob_up = yes
        prob_down = no

        edge_up = calculate_edge(prob_up)
        edge_down = calculate_edge(prob_down)

        slug = market.get("slug", "unknown-slug")

        # =========================
        # LOG TICK
        # =========================
        log(
            f"[TICK] slug={slug} yes={yes:.3f} no={no:.3f} "
            f"edge_up={edge_up:.3f} edge_down={edge_down:.3f}"
        )

        # =========================
        # TRADE SIGNAL
        # =========================
        if edge_up > EDGE_THRESHOLD:
            msg = (
                f"🚨 TRADE SIGNAL (UP)\n"
                f"Slug: {slug}\n"
                f"Price: {yes:.3f}\n"
                f"Edge: {edge_up:.3f}\n"
                f"Prob Up: {prob_up:.3f}"
            )
            log("[TRADE] UP triggered")
            send_telegram(msg)

        elif edge_down > EDGE_THRESHOLD:
            msg = (
                f"🚨 TRADE SIGNAL (DOWN)\n"
                f"Slug: {slug}\n"
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
                f"Slug: {slug}\n"
                f"Edge: {edge_up:.3f}\n"
                f"Needed: {EDGE_THRESHOLD:.3f}"
            )
            log("[NEAR MISS] UP")
            send_telegram(msg)

        elif edge_down > EDGE_THRESHOLD * 0.7:
            msg = (
                f"⚠️ NEAR MISS (DOWN)\n"
                f"Slug: {slug}\n"
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
