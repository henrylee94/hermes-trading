# Hermes Trading Assistant

Automated US stock analysis and trading assistant with three strategy pipelines and a real-time dashboard.

## Overview

Hermes scans the US equity market daily, runs three independent strategy pipelines, and serves results through a local web dashboard. Configurable via hot-reload without server restart.

### Strategy Pipelines

| Pipeline | Name | Frequency | Description |
|----------|------|-----------|-------------|
| A | 长投 (Value) | Weekly | Fundamental screening — ROE, ROIC, D/E, margin of safety |
| B | 做T (Swing) | Daily | Technical swing trading — ATR-based stops, risk/reward sizing |
| C | 大佬 (Smart) | Daily | Smart money / institutional flow analysis |

### Features

- **Finnhub API** for real-time and historical price data
- **Yahoo Finance** as fallback data source
- **FastAPI** dashboard with hot-reload config via Redis
- **Telegram bot** for alerts and quick `/t TICKER` lookups
- **Watchlist management** — add/remove stocks via dashboard or config

## Tech Stack

- Python 3.10+
- FastAPI + Uvicorn
- Redis (config hot-reload)
- pandas / numpy / yfinance / finnhub-python
- python-telegram-bot

## Setup

```bash
git clone https://github.com/henrylee94/hermes-trading.git
cd hermes-trading

python3 -m venv venv_new
source venv_new/bin/activate

pip install -r requirements.txt
```

### Required Environment Variables

Create a `.env` file in the project root:

```
FINNHUB_API_KEY=your_key
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
LLM_API_KEY=your_key
LLM_BASE_URL=https://your-llm-endpoint
```

## Running

```bash
# Start the server
bash run.sh

# Or directly
cd scripts && uvicorn server:app --host 0.0.0.0 --port 8777
```

Dashboard: `http://localhost:8777`

## Project Structure

```
scripts/
  server.py              — FastAPI server (dashboard + config API)
  module0_universe.py    — Stock universe builder
  pipeline_a_value.py    — Value / long-term pipeline
  pipeline_b_swing.py    — Swing trading pipeline
  pipeline_c_smart.py    — Smart money pipeline
  telegram_bot.py        — Telegram alerts bot
  utils.py               — Shared utilities
hermes_site/             — Frontend dashboard
config.json              — Strategy parameters
swing_watchlist.json     — Active swing watchlist
```

## License

MIT
