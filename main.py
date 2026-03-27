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

# Simulation settings
SIM_MODE = True
SIM_TP_SMALL = 0.08
SIM_TP_MED = 0.10
SIM_TP_LARGE = 0.15
SIM_SL_SMALL = 0.05
SIM_SL_MED = 0.06
SIM_SL_LARGE = 0.07
SIM_TIME_STOP_SMALL = 12 * 60
SIM_TIME_STOP_MED = 15 * 60
SIM_TIME_STOP_LARGE = 20 * 60

ET = ZoneInfo("America/New_York")

# =========================
# STATE
# =========================
hour_open_price = None
hour_started_at = None
current_slug = None

pending_action = None
pending_count = 0

last_alert_time = 0
last_alert_side = None

sim_trade = None


# =========================
# HELPERS
# =========================
def send_alert(message):
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


def get_market(slug):
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


def current_side_price(action, yes, no):
    return yes if action == "BUY UP" else no


def evaluate_signal(btc, open_price, yes, no):
    move = btc - open_price

    if abs(move) < MOVE_THRESHOLD:
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


def build_trade_plan(edge, action, yes, no):
    edge_cents = edge * 100

    if edge_cents < 25:
        tier = "SMALL"
        unit = "0.5u"
        tp_text = "+6 to +8c"
        sl_text = "-4 to -5c"
        time_text = "10-12 min"
        entry_slip = 0.01
        sim_tp = SIM_TP_SMALL
        sim_sl = SIM_SL_SMALL
        sim_time_stop = SIM_TIME_STOP_SMALL
    elif edge_cents < 40:
        tier = "MEDIUM"
        unit = "1u"
        tp_text = "+8 to +12c"
        sl_text = "-5 to -7c"
        time_text = "15-20 min"
        entry_slip = 0.02
        sim_tp = SIM_TP_MED
        sim_sl = SIM_SL_MED
        sim_time_stop = SIM_TIME_STOP_MED
    else:
        tier = "LARGE"
        unit = "1.5u"
        tp_text = "Scale: +10c / +15-20c"
        sl_text = "-6 to -8c"
        time_text = "20-30 min"
        entry_slip = 0.03
        sim_tp = SIM_TP_LARGE
        sim_sl = SIM_SL_LARGE
        sim_time_stop = SIM_TIME_STOP_LARGE

    base_price = yes if action == "BUY UP" else no
    entry_min = round(base_price, 3)
    entry_max = round(base_price + entry_slip, 3)
    entry_mid = round((entry_min + entry_max) / 2, 3)

    return {
        "tier": tier,
        "unit": unit,
        "tp_text": tp_text,
        "sl_text": sl_text,
        "time_text": time_text,
        "entry_min": entry_min,
        "entry_mid": entry_mid,
        "entry_max": entry_max,
        "sim_tp": sim_tp,
        "sim_sl": sim_sl,
        "sim_time_stop": sim_time_stop,
    }


def get_entry_quality(price_now, entry_min, entry_mid, entry_max):
    if price_now <= entry_mid:
        return "IDEAL"
    if price_now <= entry_max:
        return "ACCEPTABLE"
    return "LATE"


# =========================
# SIMULATION
# =========================
def start_sim_trade(action, entry_price, plan, now_ts, slug):
    global sim_trade

    sim_trade = {
        "action": action,
        "entry": entry_price,
        "tier": plan["tier"],
        "tp": plan["sim_tp"],
        "sl": plan["sim_sl"],
        "time_stop": plan["sim_time_stop"],
        "start": now_ts,
        "slug": slug,
        "active": True,
        "max_fav": 0.0,
        "max_adv": 0.0,
    }

    send_alert(
        f"SIM START\n"
        f"{action}\n"
        f"Entry: {entry_price}\n"
        f"Tier: {plan['tier']}\n"
        f"TP Target: +{plan['sim_tp']:.3f}\n"
        f"SL Target: -{plan['sim_sl']:.3f}\n"
        f"Time Stop: {int(plan['sim_time_stop'] / 60)} min"
    )


