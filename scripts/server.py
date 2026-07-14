"""Hermes FastAPI server — serves the dashboard and config API.

Config is stored in Redis (key: hermes:config) for instant hot-reload.
Falls back to config.json if Redis is unavailable.
"""
import json
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "hermes_site"

# ---------------------------------------------------------------------------
# Redis connection (lazy init)
# ---------------------------------------------------------------------------
_redis = None
REDIS_KEY = "hermes:config"


def _get_redis():
    """Lazy-init Redis connection. Returns None if unavailable."""
    global _redis
    if _redis is not None:
        return _redis
    try:
        import redis as _r
        _redis = _r.Redis(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=0,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        _redis.ping()
        return _redis
    except Exception:
        _redis = None
        return None


def _load_config():
    """Load config from Redis, falling back to config.json."""
    r = _get_redis()
    if r:
        try:
            data = r.get(REDIS_KEY)
            if data:
                return json.loads(data)
        except Exception:
            pass
    # Fallback to file
    cfg_path = ROOT / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return json.load(f)
    return {"long": {}, "swing": {}}


def _save_config(cfg: dict):
    """Save config to Redis AND config.json (dual-write for safety)."""
    r = _get_redis()
    if r:
        try:
            r.set(REDIS_KEY, json.dumps(cfg))
        except Exception:
            pass
    # Always persist to file as backup
    cfg_path = ROOT / "config.json"
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Pipeline imports (lazy — only when /api/swing/compute is called)
# ---------------------------------------------------------------------------
_pipeline_b = None


def _get_pipeline():
    global _pipeline_b
    if _pipeline_b is None:
        sys.path.insert(0, str(ROOT / "scripts"))
        from pipeline_b_swing import compute_one as _compute
        from utils import load_config as _load_cfg
        _pipeline_b = ("compute_one", _compute, _load_cfg)
    return _pipeline_b


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Hermes")

app.mount(
    "/data",
    StaticFiles(directory=str(SITE / "data")),
    name="data",
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    """Serve the main dashboard page."""
    return FileResponse(str(SITE / "index.html"))


@app.get("/api/config")
async def get_config():
    """Return the current config (from Redis or file)."""
    return _load_config()


@app.post("/api/config")
async def set_config(payload: dict):
    """Validate and persist config. Applies immediately (hot-reload via Redis).

    Validation ranges
    -----------------
    long:  roe 0-60, roic 0-60, de 0-5, cur 0-10, mktcap 0-5000,
           fscore 0-9, maxpe 1-100, mos 0-80
    swing: equity > 0, budget > 0, risk 0.1-5, target 10-5000,
           stepk 0.1-3, stopmult 0.5-5, atrmin/atrmax 0.5-20,
           adxmax 5-40, pool 3-60
    """
    long_cfg = payload.get("long", {})
    swing_cfg = payload.get("swing", {})

    def check_range(val, lo, hi, name):
        if val is not None and (val < lo or val > hi):
            raise HTTPException(400, f"{name} must be {lo}–{hi}, got {val}")

    # Long-term screener filters
    check_range(long_cfg.get("roe"), 0, 60, "roe")
    check_range(long_cfg.get("roic"), 0, 60, "roic")
    check_range(long_cfg.get("de"), 0, 5, "de")
    check_range(long_cfg.get("cur"), 0, 10, "cur")
    check_range(long_cfg.get("mktcap"), 0, 5000, "mktcap")
    check_range(long_cfg.get("fscore"), 0, 9, "fscore")
    check_range(long_cfg.get("maxpe"), 1, 100, "maxpe")
    check_range(long_cfg.get("mos"), 0, 80, "mos")

    # Swing-trade parameters
    if swing_cfg.get("equity") is not None and swing_cfg["equity"] <= 0:
        raise HTTPException(400, "equity must be > 0")
    if swing_cfg.get("budget") is not None and swing_cfg["budget"] <= 0:
        raise HTTPException(400, "budget must be > 0")
    check_range(swing_cfg.get("risk"), 0.1, 5, "risk")
    check_range(swing_cfg.get("target"), 10, 5000, "target")
    check_range(swing_cfg.get("stepk"), 0.1, 3, "stepk")
    check_range(swing_cfg.get("stopmult"), 0.5, 5, "stopmult")
    check_range(swing_cfg.get("atrmin"), 0.5, 20, "atrmin")
    check_range(swing_cfg.get("atrmax"), 0.5, 20, "atrmax")
    check_range(swing_cfg.get("adxmax"), 5, 40, "adxmax")
    check_range(swing_cfg.get("pool"), 3, 60, "pool")
    check_range(swing_cfg.get("mktcap_min"), 0, 5000, "mktcap_min")

    cfg = {"long": long_cfg, "swing": swing_cfg}
    _save_config(cfg)
    return {"ok": True}


@app.get("/api/swing/watchlist")
async def get_watchlist():
    """Return the current swing watchlist (auto + pins + exclude)."""
    wl_path = ROOT / "swing_watchlist.json"
    if not wl_path.exists():
        return {"auto": [], "pins": [], "exclude": []}
    with open(wl_path) as f:
        wl = json.load(f)
    wl.setdefault("exclude", [])
    return wl


@app.post("/api/swing/watchlist")
async def set_watchlist(payload: dict):
    """Update the swing watchlist.
    Supports two modes:
    - Full mode: send {auto, pins, exclude} → replaces entire watchlist
    - Partial mode: send {pins, exclude} → only updates pins/exclude, keeps auto
    """
    wl_path = ROOT / "swing_watchlist.json"

    # Load existing
    existing = {"auto": [], "pins": [], "exclude": []}
    if wl_path.exists():
        with open(wl_path) as f:
            existing = json.load(f)

    # If full watchlist provided (with auto), use it
    if "auto" in payload:
        existing["auto"] = payload["auto"]

    # Validate pins
    pins = payload.get("pins", [])
    validated = []
    for p in pins:
        sym = str(p.get("sym", "")).upper().strip()
        if not sym or not sym.isalpha() or len(sym) > 6:
            raise HTTPException(400, f"Invalid ticker: {p.get('sym')}")
        shares = p.get("fixed_shares")
        if shares is not None:
            shares = int(shares)
            if shares <= 0:
                raise HTTPException(400, f"fixed_shares must be > 0 for {sym}")
        validated.append({"sym": sym, "fixed_shares": shares})

    # Update exclude
    if "exclude" in payload:
        existing["exclude"] = payload["exclude"]

    existing["pins"] = validated

    with open(wl_path, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    effective = list(dict.fromkeys(
        [s["sym"] if isinstance(s, dict) else s for s in existing["auto"]]
        + [p["sym"] for p in validated]
    ))
    return {"ok": True, "auto": len(existing["auto"]), "pins": len(validated),
            "effective": len(effective), "exclude": len(existing["exclude"])}


@app.post("/api/swing/watchlist/remove")
async def remove_from_watchlist(payload: dict):
    """Remove a stock from the watchlist. If it's in auto, add to exclude
    so weekly scan won't re-add it. If it's in pins, just remove."""
    sym = str(payload.get("sym", "")).upper().strip()
    if not sym or not sym.isalpha():
        raise HTTPException(400, "Invalid ticker")

    wl_path = ROOT / "swing_watchlist.json"
    existing = {"auto": [], "pins": [], "exclude": []}
    if wl_path.exists():
        with open(wl_path) as f:
            existing = json.load(f)

    # Remove from pins
    existing["pins"] = [p for p in existing.get("pins", []) if p["sym"] != sym]

    # If in auto, add to exclude
    auto_syms = [s["sym"] if isinstance(s, dict) else s for s in existing.get("auto", [])]
    if sym in auto_syms:
        exclude = set(existing.get("exclude", []))
        exclude.add(sym)
        existing["exclude"] = list(exclude)

    with open(wl_path, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    effective = [s for s in auto_syms if s not in existing["exclude"]] + [p["sym"] for p in existing["pins"]]
    return {"ok": True, "removed": sym, "effective": len(dict.fromkeys(effective))}


@app.get("/api/swing/compute")
async def swing_compute(sym: str = Query(..., description="Stock ticker")):
    """On-demand swing analysis with live Finnhub price.

    Returns a single stock's swing data with real-time pricing.
    Used by the dashboard refresh button and Telegram /t command.
    """
    sym = sym.upper().strip()
    if not sym or not sym.isalpha():
        raise HTTPException(400, "Invalid ticker symbol")

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")

        compute_one, load_config = _get_pipeline()[1], _get_pipeline()[2]
        cfg = load_config()
        result = compute_one(sym, cfg)

        # Get live price from Finnhub
        import urllib.request
        api_key = os.getenv("FINNHUB_API_KEY", "")
        live_price = None
        if api_key:
            try:
                url = f"https://finnhub.io/api/v1/quote?symbol={sym}&token={api_key}"
                resp = urllib.request.urlopen(url, timeout=10)
                data = json.loads(resp.read())
                live_price = data.get("c")
            except Exception:
                pass

        if live_price:
            result["price"] = live_price
            result["live"] = True

        return result
    except Exception as e:
        raise HTTPException(500, f"Compute error: {e}")


# ---------------------------------------------------------------------------
# Catch-all — serve any file from hermes_site/ (CSS, JS, images, etc.)
# ---------------------------------------------------------------------------
@app.get("/{path:path}")
async def static_catch(path: str):
    file_path = SITE / path
    if file_path.is_file():
        return FileResponse(str(file_path))
    raise HTTPException(404)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8777)
