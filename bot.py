# bot.py — v3.2b safe patch file
# Replace ONLY your monitor_open_trades() function with this.

from datetime import datetime, timedelta

def monitor_open_trades(cfg: dict):
    open_rows = read_csv_rows(get_open_trades_file(cfg))
    if not open_rows:
        return

    now_utc = datetime.now(UTC)
    now_et = datetime.now(ET)
    changed = False

    for row in list(open_rows):
        try:
            action = row["action"]
            slug = row["slug"]
            entry_price = float(row["entry_price"])
            tp_price = float(row["tp_price"])
            sl_price = float(row["sl_price"])
            hour_open_btc = float(row["hour_open_btc"])

            scalp_status = row.get("scalp_status", "OPEN")
            settle_status = row.get("settle_status", "OPEN")

            market_hour_end_et = datetime.fromisoformat(row["market_hour_end_et"])
            time_exit_deadline_utc = datetime.fromisoformat(row["time_exit_deadline_utc"])

            midpoint = None
            if now_et < market_hour_end_et:
                try:
                    current_state = resolve_current_market_state()
                    if current_state.slug == slug:
                        token_id = current_state.yes_token_id if action == "BUY UP" else current_state.no_token_id
                        midpoint = fetch_public_clob_midpoint(token_id)
                except Exception:
                    midpoint = None

            if scalp_status == "OPEN":
                exit_reason = None
                exit_price = None

                if midpoint is not None:
                    if midpoint >= tp_price:
                        exit_reason = "TP"
                        exit_price = midpoint
                    elif midpoint <= sl_price:
                        exit_reason = "SL"
                        exit_price = midpoint

                if exit_reason is None and now_utc >= time_exit_deadline_utc and midpoint is not None:
                    exit_reason = "TIME_EXIT"
                    exit_price = midpoint

                if exit_reason and exit_price is not None:
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
                    scalp_result = "WIN" if pnl_pct > 0 else "LOSS"
                    icon = "✅" if scalp_result == "WIN" else "❌"

                    row["scalp_status"] = scalp_result
                    row["scalp_exit_reason"] = exit_reason
                    row["scalp_exit_price"] = round(exit_price, 4)
                    row["scalp_exit_utc"] = now_utc.isoformat()
                    row["scalp_pnl_pct"] = round(pnl_pct, 2)

                    send_telegram(
                        cfg,
                        f"{icon} TRADE CLOSED\n"
                        f"Mode: {get_mode(cfg).upper()}\n"
                        f"Action: {action}\n"
                        f"Grade: {row['grade']}\n"
                        f"Slug: {slug}\n"
                        f"Entry: {entry_price:.3f}\n"
                        f"Exit: {float(row['scalp_exit_price']):.3f}\n"
                        f"Reason: {exit_reason}\n"
                        f"PnL: {float(row['scalp_pnl_pct']):.2f}%"
                    )
                    changed = True

            if settle_status == "OPEN" and now_et >= market_hour_end_et + timedelta(seconds=10):
                settle_btc = fetch_btc_spot_from_coinbase()
                settle_result = "WIN" if (
                    (action == "BUY UP" and settle_btc > hour_open_btc)
                    or (action == "BUY DOWN" and settle_btc < hour_open_btc)
                ) else "LOSS"

                row["settle_status"] = "CLOSED"
                row["settle_result"] = settle_result
                row["settle_btc"] = round(settle_btc, 2)
                row["settle_utc"] = now_utc.isoformat()

                icon = "✅" if settle_result == "WIN" else "❌"
                send_telegram(
                    cfg,
                    f"{icon} FINAL SETTLE RESULT\n"
                    f"Mode: {get_mode(cfg).upper()}\n"
                    f"Action: {action}\n"
                    f"Grade: {row['grade']}\n"
                    f"Slug: {slug}\n"
                    f"Hour Open BTC: {hour_open_btc:.2f}\n"
                    f"Settle BTC: {settle_btc:.2f}\n"
                    f"Result: {settle_result}"
                )
                changed = True

            if row.get("scalp_status") != "OPEN" and row.get("settle_status") == "CLOSED":
                close_trade_record(cfg, row["trade_id"], row)
                changed = True

        except Exception as e:
            log(f"[TRACKER] error monitoring trade: {e}")

    if changed:
        write_summary(cfg)
