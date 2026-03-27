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
CHECK_SECONDS = 1
COOLDOWN_SECONDS = 60
NO_TRADE_MINUTES = 5
CONFIRMATION_CHECKS = 2

# Core signal thresholds
EDGE_THRESHOLD = 0.15
MIN_MOVE_FOR_ENTRY = 30.0
CORE_MIN_MOVE = 30.0
CORE_MAX_MOVE = 60.0

# Strong setup filters
MIN_EDGE_CENTS = 30.0
CHOP_ZONE_MIN = 0.45
CHOP_ZONE_MAX = 0.55

# Extreme / pullback logic
EXTREME_TRIGGER_MOVE = 80.0
EXTREME_BLOCK_MOVE = 100.0
PULLBACK_RETRACE_POINTS = 20.0
PULLBACK_EXPIRY_SECONDS = 12 * 60

# Entry / stack controls
MAX_ENTRIES_PER_SIDE_PER_HOUR = 2
BLOCK_SMALL_TRADES = True

# Smart stacking
SMART_STACKING_ENABLED = True
SMART_STACK_PROFIT_TRIGGER = 0.07
SMART_STACK_MIN_MOVE = 60.0
SMART_STACK_MAX_MOVE = 100.0
SMART_STACK_MIN_EDGE_CENTS = 35.0
SMART_STACK_MAX_PER_SIDE_PER_HOUR = 1

# Reversal confirmation / knife-catch block
REVERSAL_REQUIRED = True
MIN_REVERSAL_SIZE = 5.0
KNIFE_CATCH_BLOCK_ENABLED = True

# Momentum continuation block
STACK_BLOCK_IF_MOMENTUM = True
MOMENTUM_CONTINUATION_BLOCK = True

# Simulation settings
SIM_MODE = True
SIM_TIME_STOP_MED = 15 * 60
SIM_TIME_STOP_LARGE = 20 * 60

# =========================
# BANKROLL / KELLY / EXPOSURE
# =========================
BANKROLL = 1000.0
KELLY_FRACTION = 0.25  # quarter Kelly

MAX_TOTAL_EXPOSURE_PCT = 0.15
MAX_TOTAL_EXPOSURE_DOLLARS = round(BANKROLL * MAX_TOTAL_EXPOSURE_PCT, 2)
MAX_CONCURRENT_ACTIVE_TRADES = 3

# Soft floor/ceiling by setup tier for $1k bankroll
MIN_SIZE_SMALL = 10.0
MIN_SIZE_MED = 20.0
MIN_SIZE_LARGE = 30.0

MAX_SIZE_CORE_MED = 40.0
MAX_SIZE_CORE_LARGE = 60.0
MAX_SIZE_PULLBACK_MED = 45.0
MAX_SIZE_PULLBACK_LARGE = 60.0
MAX_SIZE_STACK = 25.0

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

sim_trades = []
sim_trade_counter = 0

hour_entry_counts = {}
hour_stack_counts = {}
pullback_watches = {}

recent_moves = []
recent_btcs = []


# =========================
# HELPERS
# =========================
def send_alert(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15)


def get_btc_price() -> float:
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


