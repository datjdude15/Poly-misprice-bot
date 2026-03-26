import requests
import time
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# =========================
# SETTINGS
# =========================
EDGE_THRESHOLD = 0.15
MOVE_THRESHOLD = 15.0
CHECK_SECONDS = 1
COOLDOWN_SECONDS = 180
NO_TRADE_MINUTES = 5
CONFIRMATION_CHECKS = 2

# Pullback settings
PULLBACK_GRACE_SECONDS = 600
PULLBACK_REENTRY_BUFFER = 0.02
LATE_PRICE_BUFFER = 0.07
PULLBACK_MIN_EXTENSION = 0.03

ET = ZoneInfo("America/New_York")

# =========================
# STATE
# =========================
hour_open_price = None
hour_started_at = None
last_alert_time = 0
last_alert_side = None
current_slug = None

pending_action = None
pending_count = 0


def reset_pullback_watch():
    return {
        "active": False,
        "alert_sent": False,
        "created_ts": 0,
        "action": None,
        "edge": 0.0,
        "tier": None,
        "unit": None,
        "tp": None,
        "sl": None,
        "time_stop": None,
        "entry_min": None,
        "entry_max": None,
        "extended_price": None,
        "slug": None,
    }


pullback_watch = reset_pullback_watch()

# =========================
# HELPERS
# =========================
def send_alert(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)


def get_btc_price():
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


def parse_prices(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    return float(raw[0]), float(raw[1])


def hour_to_12(h):
    h = h % 12
    return 12 if h == 0 else h


def ampm(h):
    return "am" if h < 12 else "pm"


def build_slug(dt):
    month = dt.strftime("%B").lower()
    day = dt.day
    year = dt.year
    hour = hour_to_12(dt.hour)
    suffix = ampm(dt.hour)
    return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour}{suffix}-et"


