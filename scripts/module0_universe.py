#!/usr/bin/env python3
"""
Module 0 — Shared Data Infrastructure
======================================
Builds the stock universe, fetches 1-year daily prices, and pulls
fundamental data from Finnhub.  Outputs live in cache/ and logs go
to logs/.

Run:
    venv/bin/python scripts/module0_universe.py
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Paths (all relative to project root = one level above this script)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"
LOG_DIR = PROJECT_ROOT / "logs"

CACHE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Logging — INFO to both console and a daily-rotated log file
# ---------------------------------------------------------------------------
_log_date = datetime.now().strftime("%Y%m%d")
_log_file = LOG_DIR / f"hermes_{_log_date}.log"

logger = logging.getLogger("module0")
logger.setLevel(logging.INFO)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")

_fh = RotatingFileHandler(_log_file, maxBytes=10 * 1024 * 1024,
                          backupCount=5, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
logger.addHandler(_ch)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv(PROJECT_ROOT / ".env")

import os  # noqa: E402  (after load_dotenv)

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
if not FINNHUB_API_KEY:
    logger.warning("FINNHUB_API_KEY not set — fundamentals fetch will fail.")

# Global for graceful shutdown save
_current_fundamentals: dict = {}
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Received signal %d — saving progress and exiting...", signum)
    if _current_fundamentals:
        _save_fundamentals(_current_fundamentals)
        logger.info("Gracefully saved %d tickers to cache.", len(_current_fundamentals))
    sys.exit(0)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

# ---------------------------------------------------------------------------
# Heavy imports (lazy-ish — after env so failures are logged)
# ---------------------------------------------------------------------------
import yfinance as yf  # noqa: E402
from curl_cffi import requests as cffi_requests  # noqa: E402
import finnhub as fh  # noqa: E402

# ═══════════════════════════════════════════════════════════════════════════
# 1. UNIVERSE
# ═══════════════════════════════════════════════════════════════════════════

# Tickers that are known ADRs or have non-standard treatment
_ADR_HINTS = {"SNY", "NVO", "ASML", "NICE", "TSM", "BABA", "PDD",
              "JD", "BIDU", "NIO", "LI", "XPEV", "BGNE", "ZNH"}

# Dual-class tickers where yfinance uses a different separator than finnhub.
# yfinance: BRK-B   finnhub: BRK.B
# We store BOTH formats so downstream code can pick the right one.
_DUAL_CLASS_MAP = {
    "BRK.B": "BRK-B",
    "BF.B":  "BF-B",
}


def _wiki_table(url: str, match_col: str,
                session: cffi_requests.Session | None = None) -> pd.DataFrame:
    """Read a Wikipedia HTML table via pandas + curl_cffi."""
    headers = {"User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )}
    if session is None:
        session = cffi_requests.Session(impersonate="chrome")
    resp = session.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    from io import StringIO
    tables = pd.read_html(StringIO(resp.text), match=match_col)
    if not tables:
        raise ValueError(f"No table matching '{match_col}' at {url}")
    return tables[0]


def build_universe(skip_if_cached: bool = True) -> dict:
    """
    Fetch S&P 500 + Nasdaq 100 from Wikipedia, deduplicate, and return
    a dict keyed by yfinance-style ticker symbol.

    Each entry:
    {
        "ticker_yf": "BRK-B",       # for yfinance
        "ticker_fh": "BRK.B",       # for finnhub
        "name": "Berkshire Hathaway",
        "gics_sector": "Financials",
        "is_adr": false,
        "excluded_value": true       # Financials / Utilities
    }
    """
    if skip_if_cached:
        cache_path = CACHE_DIR / "universe_latest.json"
        if cache_path.exists():
            age_h = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600
            if age_h < 24:
                logger.info("Universe cache is %.1f h old — skipping fetch.", age_h)
                with open(cache_path, encoding="utf-8") as f:
                    return json.load(f)["tickers"]
            logger.info("Universe cache is %.1f h old — refreshing.", age_h)
        else:
            logger.info("No universe cache found — fetching.")

    logger.info("═" * 60)
    logger.info("MODULE 0 — UNIVERSE")
    logger.info("═" * 60)

    sess = cffi_requests.Session(impersonate="chrome")

    # --- S&P 500 -----------------------------------------------------------
    logger.info("Fetching S&P 500 constituents from Wikipedia …")
    sp = _wiki_table(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        match_col="Symbol", session=sess,
    )
    logger.info("  S&P 500 table rows: %d", len(sp))

    # Normalise column names (Wikipedia sometimes changes casing)
    sp.columns = [c.strip() for c in sp.columns]
    # Identify the right columns
    sym_col = next(c for c in sp.columns if c.lower() in ("symbol",))
    name_col = next(c for c in sp.columns if c.lower() in ("security", "company"))
    sector_col = next(c for c in sp.columns
                      if "gics" in c.lower() and "sector" in c.lower())

    sp_tickers = {}
    for _, row in sp.iterrows():
        raw_sym = str(row[sym_col]).strip()
        yf_sym = raw_sym.replace(".", "-")          # yfinance format
        fh_sym = raw_sym                             # finnhub uses dots
        # Override for known dual-class where yfinance also needs dot→dash
        if raw_sym in _DUAL_CLASS_MAP:
            yf_sym = _DUAL_CLASS_MAP[raw_sym]

        sp_tickers[yf_sym] = {
            "ticker_yf": yf_sym,
            "ticker_fh": fh_sym,
            "name": str(row.get(name_col, "")).strip(),
            "gics_sector": str(row.get(sector_col, "")).strip(),
            "source": "sp500",
        }

    logger.info("  Unique S&P tickers (yf): %d", len(sp_tickers))

    # --- Nasdaq 100 --------------------------------------------------------
    logger.info("Fetching Nasdaq 100 constituents from Wikipedia …")
    ndq = _wiki_table(
        "https://en.wikipedia.org/wiki/Nasdaq-100",
        match_col="Ticker", session=sess,
    )
    ndq.columns = [c.strip() for c in ndq.columns]
    ndq_sym_col = next(c for c in ndq.columns if c.lower() in ("ticker",))
    ndq_name_col = next(
        c for c in ndq.columns
        if "company" in c.lower() or c.lower() == "name"
    )

    ndq_count = 0
    for _, row in ndq.iterrows():
        raw_sym = str(row[ndq_sym_col]).strip()
        yf_sym = raw_sym.replace(".", "-")
        fh_sym = raw_sym
        if raw_sym in _DUAL_CLASS_MAP:
            yf_sym = _DUAL_CLASS_MAP[raw_sym]

        if yf_sym not in sp_tickers:
            sp_tickers[yf_sym] = {
                "ticker_yf": yf_sym,
                "ticker_fh": fh_sym,
                "name": str(row.get(ndq_name_col, "")).strip(),
                "gics_sector": "",   # unknown — will be filled from yfinance if needed
                "source": "nasdaq100",
            }
            ndq_count += 1
    logger.info("  Nasdaq-100 only (new): %d", ndq_count)
    logger.info("  Combined universe: %d tickers", len(sp_tickers))

    # --- Classify / flag ----------------------------------------------------
    excluded_value_sectors = {"Financials", "Utilities"}
    for sym, info in sp_tickers.items():
        # ADR flag (simple heuristic: known ADRs or non-US exchange hints)
        info["is_adr"] = sym in _ADR_HINTS
        # Value-exclusion flag
        info["excluded_value"] = info["gics_sector"] in excluded_value_sectors

    n_value_excl = sum(1 for v in sp_tickers.values() if v["excluded_value"])
    n_adr = sum(1 for v in sp_tickers.values() if v["is_adr"])
    logger.info("  Excluded from value screen (Financials/Utilities): %d",
                n_value_excl)
    logger.info("  Flagged ADRs: %d", n_adr)

    # --- Save ---------------------------------------------------------------
    out = {
        "generated": datetime.now().isoformat(),
        "count": len(sp_tickers),
        "sp500_count": len([v for v in sp_tickers.values()
                            if v["source"] == "sp500"]),
        "nasdaq100_only": ndq_count,
        "excluded_value_count": n_value_excl,
        "adr_count": n_adr,
        "tickers": sp_tickers,
    }
    out_path = CACHE_DIR / "universe_latest.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    logger.info("  Saved universe → %s", out_path)

    return sp_tickers


# ═══════════════════════════════════════════════════════════════════════════
# 2. PRICES
# ═══════════════════════════════════════════════════════════════════════════

def fetch_prices(universe: dict | None = None, skip_if_cached: bool = True) -> pd.DataFrame:
    """
    Download 1 year of daily prices in ONE batched yf.download() call and
    compute technical summary columns.

    Returns a DataFrame indexed by ticker with columns:
        price, ma50, ma200, 52w_high, 52w_low, ret_252, ret_21
    Also saves to cache/prices_latest.parquet.
    """
    if skip_if_cached:
        cache_path = CACHE_DIR / "prices_latest.parquet"
        if cache_path.exists():
            age_h = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600
            if age_h < 24:
                logger.info("Prices cache is %.1f h old — loading from disk.", age_h)
                return pd.read_parquet(cache_path)
            logger.info("Prices cache is %.1f h old — refreshing.", age_h)
        else:
            logger.info("No prices cache found — fetching.")

    logger.info("═" * 60)
    logger.info("MODULE 0 — PRICES")
    logger.info("═" * 60)

    if universe is None:
        # Load from cache
        with open(CACHE_DIR / "universe_latest.json", encoding="utf-8") as f:
            data = json.load(f)
        universe = data["tickers"]

    tickers = sorted(universe.keys())

    # --- Extra tickers: SPY + 11 SPDR sector ETFs (needed for RRG) ----------
    _EXTRA_ETFS = [
        "SPY", "XLK", "XLV", "XLF", "XLY", "XLP",
        "XLE", "XLI", "XLU", "XLRE", "XLB", "XLC",
    ]
    extra = [e for e in _EXTRA_ETFS if e not in tickers]
    tickers.extend(extra)
    logger.info("Downloading prices for %d tickers (+%d ETFs for RRG) …",
                len(universe), len(extra))

    session = cffi_requests.Session(impersonate="chrome")

    # Single batched download
    raw = yf.download(
        tickers,
        period="1y",
        interval="1d",
        group_by="ticker",
        threads=2,
        session=session,
        progress=False,
        auto_adjust=True,
    )

    logger.info("  Raw download shape: %s", raw.shape)

    # Build summary per ticker
    rows = []
    for sym in tickers:
        try:
            if len(tickers) == 1:
                df = raw
            else:
                df = raw[sym].dropna(how="all")
            if df.empty or len(df) < 5:
                logger.debug("  %s: insufficient data (%d rows), skipping",
                             sym, len(df))
                continue

            close = df["Close"]
            latest_price = float(close.iloc[-1])

            ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
            ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

            high_52w = float(close.max())
            low_52w = float(close.min())

            # Returns
            ret_252 = None
            if len(close) >= 252:
                ret_252 = float(close.iloc[-1] / close.iloc[-252] - 1)
            ret_21 = None
            if len(close) >= 21:
                ret_21 = float(close.iloc[-1] / close.iloc[-21] - 1)

            rows.append({
                "ticker": sym,
                "price": latest_price,
                "ma50": ma50,
                "ma200": ma200,
                "52w_high": high_52w,
                "52w_low": low_52w,
                "ret_252": ret_252,
                "ret_21": ret_21,
            })
        except Exception as exc:
            logger.warning("  %s: price computation error — %s", sym, exc)

    prices = pd.DataFrame(rows).set_index("ticker")
    logger.info("  Computed prices for %d tickers", len(prices))

    out_path = CACHE_DIR / "prices_latest.parquet"
    prices.to_parquet(out_path)
    logger.info("  Saved prices → %s", out_path)

    return prices


# ═══════════════════════════════════════════════════════════════════════════
# 3. FUNDAMENTALS
# ═══════════════════════════════════════════════════════════════════════════

def _finnhub_retry(client: fh.Client, sym: str,
                   retries: int = 3, base_delay: float = 2.0) -> dict | None:
    """
    Call company_basic_financials with retry on HTTP 429.
    Returns the metric dict or None on failure.
    """
    for attempt in range(retries + 1):
        try:
            resp = client.company_basic_financials(sym, "all")
            if resp and "metric" in resp:
                return resp["metric"]
            return None
        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "rate" in msg or "limit" in msg:
                delay = base_delay * (2 ** attempt)
                logger.warning("  %s: rate-limited (attempt %d/%d), "
                               "backing off %.1fs",
                               sym, attempt + 1, retries + 1, delay)
                time.sleep(delay)
            else:
                logger.warning("  %s: finnhub error — %s", sym, exc)
                return None
    return None


# Keys to pull from the flat metric dict
_METRIC_KEYS = [
    "peTTM",
    "pbAnnual",
    "roeTTM",
    "roiTTM",
    "totalDebt/totalEquityAnnual",
    "currentRatioAnnual",
    "freeCashFlowTTM",
    "epsTTM",
    "bookValuePerShareAnnual",
    "marketCapitalization",
    "enterpriseValue",
    "beta",
    "grossMarginTTM",
    "revenueGrowth5Y",
    "dividendsPerShareTTM",
    "payoutRatioTTM",
]


def _safe_float(val, /) -> float | None:
    """Convert to float, returning None for NaN / missing."""
    if val is None:
        return None
    try:
        v = float(val)
        if np.isnan(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _save_fundamentals(results: dict) -> None:
    """Write fundamentals cache to disk."""
    out = {
        "generated": datetime.now().isoformat(),
        "count": len(results),
        "tickers": results,
    }
    out_path = CACHE_DIR / "fundamentals_latest.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


def fetch_fundamentals(universe: dict | None = None, resume: bool = True) -> dict:
    """
    Pull key metrics from Finnhub for every ticker in the universe.
    Throttles to 1.1 s/call and retries 429s.

    Returns a dict keyed by yfinance ticker with all extracted fields.
    Saves to cache/fundamentals_latest.json.
    """
    logger.info("═" * 60)
    logger.info("MODULE 0 — FUNDAMENTALS")
    logger.info("═" * 60)

    if universe is None:
        with open(CACHE_DIR / "universe_latest.json", encoding="utf-8") as f:
            data = json.load(f)
        universe = data["tickers"]

    if not FINNHUB_API_KEY:
        logger.error("FINNHUB_API_KEY is missing — skipping fundamentals.")
        return {}

    client = fh.Client(api_key=FINNHUB_API_KEY)
    tickers = sorted(universe.keys())
    total = len(tickers)

    # --- Resume support: load existing partial results ---
    cache_path = CACHE_DIR / "fundamentals_latest.json"
    existing: dict = {}
    if resume and cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        existing = cached.get("tickers", {})
        logger.info("Loaded %d previously cached fundamentals — will only fetch missing.",
                     len(existing))
    # Filter to only tickers not yet fetched
    to_fetch = [s for s in tickers if s not in existing]
    if not to_fetch:
        logger.info("All %d tickers already cached — nothing to fetch.", total)
        return existing

    logger.info("Fetching fundamentals for %d tickers (throttle 1.1 s) …",
                len(to_fetch))

    results = dict(existing)
    t_start = time.time()

    for idx, sym in enumerate(to_fetch, 1):
        fh_sym = universe[sym].get("ticker_fh", sym)

        if idx % 50 == 0 or idx == 1:
            elapsed = time.time() - t_start
            rate = idx / elapsed if elapsed > 0 else 0
            logger.info("  Progress: %d / %d  (%.1f tkr/s)  — next: %s",
                        idx, len(to_fetch), rate, fh_sym)

        metric = _finnhub_retry(client, fh_sym)

        record: dict = {}
        if metric is None:
            # Still record a skeleton so downstream knows it was attempted
            record = {k: None for k in _METRIC_KEYS}
            record["series_annual"] = None
        else:
            for k in _METRIC_KEYS:
                if k == "totalDebt/totalEquityAnnual":
                    raw = metric.get(k)
                    record[k] = _safe_float(raw)
                    # finnhub returns this as a percentage; normalise to ratio
                    if record[k] is not None:
                        record[k] = record[k] / 100.0
                else:
                    record[k] = _safe_float(metric.get(k))

            # Override PE when EPS is negative
            eps = record.get("epsTTM")
            if eps is not None and eps < 0:
                record["peTTM"] = None

            # Annual series for trend tests (Altman Z, Piotroski, etc.)
            annual = metric.get("series", {}).get("annual")
            if annual and isinstance(annual, dict):
                # Keep only the last 4 years to limit JSON size
                trimmed = {}
                for sub_key, sub_dict in annual.items():
                    if isinstance(sub_dict, dict):
                        # Keys are typically dates like "2023-12-31"
                        sorted_items = sorted(sub_dict.items(), reverse=True)[:4]
                        trimmed[sub_key] = {d: _safe_float(v)
                                            for d, v in sorted_items}
                record["series_annual"] = trimmed if trimmed else None
            else:
                record["series_annual"] = None

        results[sym] = record

        # Periodic save every 50 tickers so we can resume on interruption
        if idx % 50 == 0:
            _save_fundamentals(results)
            logger.info("  Auto-saved %d/%d tickers.", idx, len(to_fetch))

        # Throttle — 1.1 s between calls
        if idx < len(to_fetch):
            time.sleep(1.1)

    elapsed = time.time() - t_start
    logger.info("  Fundamentals complete: %d tickers fetched in %.0f s (%d total)",
                len(to_fetch), elapsed, len(results))

    # Save
    _save_fundamentals(results)
    logger.info("  Saved fundamentals → %s", CACHE_DIR / "fundamentals_latest.json")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# RUN ALL
# ═══════════════════════════════════════════════════════════════════════════

def run_all(skip_fundamentals: bool = False) -> None:
    """Run the full Module 0 pipeline: universe → prices → fundamentals."""
    t0 = time.time()
    logger.info("╔══════════════════════════════════════════════════════════╗")
    logger.info("║         MODULE 0 — SHARED DATA INFRASTRUCTURE         ║")
    logger.info("╚══════════════════════════════════════════════════════════╝")

    # 1. Universe
    universe = build_universe()

    # 2. Prices
    fetch_prices(universe)

    # 3. Fundamentals (optional skip — it takes ~10 min for ~500 tickers)
    if not skip_fundamentals:
        fetch_fundamentals(universe)
    else:
        logger.info("Skipping fundamentals (--skip-fundamentals).")

    elapsed = time.time() - t0
    logger.info("╔══════════════════════════════════════════════════════════╗")
    logger.info("║  Module 0 complete — total time: %.0f s               ║", elapsed)
    logger.info("╚══════════════════════════════════════════════════════════╝")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    skip_fh = "--skip-fundamentals" in sys.argv
    run_all(skip_fundamentals=skip_fh)