def parse_prices(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    return float(raw[0]), float(raw[1])


def hour_to_12(h: int) -> int:
    h = h % 12
    return 12 if h == 0 else h


def ampm(h: int) -> str:
    return "am" if h < 12 else "pm"


def build_slug(dt: datetime) -> str:
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


def find_market(now: datetime):
    for dt in [now, now - timedelta(hours=1), now + timedelta(hours=1)]:
        slug = build_slug(dt)
        market = get_market(slug)
        if market:
            return slug, market
    return None, None


def in_no_trade_window(now: datetime) -> bool:
    if hour_started_at is None:
        return True
    return (now - hour_started_at).total_seconds() < NO_TRADE_MINUTES * 60


def current_side_price(action: str, yes: float, no: float) -> float:
    return yes if action == "BUY UP" else no


def get_direction_from_move(move: float) -> str:
    return "UP" if move > 0 else "DOWN"


def evaluate_signal(btc: float, open_price: float, yes: float, no: float):
    move = btc - open_price
    abs_move = abs(move)

    if abs_move < MIN_MOVE_FOR_ENTRY:
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


def build_trade_plan(edge: float, action: str, yes: float, no: float, setup_type: str):
    edge_cents = edge * 100

    if edge_cents >= 45:
        tier = "LARGE"
        unit = "1.5u"
        tp_text = "Scale: +10c / +15-20c"
        sl_text = "-6 to -8c"
        time_text = "20-30 min"
        entry_slip = 0.03
        sim_tp = 0.15
        sim_sl = 0.07
        sim_time_stop = SIM_TIME_STOP_LARGE
    elif edge_cents >= 35:
        tier = "MEDIUM"
        unit = "1u"
        tp_text = "+8 to +12c"
        sl_text = "-5 to -7c"
        time_text = "15-20 min"
        entry_slip = 0.02
        sim_tp = 0.10
        sim_sl = 0.06
        sim_time_stop = SIM_TIME_STOP_MED
    else:
        tier = "SMALL"
        unit = "0.5u"
        tp_text = "+6 to +8c"
        sl_text = "-4 to -5c"
        time_text = "10-12 min"
        entry_slip = 0.01
        sim_tp = 0.08
        sim_sl = 0.05
        sim_time_stop = 12 * 60

    base_price = yes if action == "BUY UP" else no
    entry_min = round(base_price, 3)
    entry_max = round(base_price + entry_slip, 3)
    entry_mid = round((entry_min + entry_max) / 2, 3)

    cash_size = recommended_cash_size(tier, setup_type)

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
        "cash_size": cash_size,
    }


def get_entry_quality(price_now: float, entry_min: float, entry_mid: float, entry_max: float) -> str:
    if price_now <= entry_mid:
        return "IDEAL"
    if price_now <= entry_max:
        return "ACCEPTABLE"
    return "LATE"


def get_entry_count_key(slug: str, action: str):
    return f"{slug}|{action}"


def can_take_more_entries(slug: str, action: str) -> bool:
    key = get_entry_count_key(slug, action)
    return hour_entry_counts.get(key, 0) < MAX_ENTRIES_PER_SIDE_PER_HOUR


def increment_entry_count(slug: str, action: str):
    key = get_entry_count_key(slug, action)
    hour_entry_counts[key] = hour_entry_counts.get(key, 0) + 1


def get_stack_count_key(slug: str, action: str):
    return f"{slug}|{action}|stack"


def can_take_more_stacks(slug: str, action: str) -> bool:
    key = get_stack_count_key(slug, action)
    return hour_stack_counts.get(key, 0) < SMART_STACK_MAX_PER_SIDE_PER_HOUR


def increment_stack_count(slug: str, action: str):
    key = get_stack_count_key(slug, action)
    hour_stack_counts[key] = hour_stack_counts.get(key, 0) + 1


def entry_in_chop_zone(entry_mid: float) -> bool:
    return CHOP_ZONE_MIN <= entry_mid <= CHOP_ZONE_MAX


# =========================
# KELLY / SIZING / EXPOSURE
# =========================
def kelly_fraction(p: float, b: float) -> float:
    q = 1.0 - p
    f = ((b * p) - q) / b
    return max(0.0, f)


def setup_prob_and_b(tier: str, setup_type: str):
    # Conservative assumptions from your current sample
    if setup_type == "CORE":
        if tier == "LARGE":
            return 0.62, 2.0
        if tier == "MEDIUM":
            return 0.58, 1.67
        return 0.54, 1.40

    if setup_type == "EXTREME_PULLBACK":
        if tier == "LARGE":
            return 0.65, 2.1
        if tier == "MEDIUM":
            return 0.61, 1.8
        return 0.56, 1.45

    if setup_type == "SMART_STACK":
        if tier == "LARGE":
            return 0.58, 1.6
        if tier == "MEDIUM":
            return 0.55, 1.45
        return 0.52, 1.25

    if setup_type == "EXTREME_PULLBACK_STACK":
        if tier == "LARGE":
            return 0.60, 1.7
        if tier == "MEDIUM":
            return 0.56, 1.5
        return 0.53, 1.25

    return 0.55, 1.5