def update_sim_trade(yes, no, now_ts):
    global sim_trade

    if not sim_trade or not sim_trade["active"]:
        return

    price_now = yes if sim_trade["action"] == "BUY UP" else no
    pnl = round(price_now - sim_trade["entry"], 3)

    if pnl > sim_trade["max_fav"]:
        sim_trade["max_fav"] = pnl
    if pnl < sim_trade["max_adv"]:
        sim_trade["max_adv"] = pnl

    if pnl >= sim_trade["tp"]:
        send_alert(
            f"SIM RESULT\n"
            f"TP HIT\n"
            f"Action: {sim_trade['action']}\n"
            f"Entry: {sim_trade['entry']}\n"
            f"Exit: {price_now}\n"
            f"PnL: {pnl:.3f}\n"
            f"Max Favorable: {sim_trade['max_fav']:.3f}\n"
            f"Max Adverse: {sim_trade['max_adv']:.3f}"
        )
        sim_trade["active"] = False
        return

    if pnl <= -sim_trade["sl"]:
        send_alert(
            f"SIM RESULT\n"
            f"SL HIT\n"
            f"Action: {sim_trade['action']}\n"
            f"Entry: {sim_trade['entry']}\n"
            f"Exit: {price_now}\n"
            f"PnL: {pnl:.3f}\n"
            f"Max Favorable: {sim_trade['max_fav']:.3f}\n"
            f"Max Adverse: {sim_trade['max_adv']:.3f}"
        )
        sim_trade["active"] = False
        return

    if now_ts - sim_trade["start"] >= sim_trade["time_stop"]:
        send_alert(
            f"SIM RESULT\n"
            f"TIME EXIT\n"
            f"Action: {sim_trade['action']}\n"
            f"Entry: {sim_trade['entry']}\n"
            f"Exit: {price_now}\n"
            f"PnL: {pnl:.3f}\n"
            f"Max Favorable: {sim_trade['max_fav']:.3f}\n"
            f"Max Adverse: {sim_trade['max_adv']:.3f}"
        )
        sim_trade["active"] = False


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
            time.sleep(CHECK_SECONDS)
            continue

        yes, no = parse_prices(market["outcomePrices"])

        update_sim_trade(yes, no, now_ts)

        action, edge, move = evaluate_signal(btc, hour_open_price, yes, no)

        if in_no_trade_window(now):
            pending_action = None
            pending_count = 0
            time.sleep(CHECK_SECONDS)
            continue

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

        if confirmed and (now_ts - last_alert_time > COOLDOWN_SECONDS):
            plan = build_trade_plan(edge, action, yes, no)
            side_price_now = current_side_price(action, yes, no)
            entry_quality = get_entry_quality(
                side_price_now,
                plan["entry_min"],
                plan["entry_mid"],
                plan["entry_max"],
            )

            link = f"https://polymarket.com/event/{slug}"

            send_alert(
                f"MISPRICE\n"
                f"{action}\n"
                f"BTC: {btc}\n"
                f"Hour Open: {hour_open_price}\n"
                f"Move: {move:.2f}\n"
                f"YES: {yes}\n"
                f"NO: {no}\n"
                f"Edge: {edge*100:.1f}c\n\n"
                f"ENTRY QUALITY\n"
                f"{entry_quality}\n\n"
                f"ENTRY MIN: {plan['entry_min']}\n"
                f"ENTRY MID: {plan['entry_mid']}\n"
                f"ENTRY MAX: {plan['entry_max']}\n\n"
                f"{plan['tier']} | {plan['unit']}\n"
                f"TP: {plan['tp_text']}\n"
                f"SL: {plan['sl_text']}\n"
                f"TIME: {plan['time_text']}\n\n"
                f"{link}"
            )

            # Start a sim trade only if there isn't already one active
            if SIM_MODE and (sim_trade is None or not sim_trade["active"]):
                start_sim_trade(
                    action=action,
                    entry_price=plan["entry_mid"],
                    plan=plan,
                    now_ts=now_ts,
                    slug=slug
                )

            last_alert_time = now_ts
            last_alert_side = action

        time.sleep(CHECK_SECONDS)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
