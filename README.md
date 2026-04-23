# AI Dynamic Grid Bot — MEXC

Telegram-controlled grid trading bot for MEXC spot market.
Automatically adjusts grid range and count using ATR (Average True Range), mirroring KuCoin AI Plus Bot behaviour.

## Quick Start

```bash
cp .env.example .env
# Fill in MEXC_API_KEY, MEXC_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL

pip install -r requirements.txt
python main.py
```

## Telegram Commands

| Command | Description |
|---|---|
| `/start_ai BTCUSDT 500 medium` | Start AI grid (symbol, amount USDT, risk: low/medium/high) |
| `/status BTCUSDT` | Full profit report |
| `/stop BTCUSDT` | Stop bot and market-sell all holdings |
| `/list` | Show all active pairs |
| `/help` | Help message |

## Risk Levels

| Level | ATR Multiplier | Grids |
|---|---|---|
| low | 1.5× | 5–10 |
| medium | 2.0× | 8–20 |
| high | 3.0× | 15–30 |

## How It Works

1. Fetches current price and computes ATR from 15m candles.
2. Derives grid range (`price ± ATR × multiplier`) and grid count.
3. Places limit buy orders below price, limit sell orders above.
4. Every 5 minutes: recomputes ATR. If price escapes range by ≥ 1× ATR, cancels all orders and rebuilds grid around new price.
5. When a buy fills → places matching sell one grid up. When a sell fills → places matching buy one grid down.

## Deployment (Railway)

Set environment variables in Railway dashboard, then deploy. The `Procfile` runs `python main.py` as a worker dyno.