def current_open_exposure() -> float:
    return round(sum(t["cash_size"] for t in sim_trades if t["active"]), 2)


def active_trade_count() -> int:
    return sum(1 for t in sim_trades if t["active"])


def correlation_blocked(slug: str, action: str, setup_type: str) -> bool:
    # Allow CORE -> SMART_STACK and EXTREME_PULLBACK -> EXTREME_PULLBACK_STACK
    # Block duplicate same-type active trades on same slug/action
    for trade in sim_trades:
        if not trade["active"]:
            continue
        if trade["slug"] != slug:
            continue
        if trade["action"] != action:
            continue
        if trade["setup_type"] == setup_type:
            return True
    return False


def recommended_cash_size(tier: str, setup_type: str) -> float:
    p, b = setup_prob_and_b(tier, setup_type)
    full_kelly = kelly_fraction(p, b)
    frac = full_kelly * KELLY_FRACTION
    raw_size = BANKROLL * frac

    if tier == "SMALL":
        size = max(MIN_SIZE_SMALL, raw_size)
    elif tier == "MEDIUM":
        size = max(MIN_SIZE_MED, raw_size)
    else:
        size = max(MIN_SIZE_LARGE, raw_size)

    if setup_type == "CORE":
        cap = MAX_SIZE_CORE_LARGE if tier == "LARGE" else MAX_SIZE_CORE_MED
    elif setup_type == "EXTREME_PULLBACK":
        cap = MAX_SIZE_PULLBACK_LARGE if tier == "LARGE" else MAX_SIZE_PULLBACK_MED
    else:
        cap = MAX_SIZE_STACK

    size = min(size, cap)

    # Round to nearest whole dollar for cleaner live guidance
    return float(int(round(size)))


def can_fit_exposure(cash_size: float) -> bool:
    if active_trade_count() >= MAX_CONCURRENT_ACTIVE_TRADES:
        return False
    if current_open_exposure() + cash_size > MAX_TOTAL_EXPOSURE_DOLLARS:
        return False
    return True


# =========================
# MOVE HISTORY / CONFIRMATION
# =========================
def update_recent_state(move: float, btc: float):
    recent_moves.append(move)
    recent_btcs.append(btc)

    if len(recent_moves) > 10:
        recent_moves.pop(0)
    if len(recent_btcs) > 10:
        recent_btcs.pop(0)


def reversal_confirmed(action: str) -> bool:
    if not REVERSAL_REQUIRED:
        return True

    if len(recent_btcs) < 4:
        return False

    b0 = recent_btcs[-4]
    b1 = recent_btcs[-3]
    b2 = recent_btcs[-2]
    b3 = recent_btcs[-1]

    if action == "BUY DOWN":
        recent_low = min(b0, b1, b2)
        bounce_size = b3 - recent_low

        if KNIFE_CATCH_BLOCK_ENABLED and (b3 < b2 < b1):
            return False

        return bounce_size >= MIN_REVERSAL_SIZE

    if action == "BUY UP":
        recent_high = max(b0, b1, b2)
        pullback_size = recent_high - b3

        if KNIFE_CATCH_BLOCK_ENABLED and (b3 > b2 > b1):
            return False

        return pullback_size >= MIN_REVERSAL_SIZE

    return False


def momentum_still_extending(action: str) -> bool:
    if not MOMENTUM_CONTINUATION_BLOCK:
        return False

    if len(recent_moves) < 3:
        return False

    m0 = recent_moves[-3]
    m1 = recent_moves[-2]
    m2 = recent_moves[-1]

    if action == "BUY DOWN":
        return m2 < m1 < m0

    if action == "BUY UP":
        return m2 > m1 > m0

    return False


