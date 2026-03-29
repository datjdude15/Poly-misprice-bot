import argparse
import time
import yaml
import requests
from datetime import datetime

def now():
    return datetime.utcnow().strftime("%H:%M:%S")

def log(msg: str):
    print(f"[{now()}] {msg}", flush=True)

def send_telegram_alert(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        log("[ALERT] Telegram not configured")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        log("[ALERT] Telegram alert sent successfully")
        return True
    except Exception as e:
        log(f"[ALERT] Telegram send failed: {e}")
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--test-alert", action="store_true")
    args = parser.parse_args()

    log("🚀 BOT STARTING")
    log(f"Loading config from {args.config}")

    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    token = config.get("telegram_bot_token", "")
    chat_id = str(config.get("telegram_chat_id", ""))

    mode = config.get("mode", "paper")
    poll_seconds = config.get("poll_seconds", 5)

    log(f"Mode -> {mode.upper()}")
    log(f"Polling every {poll_seconds} seconds")

    if args.test_alert:
        sent = send_telegram_alert(
            token,
            chat_id,
            "✅ PolySniperBot test alert successful. Telegram is connected."
        )
        if sent:
            log("Test complete")
        else:
            log("Test failed")
        return

    while True:
        log("Heartbeat... bot running")
        time.sleep(poll_seconds)

if __name__ == "__main__":
    main()
