---
name: hermes-trading
description: Hermes Trading Assistant project guide — dev workflow, pipeline architecture, local deploy.
---

# Hermes Trading Assistant — SKILL.md

## What Is This

Automated US stock analysis with 3 strategy pipelines + FastAPI dashboard. Runs locally, serves on port 8777.

## Quick Start

```bash
python3 -m venv venv_new && source venv_new/bin/activate
pip install -r requirements.txt

# Copy and fill in your API keys
cp .env.example .env

# Run
bash run.sh
# Or: cd scripts && uvicorn server:app --host 0.0.0.0 --port 8777
```

Dashboard: `http://localhost:8777`

## Architecture

```
scripts/
├── server.py              FastAPI — dashboard + config API (Redis hot-reload)
├── module0_universe.py    Stock universe builder
├── pipeline_a_value.py    Pipeline A: 长投 (Value) — weekly
├── pipeline_b_swing.py    Pipeline B: 做T (Swing) — daily
├── pipeline_c_smart.py    Pipeline C: 大佬 (Smart) — daily
├── telegram_bot.py        Telegram alerts + /t TICKER lookup
└── utils.py               Shared utilities
hermes_site/               Frontend dashboard (static HTML)
config.json                Strategy parameters (hot-reloadable)
swing_watchlist.json       Active swing watchlist
```

## Pipelines

| Pipeline | Name | Trigger | Data Source |
|----------|------|---------|-------------|
| A | 长投 (Value) | Weekly scan | Fundamentals (ROE, ROIC, D/E) |
| B | 做T (Swing) | Daily | Price action (ATR, ADX, stops) |
| C | 大佬 (Smart) | Daily | Institutional flow |

## Config Hot-Reload

Config lives in Redis (`hermes:config` key). Dashboard edits apply instantly — no server restart needed.

Fallback: `config.json` if Redis is unavailable.

## Environment Variables

```
FINNHUB_API_KEY=...      # Real-time quotes
TELEGRAM_BOT_TOKEN=...   # Alert bot
TELEGRAM_CHAT_ID=...     # Your chat ID
LLM_API_KEY=...          # AI review (optional)
LLM_BASE_URL=...         # LLM endpoint
```

**NEVER commit .env. It's in .gitignore.**

## Data Flow

```
Finnhub/yfinance → pipeline scripts → JSON output → hermes_site/data/ → dashboard
                                                   ↘ telegram_bot → alerts
```

## Adding a New Pipeline

1. Create `scripts/pipeline_d_xxx.py`
2. Implement `compute_one(symbol, config)` function
3. Register in `server.py` pipeline routing
4. Add frontend tab in `hermes_site/index.html`
5. Update `config.json` with strategy params

## Testing

```bash
# Quick smoke test
cd scripts && python -c "from pipeline_b_swing import compute_one; print(compute_one('NVDA', {}))"
```

## Deploy Notes

- Server runs in WSL, accessible via Tailscale IP
- Port 8777 must be portproxied for Windows access
- Systemd service: `hermes-trading` (if configured)

## Do NOT

- Commit `.env` or API keys
- Commit `cache/`, `logs/`, `venv_new/`
- Hot-reload config without checking Redis connection
- Modify pipeline output format without updating frontend