# =========================
# PULLBACK WATCH LOGIC
# =========================
def update_pullback_watch(slug: str, action: str, move: float, btc: float, now_ts: float):
    direction = get_direction_from_move(move)
    key = (slug, action)

    if abs(move) < EXTREME_TRIGGER_MOVE:
        if key in pullback_watches:
            del pullback_watches[key]
        return

    existing = pullback_watches.get(key)

    if existing is None:
        pullback_watches[key] = {
            "action": action,
            "slug": slug,
            "direction": direction,
            "extreme_btc": btc,
            "created_ts": now_ts,
            "armed": True,
        }
        return

    if direction == "DOWN":
        if btc < existing["extreme_btc"]:
            existing["extreme_btc"] = btc
            existing["created_ts"] = now_ts
    else:
        if btc > existing["extreme_btc"]:
            existing["extreme_btc"] = btc
            existing["created_ts"] = now_ts


def pullback_retrace_met(slug: str, action: str, btc: float) -> bool:
    key = (slug, action)
    watch = pullback_watches.get(key)

    if not watch or not watch["armed"]:
        return False

    if watch["direction"] == "DOWN":
        retrace = btc - watch["extreme_btc"]
        return retrace >= PULLBACK_RETRACE_POINTS
    else:
        retrace = watch["extreme_btc"] - btc
        return retrace >= PULLBACK_RETRACE_POINTS


def expire_old_pullback_watches(now_ts: float):
    to_delete = []
    for key, watch in pullback_watches.items():
        if now_ts - watch["created_ts"] > PULLBACK_EXPIRY_SECONDS:
            to_delete.append(key)
    for key in to_delete:
        del pullback_watches[key]


# =========================
# SMART STACKING
# =========================
def find_best_active_trade(slug: str, action: str, yes: float, no: float):
    candidates = []
    current_price = current_side_price(action, yes, no)

    for trade in sim_trades:
        if not trade["active"]:
            continue
        if trade["slug"] != slug:
            continue
        if trade["action"] != action:
            continue
        if trade["setup_type"] not in ("CORE", "EXTREME_PULLBACK"):
            continue

        pnl = round(current_price - trade["entry"], 3)
        candidates.append((pnl, trade))

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0]


def smart_stack_allowed(slug: str, action: str, move: float, edge: float, yes: float, no: float) -> bool:
    if not SMART_STACKING_ENABLED:
        return False

    abs_move = abs(move)
    edge_cents = edge * 100

    if abs_move < SMART_STACK_MIN_MOVE:
        return False

    if abs_move > SMART_STACK_MAX_MOVE:
        return False

    if edge_cents < SMART_STACK_MIN_EDGE_CENTS:
        return False

    if not can_take_more_stacks(slug, action):
        return False

    if STACK_BLOCK_IF_MOMENTUM and momentum_still_extending(action):
        return False

    best_pnl, best_trade = find_best_active_trade(slug, action, yes, no)
    if best_trade is None:
        return False

    if best_pnl is None or best_pnl < SMART_STACK_PROFIT_TRIGGER:
        return False

    return True


# =========================
# SIMULATION
# =========================
def start_sim_trade(action: str, entry_price: float, plan: dict, now_ts: float, slug: str, setup_type: str):
    global sim_trades, sim_trade_counter

    sim_trade_counter += 1
    trade_id = sim_trade_counter

    trade = {
        "id": trade_id,
        "action": action,
        "entry": entry_price,
        "tier": plan["tier"],
        "cash_size": plan["cash_size"],
        "tp": plan["sim_tp"],
        "sl": plan["sim_sl"],
        "time_stop": plan["sim_time_stop"],
        "start": now_ts,
        "slug": slug,
        "setup_type": setup_type,
        "active": True,
        "max_fav": 0.0,
        "max_adv": 0.0,
    }

    sim_trades.append(trade)

    send_alert(
        f"SIM START #{trade_id}\n"
        f"{action}\n"
        f"Setup: {setup_type}\n"
        f"Entry: {entry_price}\n"
        f"Tier: {plan['tier']}\n"
        f"Cash Size: ${plan['cash_size']:.0f}\n"
        f"TP Target: +{plan['sim_tp']:.3f}\n"
        f"SL Target: -{plan['sim_sl']:.3f}\n"
        f"Time Stop: {int(plan['sim_time_stop'] / 60)} min\n"
        f"Open Exposure After Fill: ${current_open_exposure():.0f} / ${MAX_TOTAL_EXPOSURE_DOLLARS:.0f}"
    )


