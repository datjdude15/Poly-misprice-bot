# PolySniper Live Mode Scaffold

This package adds three things:

1. Live auto-execution logic
2. Profit tracker dashboard (Streamlit)
3. Momentum strength indicator

It is designed to be **honest and safe by default**:
- `live_mode: false` by default
- if Polymarket credentials are missing, the bot falls back to simulation / paper mode
- all order placement goes through a single execution layer so you can audit and throttle it

## Important

Polymarket's CLOB trading endpoints require authenticated order signing and L2 headers, while read endpoints are public. The official docs say limit orders are the native order type, with marketable limit orders used for immediate execution. The docs also provide official clients/SDKs and a public WebSocket market channel for real-time orderbook updates. Taker fees currently apply to Crypto and Sports markets. Verify geoblocks before attempting live trading. citeturn486950search0turn486950search1turn486950search8turn486950search10turn486950search12turn486950search15

## Files

- `bot.py` – main loop
- `strategy.py` – signal, filters, momentum score, Kelly sizing
- `execution.py` – simulation and live execution adapter
- `tracker.py` – SQLite logging and PnL tracking
- `dashboard.py` – Streamlit dashboard
- `config.example.yaml` – example config
- `requirements.txt` – dependencies

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

## Run bot

```bash
python bot.py --config config.yaml
```

## Run dashboard

```bash
streamlit run dashboard.py
```

## Live trading checklist

1. Start in paper mode.
2. Confirm signals, fills, blocked reasons, and tracker output for at least 50–100 trades.
3. Turn on `live_mode: true` only after you have valid API credentials, a funded wallet, and confirmed geoblock eligibility.
4. Start with the smallest allowed size.
5. Keep `max_concurrent_positions`, `max_open_exposure_usd`, and `panic_stop_enabled` on.

## What you still need to do

- install the official Polymarket CLOB client if you want actual order placement
- fill in your credential environment variables
- map your target market slug to the actual token / asset IDs you want to trade
- optionally replace the REST polling in this scaffold with Polymarket's public market WebSocket for lower latency

## Environment variables

```bash
export POLY_HOST="https://clob.polymarket.com"
export POLY_CHAIN_ID="137"
export POLY_PRIVATE_KEY="..."
export POLY_API_KEY="..."
export POLY_API_SECRET="..."
export POLY_API_PASSPHRASE="..."
```

## Notes on implementation

This scaffold uses a simple REST polling data source by default so it can run without private SDK setup. For the fastest production version, switch the price feed to Polymarket's public market WebSocket. The official docs expose `wss://ws-subscriptions-clob.polymarket.com/ws/market` for public market data subscriptions. citeturn486950search8turn486950search9
