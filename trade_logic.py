from __future__ import annotations


def classify_trade_strength(edge_cents: float, move_abs: float, momentum: float, cfg: dict) -> str:
    strat = cfg.get("strategy", {})

    strong_edge_cents = float(strat.get("strong_edge_cents", 30))
    strong_move_abs = float(strat.get("strong_move_abs", 12))
    strong_momentum_score = float(strat.get("strong_momentum_score", 80))

    if edge_cents >= strong_edge_cents and move_abs >= strong_move_abs and momentum >= strong_momentum_score:
        return "STRONG"

    if edge_cents >= 25 and move_abs >= 8 and momentum >= 50:
        return "NORMAL"

    return "WEAK"


def get_dynamic_sl_percent(
    edge_cents: float,
    move_abs: float,
    momentum: float,
    entry_price: float,
    minutes_left: float,
    cfg: dict,
) -> float:
    strat = cfg.get("strategy", {})
    bucket = classify_trade_strength(edge_cents, move_abs, momentum, cfg)

    weak_sl = float(strat.get("weak_sl_percent", 0.10))
    normal_sl = float(strat.get("normal_sl_percent", 0.12))
    strong_sl = float(strat.get("strong_sl_percent", 0.16))

    if bucket == "STRONG":
        sl = strong_sl
    elif bucket == "NORMAL":
        sl = normal_sl
    else:
        sl = weak_sl

    if entry_price <= 0.25:
        sl += 0.02
    elif entry_price <= 0.35:
        sl += 0.01
    elif entry_price >= 0.60:
        sl -= 0.01

    if move_abs >= 20:
        sl -= 0.02
    elif move_abs >= 12:
        sl -= 0.01

    if minutes_left <= 1.0:
        sl -= 0.03
    elif minutes_left <= 2.0:
        sl -= 0.02
    elif minutes_left <= 5.0:
        sl -= 0.01

    return max(0.06, min(sl, 0.20))


def get_strong_grace_period_seconds(cfg: dict) -> int:
    return int(cfg.get("strategy", {}).get("strong_grace_period_seconds", 90))


def is_strong_trade_row(row: dict, cfg: dict) -> bool:
    try:
        edge_cents = float(row.get("edge_cents", 0))
        move_abs = float(row.get("move", 0))
        momentum = float(row.get("momentum", 0))
        return classify_trade_strength(edge_cents, move_abs, momentum, cfg) == "STRONG"
    except Exception:
        return False


def should_block_same_slug_reentry(
    open_rows: list[dict],
    closed_rows: list[dict],
    slug: str,
    action: str,
    prob_up: float,
    prob_down: float,
    move_abs: float,
    cfg: dict,
) -> bool:
    strat = cfg.get("strategy", {})
    reentry_prob_shift = float(strat.get("reentry_prob_shift", 0.15))
    reentry_move_shift = float(strat.get("reentry_move_shift", 10.0))

    same_slug_open = [r for r in open_rows if r.get("slug") == slug]
    if same_slug_open:
        return True

    same_slug_closed = [r for r in closed_rows if r.get("slug") == slug]
    if not same_slug_closed:
        return False

    last = same_slug_closed[-1]
    last_action = str(last.get("action", ""))
    last_prob_up = float(last.get("prob_up", 0) or 0)
    last_prob_down = float(last.get("prob_down", 0) or 0)
    last_move = float(last.get("move", 0) or 0)

    if last_action == action:
        return True

    prob_shift = abs(prob_up - last_prob_up) if action == "BUY UP" else abs(prob_down - last_prob_down)
    move_shift = abs(move_abs - last_move)

    if prob_shift < reentry_prob_shift and move_shift < reentry_move_shift:
        return True

    return False


def should_force_time_pressure_exit(
    midpoint: float | None,
    entry_price: float,
    tp_price: float,
    minutes_left: float,
    action: str,
    hour_open_btc: float,
    current_btc: float,
    cfg: dict,
) -> tuple[bool, str | None]:
    strat = cfg.get("strategy", {})
    pressure_minutes = float(strat.get("time_pressure_minutes_left", 1.5))
    btc_distance_hard = float(strat.get("time_pressure_btc_distance", 120.0))
    min_lock_pnl = float(strat.get("time_pressure_min_profit", 0.06))

    if midpoint is None:
        return False, None

    if minutes_left > pressure_minutes:
        return False, None

    btc_distance = abs(current_btc - hour_open_btc)
    if btc_distance < btc_distance_hard:
        return False, None

    pnl_pct = (midpoint - entry_price) / entry_price

    if pnl_pct >= min_lock_pnl:
        return True, "TIME_PRESSURE_EXIT"

    if midpoint >= tp_price * 0.92:
        return True, "TIME_PRESSURE_EXIT"

    return False, None