def update_sim_trades(yes: float, no: float, now_ts: float):
    global sim_trades

    for trade in sim_trades:
        if not trade["active"]:
            continue

        price_now = yes if trade["action"] == "BUY UP" else no
        pnl = round(price_now - trade["entry"], 3)

        if pnl > trade["max_fav"]:
            trade["max_fav"] = pnl
        if pnl < trade["max_adv"]:
            trade["max_adv"] = pnl

        if pnl >= trade["tp"]:
            send_alert(
                f"SIM RESULT #{trade['id']}\n"
                f"TP HIT\n"
                f"Action: {trade['action']}\n"
                f"Setup: {trade['setup_type']}\n"
                f"Entry: {trade['entry']}\n"
                f"Exit: {price_now}\n"
                f"PnL: {pnl:.3f}\n"
                f"Cash Size: ${trade['cash_size']:.0f}\n"
                f"Max Favorable: {trade['max_fav']:.3f}\n"
                f"Max Adverse: {trade['max_adv']:.3f}"
            )
            trade["active"] = False
            continue

        if pnl <= -trade["sl"]:
            send_alert(
                f"SIM RESULT #{trade['id']}\n"
                f"SL HIT\n"
                f"Action: {trade['action']}\n"
                f"Setup: {trade['setup_type']}\n"
                f"Entry: {trade['entry']}\n"
                f"Exit: {price_now}\n"
                f"PnL: {pnl:.3f}\n"
                f"Cash Size: ${trade['cash_size']:.0f}\n"
                f"Max Favorable: {trade['max_fav']:.3f}\n"
                f"Max Adverse: {trade['max_adv']:.3f}"
            )
            trade["active"] = False
            continue

        if now_ts - trade["start"] >= trade["time_stop"]:
            send_alert(
                f"SIM RESULT #{trade['id']}\n"
                f"TIME EXIT\n"
                f"Action: {trade['action']}\n"
                f"Setup: {trade['setup_type']}\n"
                f"Entry: {trade['entry']}\n"
                f"Exit: {price_now}\n"
                f"PnL: {pnl:.3f}\n"
                f"Cash Size: ${trade['cash_size']:.0f}\n"
                f"Max Favorable: {trade['max_fav']:.3f}\n"
                f"Max Adverse: {trade['max_adv']:.3f}"
            )
            trade["active"] = False

    sim_trades = [t for t in sim_trades if t["active"]]