def get_market(slug: str):
    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        r = requests.get(url, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def find_market(now):
    for dt in [now, now - timedelta(hours=1), now + timedelta(hours=1)]:
        slug = build_slug(dt)
        market = get_market(slug)
        if market:
            return slug, market
    return None, None


def in_no_trade_window(now):
    if hour_started_at is None:
        return True
    return (now - hour_started_at).total_seconds() < NO_TRADE_MINUTES * 60


def build_trade_plan(edge, action, yes_price, no_price):
    edge_cents = edge * 100

    if edge_cents < 25:
        tier = "SMALL"
        unit = "0.5u"
        tp = "+6 to +8c"
        sl = "-4 to -5c"
        time_stop = "10-12 min"
        entry_slippage = 0.01
    elif edge_cents < 40:
        tier = "MEDIUM"
        unit = "1u"
        tp = "+8 to +12c"
        sl = "-5 to -7c"
        time_stop = "15-20 min"
        entry_slippage = 0.02
    else:
        tier = "LARGE"
        unit = "1.5u"
        tp = "Scale: +10c / +15-20c"
        sl = "-6 to -8c"
        time_stop = "20-30 min"
        entry_slippage = 0.03

    if action == "BUY UP":
        base_price = yes_price
    else:
        base_price = no_price

    entry_min = round(base_price, 3)
    entry_max = round(base_price + entry_slippage, 3)

    return tier, unit, tp, sl, time_stop, entry_min, entry_max


def evaluate_signal(btc, open_price, yes, no):
    move = btc - open_price
    abs_move = abs(move)

    if abs_move < MOVE_THRESHOLD:
        return None, 0.0, move

    if move > 0:
        edge = 0.75 - yes
        if edge >= EDGE_THRESHOLD:
            return "BUY UP", edge, move

    if move < 0:
        edge = 0.75 - no
        if edge >= EDGE_THRESHOLD:
            return "BUY DOWN", edge, move

    return None, 0.0, move


def current_side_price(action, yes, no):
    return yes if action == "BUY UP" else no


def is_initial_entry_still_valid(action, yes, no, entry_max):
    price_now = current_side_price(action, yes, no)
    return price_now <= entry_max


def is_too_late_to_chase(action, yes, no, entry_max):
    price_now = current_side_price(action, yes, no)
    return price_now > (entry_max + LATE_PRICE_BUFFER)


def start_pullback_watch(now_ts, action, edge, tier, unit, tp, sl, time_stop, entry_min, entry_max, yes, no, slug):
    global pullback_watch

    price_now = current_side_price(action, yes, no)

    pullback_watch = {
        "active": True,
        "alert_sent": False,
        "created_ts": now_ts,
        "action": action,
        "edge": edge,
        "tier": tier,
        "unit": unit,
        "tp": tp,
        "sl": sl,
        "time_stop": time_stop,
        "entry_min": entry_min,
        "entry_max": entry_max,
        "extended_price": price_now,
        "slug": slug,
    }


def update_pullback_watch_extension(action, yes, no):
    global pullback_watch
    if not pullback_watch["active"]:
        return

    price_now = current_side_price(action, yes, no)

    if price_now > pullback_watch["extended_price"]:
        pullback_watch["extended_price"] = price_now


def should_fire_pullback_alert(now_ts, action, edge, yes, no):
    if not pullback_watch["active"]:
        return False, None

    if pullback_watch["alert_sent"]:
        return False, None

    if action != pullback_watch["action"]:
        return False, None

    if now_ts - pullback_watch["created_ts"] > PULLBACK_GRACE_SECONDS:
        return False, None

    if edge < EDGE_THRESHOLD:
        return False, None

    price_now = current_side_price(action, yes, no)
    entry_min = pullback_watch["entry_min"]
    entry_max = pullback_watch["entry_max"]
    extended_price = pullback_watch["extended_price"]

    if extended_price < (entry_max + PULLBACK_MIN_EXTENSION):
        return False, None

    pullback_max = entry_max + PULLBACK_REENTRY_BUFFER

    if entry_min <= price_now <= pullback_max:
        return True, price_now

    return False, None


# =========================
# MAIN LOOP
# =========================
while True:
    try:
        btc = get_btc_price()
        now = datetime.now(ET)
        now_ts = time.time()

        slug, market = find_market(now)

        if not market:
            time.sleep(CHECK_SECONDS)
            continue

        if slug != current_slug:
            current_slug = slug
            hour_open_price = btc
            hour_started_at = now.replace(minute=0, second=0, microsecond=0)

            pending_action = None
            pending_count = 0
            last_alert_side = None
            pullback_watch = reset_pullback_watch()

            time.sleep(CHECK_SECONDS)
            continue

        yes, no = parse_prices(market["outcomePrices"])
        action, edge, move = evaluate_signal(btc, hour_open_price, yes, no)

        if in_no_trade_window(now):
            pending_action = None
            pending_count = 0
            time.sleep(CHECK_SECONDS)
            continue

        # Confirmation logic
        if action:
            if action == pending_action:
                pending_count += 1
            else:
                pending_action = action
                pending_count = 1
        else:
            pending_action = None
            pending_count = 0

        confirmed = pending_count >= CONFIRMATION_CHECKS

        # Pullback watch maintenance
        if pullback_watch["active"] and action == pullback_watch["action"]:
            update_pullback_watch_extension(action, yes, no)

        # Initial confirmed setup
        if confirmed and (now_ts - last_alert_time > COOLDOWN_SECONDS):
            tier, unit, tp, sl, time_stop, entry_min, entry_max = build_trade_plan(edge, action, yes, no)
            link = f"https://polymarket.com/event/{slug}"

            if is_initial_entry_still_valid(action, yes, no, entry_max) and action != last_alert_side:
                send_alert(
                    f"MISPRICE\n"
                    f"{action}\n"
                    f"BTC: {btc}\n"
                    f"Hour Open: {hour_open_price}\n"
                    f"Move: {move:.2f}\n"
                    f"YES: {yes}\n"
                    f"NO: {no}\n"
                    f"Edge: {edge*100:.1f}c\n\n"
                    f"ENTRY ZONE\n"
                    f"{entry_min} - {entry_max}\n"
                    f"Do Not Chase Above: {entry_max}\n\n"
                    f"TRADE PLAN\n"
                    f"Tier: {tier}\n"
                    f"Size: {unit}\n"
                    f"Take Profit: {tp}\n"
                    f"Stop Loss: {sl}\n"
                    f"Time Stop: {time_stop}\n\n"
                    f"{link}"
                )

                last_alert_time = now_ts
                last_alert_side = action
                pending_action = None
                pending_count = 0
                pullback_watch = reset_pullback_watch()

            elif is_too_late_to_chase(action, yes, no, entry_max):
                start_pullback_watch(
                    now_ts=now_ts,
                    action=action,
                    edge=edge,
                    tier=tier,
                    unit=unit,
                    tp=tp,
                    sl=sl,
                    time_stop=time_stop,
                    entry_min=entry_min,
                    entry_max=entry_max,
                    yes=yes,
                    no=no,
                    slug=slug,
                )

                pending_action = None
                pending_count = 0

        # Pullback alert
        if pullback_watch["active"]:
            fire_pullback, pullback_price = should_fire_pullback_alert(now_ts, action, edge, yes, no)

            if fire_pullback and (now_ts - last_alert_time > COOLDOWN_SECONDS):
                link = f"https://polymarket.com/event/{pullback_watch['slug']}"
                action_pb = pullback_watch["action"]
                tier_pb = pullback_watch["tier"]
                unit_pb = pullback_watch["unit"]
                tp_pb = pullback_watch["tp"]
                sl_pb = pullback_watch["sl"]
                time_stop_pb = pullback_watch["time_stop"]
                entry_min_pb = pullback_watch["entry_min"]
                entry_max_pb = round(pullback_watch["entry_max"] + PULLBACK_REENTRY_BUFFER, 3)

                send_alert(
                    f"PULLBACK ENTRY\n"
                    f"{action_pb}\n"
                    f"BTC: {btc}\n"
                    f"Hour Open: {hour_open_price}\n"
                    f"Move: {move:.2f}\n"
                    f"YES: {yes}\n"
                    f"NO: {no}\n"
                    f"Edge: {edge*100:.1f}c\n\n"
                    f"REENTRY ZONE\n"
                    f"{entry_min_pb} - {entry_max_pb}\n"
                    f"Price Returned Near Value Zone\n\n"
                    f"TRADE PLAN\n"
                    f"Tier: {tier_pb}\n"
                    f"Size: {unit_pb}\n"
                    f"Take Profit: {tp_pb}\n"
                    f"Stop Loss: {sl_pb}\n"
                    f"Time Stop: {time_stop_pb}\n\n"
                    f"{link}"
                )

                last_alert_time = now_ts
                last_alert_side = action_pb
                pullback_watch["alert_sent"] = True

            if now_ts - pullback_watch["created_ts"] > PULLBACK_GRACE_SECONDS:
                pullback_watch = reset_pullback_watch()

        time.sleep(CHECK_SECONDS)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