# =========================
# ALERT / ENTRY HANDLERS
# =========================
def handle_entry(slug: str, action: str, edge: float, move: float, btc: float, yes: float, no: float, now_ts: float, setup_type: str):
    global last_alert_time

    if not can_take_more_entries(slug, action):
        return

    if correlation_blocked(slug, action, setup_type):
        return

    plan = build_trade_plan(edge, action, yes, no, setup_type)

    if BLOCK_SMALL_TRADES and plan["tier"] == "SMALL":
        return

    if entry_in_chop_zone(plan["entry_mid"]):
        return

    edge_cents = edge * 100
    if edge_cents < MIN_EDGE_CENTS:
        return

    if not can_fit_exposure(plan["cash_size"]):
        return

    price_now = current_side_price(action, yes, no)
    entry_quality = get_entry_quality(price_now, plan["entry_min"], plan["entry_mid"], plan["entry_max"])
    link = f"https://polymarket.com/event/{slug}"

    send_alert(
        f"MISPRICE\n"
        f"{action}\n"
        f"Setup: {setup_type}\n"
        f"BTC: {btc}\n"
        f"Hour Open: {hour_open_price}\n"
        f"Move: {move:.2f}\n"
        f"YES: {yes}\n"
        f"NO: {no}\n"
        f"Edge: {edge_cents:.1f}c\n\n"
        f"ENTRY QUALITY\n"
        f"{entry_quality}\n\n"
        f"ENTRY MIN: {plan['entry_min']}\n"
        f"ENTRY MID: {plan['entry_mid']}\n"
        f"ENTRY MAX: {plan['entry_max']}\n\n"
        f"{plan['tier']} | {plan['unit']}\n"
        f"Kelly Size: ${plan['cash_size']:.0f}\n"
        f"TP: {plan['tp_text']}\n"
        f"SL: {plan['sl_text']}\n"
        f"TIME: {plan['time_text']}\n"
        f"Exposure Cap: ${MAX_TOTAL_EXPOSURE_DOLLARS:.0f}\n"
        f"Concurrent Cap: {MAX_CONCURRENT_ACTIVE_TRADES}\n\n"
        f"{link}"
    )

    if SIM_MODE:
        start_sim_trade(
            action=action,
            entry_price=plan["entry_mid"],
            plan=plan,
            now_ts=now_ts,
            slug=slug,
            setup_type=setup_type,
        )

    increment_entry_count(slug, action)

    if setup_type in ("SMART_STACK", "EXTREME_PULLBACK_STACK"):
        increment_stack_count(slug, action)

    last_alert_time = now_ts

    key = (slug, action)
    if key in pullback_watches and setup_type in ("EXTREME_PULLBACK", "EXTREME_PULLBACK_STACK"):
        del pullback_watches[key]


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
            last_alert_time = 0

            hour_entry_counts = {}
            hour_stack_counts = {}
            pullback_watches = {}
            recent_moves = []
            recent_btcs = []

            time.sleep(CHECK_SECONDS)
            continue

        yes, no = parse_prices(market["outcomePrices"])
        action, edge, move = evaluate_signal(btc, hour_open_price, yes, no)

        update_recent_state(move, btc)
        update_sim_trades(yes, no, now_ts)
        expire_old_pullback_watches(now_ts)

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

        if not confirmed:
            time.sleep(CHECK_SECONDS)
            continue

        if now_ts - last_alert_time < COOLDOWN_SECONDS:
            time.sleep(CHECK_SECONDS)
            continue

        abs_move = abs(move)
        edge_cents = edge * 100

        if edge_cents < MIN_EDGE_CENTS:
            time.sleep(CHECK_SECONDS)
            continue

        if abs_move >= EXTREME_TRIGGER_MOVE and action:
            update_pullback_watch(slug, action, move, btc, now_ts)

        # CORE / MID RANGE
        if action and CORE_MIN_MOVE <= abs_move < EXTREME_BLOCK_MOVE:
            if not reversal_confirmed(action):
                time.sleep(CHECK_SECONDS)
                continue

            if smart_stack_allowed(slug, action, move, edge, yes, no):
                handle_entry(slug, action, edge, move, btc, yes, no, now_ts, "SMART_STACK")
                time.sleep(CHECK_SECONDS)
                continue

            if can_take_more_entries(slug, action):
                key = get_entry_count_key(slug, action)
                if hour_entry_counts.get(key, 0) == 0:
                    handle_entry(slug, action, edge, move, btc, yes, no, now_ts, "CORE")
                    time.sleep(CHECK_SECONDS)
                    continue

            time.sleep(CHECK_SECONDS)
            continue

        # EXTREME RANGE = PULLBACK ONLY
        if action and abs_move >= EXTREME_BLOCK_MOVE:
            if pullback_retrace_met(slug, action, btc):
                if not reversal_confirmed(action):
                    time.sleep(CHECK_SECONDS)
                    continue

                if smart_stack_allowed(slug, action, move, edge, yes, no):
                    handle_entry(slug, action, edge, move, btc, yes, no, now_ts, "EXTREME_PULLBACK_STACK")
                    time.sleep(CHECK_SECONDS)
                    continue

                key = get_entry_count_key(slug, action)
                if hour_entry_counts.get(key, 0) == 0:
                    handle_entry(slug, action, edge, move, btc, yes, no, now_ts, "EXTREME_PULLBACK")
                    time.sleep(CHECK_SECONDS)
                    continue

            time.sleep(CHECK_SECONDS)
            continue

        time.sleep(CHECK_SECONDS)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(10)
