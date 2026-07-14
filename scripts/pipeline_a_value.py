#!/usr/bin/env python3
"""
Pipeline A — 价值投资周报 (Weekly Value Investing Pipeline)

Reads pre-built cache (universe, prices, fundamentals) and produces
hermes_site/data/value/{ISOweek}.json with ranked value stocks,
sector rotation, and a single LLM-generated narrative digest.

Usage:
    python pipeline_a_value.py          # run full pipeline
    python pipeline_a_value.py --dry    # print JSON to stdout, skip writes
"""

from __future__ import annotations

import json
import math
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
from utils import (
    ROOT,
    CACHE,
    DATA,
    LOGS,
    load_json,
    save_json,
    load_config,
    setup_logger,
    get_env,
    retry,
    update_index,
    append_journal,
    call_llm,
)

warnings.filterwarnings("ignore", category=FutureWarning)

logger = setup_logger("pipeline_a_value")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SPDR_MAP: dict[str, str] = {
    "XLK":  "Technology",
    "XLV":  "Health Care",
    "XLF":  "Financials",
    "XLY":  "Consumer Disc.",
    "XLP":  "Consumer Staples",
    "XLE":  "Energy",
    "XLI":  "Industrials",
    "XLU":  "Utilities",
    "XLRE": "Real Estate",
    "XLB":  "Materials",
    "XLC":  "Comm. Services",
    "SPY":  "SPY",
}

SECTOR_EXCLUDE_VALUE = {"Financials", "Utilities"}

# Series keys used in altman_z (non-manufacturing)
_ALT_KEYS = (
    "current_assets", "current_liabilities", "total_assets",
    "retained_earnings", "ebit", "total_liabilities", "book_equity",
)

# Piotroski series keys
_PIO_KEYS = (
    "net_income", "cfo", "total_assets", "total_assets_prev",
    "roa", "roa_prev", "lt_debt", "lt_debt_prev",
    "current_ratio", "current_ratio_prev",
    "shares_outstanding", "shares_outstanding_prev",
    "gross_margin", "gross_margin_prev",
    "asset_turnover", "asset_turnover_prev",
)

# ---------------------------------------------------------------------------
# SPDR Sector Rotation helpers (using Yahoo Finance cached data)
# ---------------------------------------------------------------------------
def _spdr_rotation_data(prices_df: pd.DataFrame) -> pd.DataFrame:
    """
    Download 1-year daily prices for 11 SPDR sector ETFs + SPY,
    compute relative strength and RRG quadrant for each sector.
    """
    etf_symbols = list(SPDR_MAP.keys())

    # Download daily close prices for ETFs directly (need time series, not summary)
    try:
        import yfinance as yf
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate="chrome")
        raw = yf.download(
            etf_symbols, period="1y", interval="1d",
            group_by="ticker", threads=2, session=session,
            progress=False, auto_adjust=True,
        )
        # Build close price DataFrame: rows=date, columns=ticker
        if len(etf_symbols) == 1:
            close_df = raw[["Close"]].rename(columns={"Close": etf_symbols[0]})
        else:
            close_df = pd.DataFrame(
                {sym: raw[(sym, "Close")] for sym in etf_symbols if (sym, "Close") in raw.columns}
            )
    except Exception as exc:
        logger.warning("ETF price download failed: %s", exc)
        return pd.DataFrame()

    available = [s for s in etf_symbols if s in close_df.columns]
    if "SPY" not in available:
        logger.warning("SPY not in ETF prices; sector rotation skipped")
        return pd.DataFrame()

    pivot = close_df[available].dropna()
    if pivot.empty:
        return pd.DataFrame()

    # Relative Strength = (sector / SPY) * 100
    spy_col = pivot["SPY"].replace(0, np.nan)
    rs = pivot.div(spy_col, axis=0) * 100.0

    # RS_Ratio = 100 + sma( (RS - sma(RS,55)) / std(RS,55), 4 )
    results: list[dict[str, Any]] = []
    for col in rs.columns:
        if col == "SPY":
            continue
        series = rs[col].dropna()
        if len(series) < 60:
            continue
        sma55 = series.rolling(55).mean()
        std55 = series.rolling(55).std()
        z = (series - sma55) / std55
        z_smooth = z.rolling(4).mean()

        # RS-Momentum via SMA(4) of the z-series
        rs_ratio_val = 100.0 + z_smooth.iloc[-1] if not np.isnan(z_smooth.iloc[-1]) else 100.0

        # RS-Rate-of-change for quadrant (use smoothed z vs previous)
        if len(z_smooth.dropna()) >= 4:
            rs_roc = z_smooth.iloc[-1] - z_smooth.iloc[-5]
        else:
            rs_roc = 0.0

        # Quadrant classification
        if rs_ratio_val >= 100 and rs_roc >= 0:
            quadrant = "Leading"
        elif rs_ratio_val < 100 and rs_roc >= 0:
            quadrant = "Improving"
        elif rs_ratio_val >= 100 and rs_roc < 0:
            quadrant = "Weakening"
        else:
            quadrant = "Lagging"

        sector_name = SPDR_MAP.get(col, col)
        results.append({
            "etf": col,
            "sector": sector_name,
            "rs_ratio": round(rs_ratio_val, 2),
            "rs_mom": round(100 + rs_roc, 2),
            "quadrant": quadrant,
        })

    return pd.DataFrame(results)


def _rs_ratio_chinese(rs_ratio: float) -> str:
    if rs_ratio >= 103:
        return "明显比大盘强"
    elif rs_ratio >= 101:
        return "比大盘强"
    elif rs_ratio >= 99:
        return "和大盘差不多"
    elif rs_ratio >= 97:
        return "比大盘弱"
    else:
        return "明显比大盘弱"


def sector_rotation(prices_df: pd.DataFrame) -> list[dict]:
    """Compute sector rotation (RRG) for 11 SPDR sectors."""
    rot_df = _spdr_rotation_data(prices_df)
    if rot_df.empty:
        return []

    out: list[dict] = []
    for _, row in rot_df.iterrows():
        out.append({
            "s": f"{row['sector']} {row['etf']}",
            "x": row["rs_ratio"],
            "y": row["rs_mom"],
            "q": row["quadrant"],
        })
    return out


# ---------------------------------------------------------------------------
# 1. load_data
# ---------------------------------------------------------------------------
def load_data() -> tuple[list[dict], pd.DataFrame, dict, dict]:
    """Load universe, prices, fundamentals and config from cache."""
    logger.info("Loading data …")
    config = load_config()

    # Universe — raw JSON has {"tickers": {SYMBOL: {...}, ...}, ...}
    universe_raw = load_json(CACHE / "universe_latest.json")
    if not universe_raw:
        raise FileNotFoundError("cache/universe_latest.json is empty or missing")
    # Extract the tickers dict → list of stock dicts
    if isinstance(universe_raw, dict) and "tickers" in universe_raw:
        universe = list(universe_raw["tickers"].values())
    elif isinstance(universe_raw, list):
        universe = universe_raw
    else:
        universe = list(universe_raw.values())
    # Normalise: rename gics_sector → sector if needed
    for stock in universe:
        if "gics_sector" in stock and "sector" not in stock:
            stock["sector"] = stock["gics_sector"]

    # Prices (parquet) — ticker is the index; add 'symbol' column
    prices_path = CACHE / "prices_latest.parquet"
    if not prices_path.exists():
        raise FileNotFoundError(f"Missing {prices_path}")
    prices_df = pd.read_parquet(prices_path)
    if "symbol" not in prices_df.columns:
        prices_df = prices_df.reset_index()
        # The index name might be 'ticker' or None
        idx_col = prices_df.columns[0]
        if idx_col != "symbol":
            prices_df = prices_df.rename(columns={idx_col: "symbol"})

    # Fundamentals — raw JSON has {"tickers": {SYMBOL: {...}, ...}, ...}
    fundies_raw = load_json(CACHE / "fundamentals_latest.json")
    if not fundies_raw:
        raise FileNotFoundError("cache/fundamentals_latest.json is empty or missing")
    if isinstance(fundies_raw, dict) and "tickers" in fundies_raw:
        fundies = fundies_raw["tickers"]
    elif isinstance(fundies_raw, dict):
        fundies = fundies_raw
    else:
        fundies = fundies_raw

    # Normalise fundamental keys from Finnhub format to pipeline format
    _FUND_KEY_MAP = {
        "roeTTM": "roe",
        "roiTTM": "roic",
        "totalDebt/totalEquityAnnual": "de",
        "currentRatioAnnual": "cur",
        "freeCashFlowTTM": "fcf",
        "marketCapitalization": "mktcap",
        "epsTTM": "eps",
        "enterpriseValue": "ev",
        "peTTM": "pe",
        "pbAnnual": "pb",
        "bookValuePerShareAnnual": "bvps",
        "grossMarginTTM": "gross_margin",
        "revenueGrowth5Y": "rev_growth_5y",
        "payoutRatioTTM": "payout_ratio",
        "beta": "beta",
    }
    if isinstance(fundies, dict):
        for ticker, fdata in fundies.items():
            if isinstance(fdata, dict):
                for fh_key, pipe_key in _FUND_KEY_MAP.items():
                    if fh_key in fdata and pipe_key not in fdata:
                        fdata[pipe_key] = fdata[fh_key]
                # Finnhub returns marketCapitalization & enterpriseValue in millions of USD;
                # convert to raw dollars so quality-gate comparisons work.
                for money_key in ("mktcap", "ev"):
                    val = fdata.get(money_key)
                    if val is not None:
                        try:
                            fdata[money_key] = float(val) * 1e6
                        except (TypeError, ValueError):
                            pass

    logger.info(
        "Loaded universe=%d, prices_rows=%d, fundies=%d",
        len(universe), len(prices_df), len(fundies),
    )
    return universe, prices_df, fundies, config


# ---------------------------------------------------------------------------
# 2. quality_gates
# ---------------------------------------------------------------------------
def quality_gates(
    universe: list[dict],
    prices_df: pd.DataFrame,
    fundies: dict,
    config: dict,
) -> list[dict]:
    """
    Apply fundamental quality gates. Returns enriched stock dicts that pass.
    """
    gate_cfg = config.get("long", {})
    roe_min = gate_cfg.get("roe", 15) / 100.0
    roic_min = gate_cfg.get("roic", 10) / 100.0
    de_max = gate_cfg.get("de", 1.5)
    cur_min = gate_cfg.get("cur", 1.0)
    mktcap_min = gate_cfg.get("mktcap", 2.0) * 1e9  # in billions → raw

    passed: list[dict] = []
    prices_lookup = {}
    for _, row in prices_df.iterrows():
        prices_lookup[row.name] = row.to_dict()

    for stock in universe:
        yf = stock["ticker_yf"]
        fh = stock["ticker_fh"]
        sector = stock.get("sector", "")

        # Exclude by sector
        if sector in SECTOR_EXCLUDE_VALUE:
            continue
        # Exclude ADRs
        if stock.get("is_adr", False):
            continue
        # Excluded by flag
        if stock.get("excluded_value", False):
            continue

        f = fundies.get(fh)
        if not f:
            continue

        # Numeric gates
        roe = f.get("roe")
        roic = f.get("roic")
        de = f.get("de")
        cur = f.get("cur")
        fcf = f.get("fcf")
        mktcap = f.get("mktcap")
        eps = f.get("eps")
        ev = f.get("ev")

        # Require at least mktcap and eps; skip individual gates for missing data
        if None in (mktcap, eps):
            continue
        if f"{eps}" == "":
            continue

        if fcf is not None:
            try:
                fcf_f = float(fcf)
            except (TypeError, ValueError):
                fcf_f = None
            if fcf_f is not None and fcf_f <= 0:
                continue
        else:
            fcf_f = None

        try:
            roe_f = float(roe) if roe is not None else None
            roic_f = float(roic) if roic is not None else None
            de_f = float(de) if de is not None else None
            cur_f = float(cur) if cur is not None else None
            mktcap_f = float(mktcap)
            eps_f = float(eps)
            ev_f = float(ev) if ev is not None else None
        except (TypeError, ValueError):
            continue

        if roe_f is not None and roe_f < roe_min:
            continue
        if roic_f is not None and roic_f < roic_min:
            continue
        if de_f is not None and de_f >= de_max:
            continue
        if cur_f is not None and cur_f < cur_min:
            continue
        if fcf_f is not None and fcf_f <= 0:
            continue
        if mktcap_f < mktcap_min:
            continue
        if eps_f <= 0:
            continue

        # Enrich with price data
        price_row = prices_lookup.get(yf, {})
        enriched = {
            **stock,
            **f,
            "roe_f": roe_f,
            "roic_f": roic_f,
            "de_f": de_f,
            "cur_f": cur_f,
            "fcf_f": fcf_f,
            "mktcap_f": mktcap_f,
            "eps_f": eps_f,
            "ev_f": ev_f,
            "close": price_row.get("close"),
            "ma50": price_row.get("ma50"),
            "ma200": price_row.get("ma200"),
            "high_52w": price_row.get("high_52w"),
            "low_52w": price_row.get("low_52w"),
            "ret_252": price_row.get("ret_252"),
            "ret_21": price_row.get("ret_21"),
        }
        passed.append(enriched)

    logger.info("Quality gates: %d → %d", len(universe), len(passed))
    return passed


# ---------------------------------------------------------------------------
# 3. altman_z  (non-manufacturing variant)
# ---------------------------------------------------------------------------
def altman_z(stock: dict) -> float | None:
    """
    Altman Z-score (non-manufacturing):
      Z = 6.56*X1 + 3.26*X2 + 6.72*X3 + 1.05*X4
    X1 = (CA - CL) / TA
    X2 = RE / TA
    X3 = EBIT / TA
    X4 = Book Equity / TL
    Returns None if data missing.
    """
    series = stock.get("series_annual") or {}
    if not series:
        return None

    # series_annual is typically a dict keyed by year → annual data
    # We try the most recent year (last key)
    if isinstance(series, dict):
        years = sorted(series.keys())
        if not years:
            return None
        latest = series[years[-1]]
    elif isinstance(series, list) and series:
        latest = series[-1]
    else:
        return None

    if not isinstance(latest, dict):
        return None

    try:
        ca = float(latest.get("current_assets", 0))
        cl = float(latest.get("current_liabilities", 0))
        ta = float(latest.get("total_assets", 0))
        re = float(latest.get("retained_earnings", 0))
        ebit = float(latest.get("ebit", 0))
        tl = float(latest.get("total_liabilities", 0))
        be = float(latest.get("book_equity", 0))
    except (TypeError, ValueError):
        return None

    if ta == 0 or tl == 0:
        return None

    x1 = (ca - cl) / ta
    x2 = re / ta
    x3 = ebit / ta
    x4 = be / tl

    z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
    return round(z, 3)


def _altman_z_chinese(z: float | None) -> str:
    if z is None:
        return "破产风险 未知"
    if z >= 2.6:
        return f"破产风险 低 (Z={z})"
    elif z >= 1.1:
        return f"破产风险 中 (Z={z})"
    else:
        return f"破产风险 高 (Z={z})"


# ---------------------------------------------------------------------------
# 4. piotroski_f  (9-point scoring)
# ---------------------------------------------------------------------------
def piotroski_f(stock: dict) -> int:
    """
    Piotroski F-Score: 9 binary checks, ≥7 = strong.
    """
    series = stock.get("series_annual") or {}
    if isinstance(series, dict) and series:
        years = sorted(series.keys())
        curr = series[years[-1]]
        prev = series[years[-2]] if len(years) >= 2 else {}
    elif isinstance(series, list) and series:
        curr = series[-1]
        prev = series[-2] if len(series) >= 2 else {}
    else:
        return 0

    if not isinstance(curr, dict):
        return 0

    def _f(key, d=None, default=None):
        """Safely extract float from dict."""
        src = d if d is not None else curr
        v = src.get(key, default)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    score = 0

    # 1. ROA > 0
    roa = _f("roa")
    if roa is not None and roa > 0:
        score += 1

    # 2. CFO > 0
    cfo = _f("cfo")
    if cfo is not None and cfo > 0:
        score += 1

    # 3. ΔROA > 0
    roa_prev = _f("roa", prev)
    if roa is not None and roa_prev is not None and roa > roa_prev:
        score += 1

    # 4. CFO / TA > ROA
    ta = _f("total_assets")
    if cfo is not None and ta is not None and ta != 0 and roa is not None:
        if (cfo / ta) > roa:
            score += 1

    # 5. LT Debt ↓
    lt = _f("lt_debt")
    lt_prev = _f("lt_debt", prev)
    if lt is not None and lt_prev is not None:
        if lt < lt_prev:
            score += 1

    # 6. Current Ratio ↑
    cr = _f("current_ratio")
    cr_prev = _f("current_ratio", prev)
    if cr is not None and cr_prev is not None:
        if cr > cr_prev:
            score += 1

    # 7. No new shares issued
    shares = _f("shares_outstanding")
    shares_prev = _f("shares_outstanding", prev)
    if shares is not None and shares_prev is not None:
        if shares <= shares_prev:
            score += 1

    # 8. Gross Margin ↑
    gm = _f("gross_margin")
    gm_prev = _f("gross_margin", prev)
    if gm is not None and gm_prev is not None:
        if gm > gm_prev:
            score += 1

    # 9. Asset Turnover ↑
    at = _f("asset_turnover")
    at_prev = _f("asset_turnover", prev)
    if at is not None and at_prev is not None:
        if at > at_prev:
            score += 1

    return score


def _piotroski_chinese(score: int) -> str:
    return f"财务健康 {score}/9"


# ---------------------------------------------------------------------------
# 5. Valuation — MagicFormula ranking
# ---------------------------------------------------------------------------
def valuation(stocks: list[dict], config: dict) -> list[dict]:
    """
    Rank stocks by MagicFormula (earnings_yield + ROC combined rank).
    Pre-filter: PE ≤ min(sector_median_PE, 15), PB ≤ 1.5 or PE*PB ≤ 22.5.
    """
    if not stocks:
        return []

    # Build sector median PE
    sector_pe_map: dict[str, list[float]] = {}
    for s in stocks:
        sector = s.get("sector", "Unknown")
        pe = s.get("pe")
        if pe is not None:
            try:
                pe_f = float(pe)
                if pe_f > 0:
                    sector_pe_map.setdefault(sector, []).append(pe_f)
            except (TypeError, ValueError):
                pass

    sector_median_pe: dict[str, float] = {}
    for sec, pes in sector_pe_map.items():
        sector_median_pe[sec] = float(np.median(pes))

    filtered: list[dict] = []
    for s in stocks:
        pe = s.get("pe")
        pb = s.get("pb")
        ev = s.get("ev_f") or s.get("ev")
        eps = s.get("eps_f") or s.get("eps")

        try:
            pe_f = float(pe) if pe else None
            pb_f = float(pb) if pb else None
            ev_f = float(ev) if ev else None
            eps_f = float(eps) if eps else None
        except (TypeError, ValueError):
            continue

        if pe_f is None or pb_f is None or ev_f is None or eps_f is None:
            continue
        if pe_f <= 0 or pb_f <= 0 or ev_f <= 0:
            continue

        sector = s.get("sector", "Unknown")
        med_pe = sector_median_pe.get(sector, 20.0)
        max_pe = min(med_pe, 15.0)

        if pe_f > max_pe:
            continue
        if pb_f > 1.5 and pe_f * pb_f > 22.5:
            continue

        filtered.append(s)

    if not filtered:
        return []

    # Compute EBIT (proxy: eps * shares or net_income approx)
    # We use eps * 1e6 (assume millions) but better: eps / (mktcap/ev) etc.
    # Simplified: EBIT ≈ eps * (mktcap / close) when shares ≈ mktcap/close
    # Actually we'll compute EBIT directly from fundamentals if available,
    # or approximate: EBIT/EV = earnings_yield; ROC = EBIT/(NFA+NWC)
    # We'll approximate EBIT = eps * shares_outstanding ≈ mktcap (simpler: use net_income)

    for s in filtered:
        mktcap = s.get("mktcap_f", 0)
        ev = s.get("ev_f", 0)
        eps_f = s.get("eps_f", 0)
        pe_f = float(s.get("pe", 999))
        pb_f = float(s.get("pb", 999))
        bvps = s.get("bvps", 0)
        try:
            bvps = float(bvps) if bvps else 0
        except (TypeError, ValueError):
            bvps = 0
        mktcap_f = float(mktcap) if mktcap else 0
        ev_f = float(ev) if ev else 0

        # EBIT proxy: use net_income (eps * shares). Shares ≈ mktcap/close
        close = s.get("close", 0)
        try:
            close_f = float(close) if close else 1
        except (TypeError, ValueError):
            close_f = 1

        shares_approx = mktcap_f / close_f if close_f else 0
        ebit_proxy = eps_f * shares_approx  # rough EBIT

        earnings_yield = ebit_proxy / ev_f if ev_f > 0 else 0

        # ROC = EBIT / (NFA + NWC) ≈ EBIT / (mktcap + net_debt) rough
        de = s.get("de_f", 1)
        try:
            de_f = float(de) if de else 1
        except (TypeError, ValueError):
            de_f = 1
        # NFA+NWC ≈ book_equity * (1 + de_ratio)
        invested_capital = mktcap_f * (1 + de_f) if de_f else mktcap_f
        roc = ebit_proxy / invested_capital if invested_capital > 0 else 0

        s["earnings_yield"] = round(earnings_yield, 4)
        s["roc"] = round(roc, 4)

    # Rank by earnings_yield (desc) + ROC (desc) combined rank
    sorted_ey = sorted(filtered, key=lambda x: x.get("earnings_yield", 0), reverse=True)
    for i, s in enumerate(sorted_ey):
        s["ey_rank"] = i + 1

    sorted_roc = sorted(filtered, key=lambda x: x.get("roc", 0), reverse=True)
    for i, s in enumerate(sorted_roc):
        s["roc_rank"] = i + 1

    for s in filtered:
        s["magic_rank"] = s.get("ey_rank", 999) + s.get("roc_rank", 999)

    ranked = sorted(filtered, key=lambda x: x.get("magic_rank", 9999))

    # Add Chinese description
    for i, s in enumerate(ranked):
        s["magic_position"] = i + 1
        s["magic_rank_score"] = s.get("magic_rank", "N/A")

    return ranked


# ---------------------------------------------------------------------------
# 6. fair_value — Graham + PE-based
# ---------------------------------------------------------------------------
def fair_value(stock: dict, config: dict) -> dict:
    """
    Compute fair value estimates.
    Graham: √(22.5 × EPS_norm × BVPS)
    PE-based: EPS_norm × min(5y_PE, sector_PE, 15)
    Buy price = fair_value × (1 - margin_of_safety/100)
    """
    fv_config = config.get("long", {})
    mos = fv_config.get("mos", 20)  # margin of safety %

    eps = stock.get("eps_f", 0)
    bvps = stock.get("bvps", 0)
    pe = stock.get("pe")
    close = stock.get("close", 0)
    sector = stock.get("sector", "Unknown")

    try:
        eps_f = float(eps) if eps else 0
        bvps_f = float(bvps) if bvps else 0
        pe_f = float(pe) if pe else 0
        close_f = float(close) if close else 0
    except (TypeError, ValueError):
        eps_f = bvps_f = pe_f = close_f = 0

    # Normalise EPS (use positive)
    eps_norm = max(eps_f, 0)

    # Graham number
    graham = None
    if eps_norm > 0 and bvps_f > 0:
        graham = math.sqrt(22.5 * eps_norm * bvps_f)

    # 5-year average PE from series_annual
    series = stock.get("series_annual") or {}
    five_year_pes: list[float] = []
    if isinstance(series, dict):
        for yr_key in sorted(series.keys())[-5:]:
            yr_data = series[yr_key]
            if isinstance(yr_data, dict):
                p = yr_data.get("pe")
                if p is not None:
                    try:
                        pf = float(p)
                        if pf > 0:
                            five_year_pes.append(pf)
                    except (TypeError, ValueError):
                        pass
    avg_5y_pe = float(np.mean(five_year_pes)) if five_year_pes else pe_f

    # PE-based fair value
    fv_pe = eps_norm * min(avg_5y_pe, pe_f, 15.0) if pe_f > 0 else None

    # Fair value = min(graham, fv_pe) where available
    values = [v for v in [graham, fv_pe] if v is not None and v > 0]
    fair_value_num = min(values) if values else None

    buy_price = None
    in_zone = False
    if fair_value_num is not None and fair_value_num > 0:
        buy_price = fair_value_num * (1 - mos / 100.0)
        if close_f > 0 and buy_price > 0:
            in_zone = close_f <= buy_price

    return {
        "graham": round(graham, 2) if graham else None,
        "fv_pe": round(fv_pe, 2) if fv_pe else None,
        "fair_value": round(fair_value_num, 2) if fair_value_num else None,
        "buy_price": round(buy_price, 2) if buy_price else None,
        "mos_pct": mos,
        "in_zone": in_zone,
    }


# ---------------------------------------------------------------------------
# 7. drop_diagnoser — trap flags + event study
# ---------------------------------------------------------------------------
def drop_diagnoser(stock: dict, prices_df: pd.DataFrame) -> dict:
    """
    For IN_ZONE stocks only.
    7 trap flags + event study z-score.
    """
    trap_flags: list[str] = []
    trap_count = 0

    close = stock.get("close", 0)
    ma50 = stock.get("ma50", 0)
    ma200 = stock.get("ma200", 0)
    high_52w = stock.get("high_52w", 0)
    low_52w = stock.get("low_52w", 0)
    ret_252 = stock.get("ret_252", 0)
    ret_21 = stock.get("ret_21", 0)

    try:
        close_f = float(close) if close else 0
        ma50_f = float(ma50) if ma50 else 0
        ma200_f = float(ma200) if ma200 else 0
        high_f = float(high_52w) if high_52w else 0
        low_f = float(low_52w) if low_52w else 0
        ret252 = float(ret_252) if ret_252 else 0
        ret21 = float(ret_21) if ret_21 else 0
    except (TypeError, ValueError):
        return {"trap_count": 0, "trap_flags": [], "tag": "⚪ 数据不足"}

    # Flag 1: Price below MA200 (death cross zone)
    if ma200_f > 0 and close_f < ma200_f:
        trap_flags.append("跌破200日均线")
        trap_count += 1

    # Flag 2: Price below MA50
    if ma50_f > 0 and close_f < ma50_f:
        trap_flags.append("跌破50日均线")
        trap_count += 1

    # Flag 3: 52w high drawdown > 40%
    if high_f > 0:
        drawdown = (high_f - close_f) / high_f
        if drawdown > 0.40:
            trap_flags.append(f"距52周高点跌{drawdown*100:.0f}%")
            trap_count += 1

    # Flag 4: Near 52w low (within 5%)
    if low_f > 0 and close_f > 0:
        near_low = (close_f - low_f) / low_f
        if near_low < 0.05:
            trap_flags.append("接近52周低点")
            trap_count += 1

    # Flag 5: 1-year return deeply negative
    if ret252 < -0.30:
        trap_flags.append(f"年度跌幅{ret252*100:.0f}%")
        trap_count += 1

    # Flag 6: Recent 21-day drop > 15%
    if ret21 < -0.15:
        trap_flags.append(f"近21日跌幅{ret21*100:.0f}%")
        trap_count += 1

    # Flag 7: Declining revenue growth
    rev_g = stock.get("rev_growth_5y")
    try:
        rev_g_f = float(rev_g) if rev_g else 0
        if rev_g_f < 0:
            trap_flags.append("营收增长为负")
            trap_count += 1
    except (TypeError, ValueError):
        pass

    # Event study z-score
    # Compare stock's 21d return vs SPY's 21d return
    spy_ret21 = None
    yf_ticker = stock.get("ticker_yf", "")
    for _, row in prices_df.iterrows():
        if row.name == "SPY":
            spy_ret21 = row.get("ret_21")
            break

    event_z = None
    event_desc = ""
    if spy_ret21 is not None and ret21 != 0:
        try:
            spy_r = float(spy_ret21)
            stock_r = float(ret21)
            # Simple z: (stock_ret - SPY_ret) / rolling_std approximate
            # Use difference in returns as a rough proxy
            diff = stock_r - spy_r
            event_z = round(diff, 4)
            if diff > -0.05:
                event_desc = "🟢 只是跟着大盘回调,公司没事"
            else:
                event_desc = "🔴 它自己单独大跌"
        except (TypeError, ValueError):
            pass

    # Tag
    if trap_count < 2:
        tag = "🟢"
    else:
        tag = "🔴"

    return {
        "trap_count": trap_count,
        "trap_flags": trap_flags,
        "tag": tag,
        "event_z": event_z,
        "event_desc": event_desc,
    }


# ---------------------------------------------------------------------------
# 8. sector_rotation — delegated to helper above
# (Implemented inline via _spdr_rotation_data and called in run_all)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 9. generate_digest — deterministic score + single LLM call
# ---------------------------------------------------------------------------
def _compute_digest_score(prices_df: pd.DataFrame) -> dict:
    """Deterministic market sentiment score 0-100.
    Blend: SPY weekly return (40%), % above 200DMA (30%), VIX (30%).
    Returns {score: int, sentiment: "pos"|"neu"|"neg"}.
    """
    spy_row = prices_df.loc[prices_df.index == "SPY"]
    if spy_row.empty:
        return {"score": 50, "sentiment": "neu"}

    spy = spy_row.iloc[0]
    ret_21 = float(spy.get("ret_21", 0) or 0)
    close = float(spy.get("close", 0) or 0)
    ma200 = float(spy.get("ma200", 0) or 0)

    # Component 1: SPY weekly return (use ret_21 as proxy)
    # Map: -10% → 0, 0% → 50, +10% → 100
    ret_score = min(max((ret_21 + 0.10) / 0.20 * 100, 0), 100)

    # Component 2: % above 200DMA
    if ma200 > 0:
        pct_above = (close - ma200) / ma200
        # Map: -20% → 0, 0% → 50, +20% → 100
        dma_score = min(max((pct_above + 0.20) / 0.40 * 100, 0), 100)
    else:
        dma_score = 50

    # Component 3: VIX — not available in current cache, use neutral
    vix_score = 50

    # Weighted blend
    score = round(0.4 * ret_score + 0.3 * dma_score + 0.3 * vix_score)
    score = min(max(score, 0), 100)

    if score >= 60:
        sentiment = "pos"
    elif score >= 40:
        sentiment = "neu"
    else:
        sentiment = "neg"

    return {"score": score, "sentiment": sentiment}


def generate_digest(
    zone_stocks: list[dict],
    rrg: list[dict],
    market_stats: list[dict],
    digest_info: dict,
) -> dict:
    """Generate a narrative digest via LLM. Only 1 call, temp=0.2.
    Wraps deterministic score with LLM narrative text.
    """
    score = digest_info.get("score", 50)
    sentiment = digest_info.get("sentiment", "neu")

    try:
        # Build pre-computed context
        stock_lines = []
        for s in zone_stocks[:20]:  # cap at 20
            line = (
                f"{s.get('name', s.get('sym', ''))} "
                f"({s.get('sym', '')}): "
                f"价格={s.get('price')}, "
                f"公允价值={s.get('fair_value')}, "
                f"买入价={s.get('buy')}, "
                f"Z={s.get('altman_z_str')}, "
                f"Piotroski={s.get('piotroski_score')}/9, "
                f"危险信号={s.get('trap_count', 0)}/7"
            )
            stock_lines.append(line)

        rrg_lines = []
        for r in rrg:
            rrg_lines.append(
                f"{r['s']}: x={r['x']}, y={r['y']}, 象限={r['q']}"
            )

        # Extract market stats for prompt
        ms_dict = {item["k"]: item["v"] for item in market_stats} if market_stats else {}

        prompt = f"""你是一位资深价值投资分析师。请基于以下数据，用中文写一份简洁的投资观察摘要（300-500字）。
重点关注：1) 当前有哪些值得买入的低估股票 2) 各板块的轮动情况 3) 需要注意的风险。
请保持客观，不要推荐具体买卖操作。

【在买入区间的股票】
{chr(10).join(stock_lines) if stock_lines else '无'}

【板块轮动 RRG】
{chr(10).join(rrg_lines) if rrg_lines else '数据不足'}

【市场统计】
{chr(10).join(f"{item['k']}: {item['v']}" for item in market_stats) if market_stats else '数据不足'}
市场情绪评分: {score}/100 ({sentiment})
在买入区间股票数: {len(zone_stocks)}
"""

        narrative = call_llm(prompt, temperature=0.2)
        if not narrative:
            raise RuntimeError("call_llm returned None")
        return {"sentiment": sentiment, "score": score, "text": narrative}

    except Exception as e:
        logger.warning("LLM call failed: %s", e)
        return {"sentiment": sentiment, "score": score, "text": ""}


# ---------------------------------------------------------------------------
# 10. run_all — orchestrator
# ---------------------------------------------------------------------------
def run_all(dry: bool = False) -> None:
    """Full pipeline: load → filter → score → value → diagnose → rotate → digest → output."""
    logger.info("=" * 60)
    logger.info("Pipeline A — Value Weekly — START")
    logger.info("=" * 60)

    # ── 1. Load data ──
    universe, prices_df, fundies, config = load_data()

    # ── 2. Quality gates ──
    stocks = quality_gates(universe, prices_df, fundies, config)

    if not stocks:
        logger.warning("No stocks passed quality gates. Aborting.")
        _write_output([], [], [], [], [], dry)
        return

    # ── 3–4. Altman Z + Piotroski (compute for all quality-passed) ──
    for s in stocks:
        z = altman_z(s)
        s["altman_z"] = z
        s["altman_z_str"] = _altman_z_chinese(z)

        pio = piotroski_f(s)
        s["piotroski_score"] = pio
        s["piotroski_str"] = _piotroski_chinese(pio)

    # Filter: only keep Altman Z ≥ 1.1 (not in danger zone)
    # Keep stocks that pass altman_z or have None (unknown)
    altman_pass = [s for s in stocks if s["altman_z"] is None or s["altman_z"] >= 1.1]
    logger.info("Altman Z filter: %d → %d", len(stocks), len(altman_pass))

    # ── 5. Valuation — MagicFormula ──
    ranked = valuation(altman_pass, config)
    logger.info("MagicFormula ranked: %d", len(ranked))

    # ── 6. Fair value + buy zone ──
    for s in ranked:
        fv = fair_value(s, config)
        s.update({
            "graham": fv["graham"],
            "fv_pe": fv["fv_pe"],
            "fair_value": fv["fair_value"],
            "buy_price": fv["buy_price"],
            "mos_pct": fv["mos_pct"],
            "in_zone": fv["in_zone"],
        })

    # ── 7. Drop diagnoser (only for IN_ZONE) ──
    for s in ranked:
        if s["in_zone"]:
            dd = drop_diagnoser(s, prices_df)
            s["trap_count"] = dd["trap_count"]
            s["trap_flags"] = dd["trap_flags"]
            s["drop_tag"] = dd["tag"]
            s["event_z"] = dd["event_z"]
            s["event_desc"] = dd["event_desc"]
            s["danger_desc"] = f"危险信号 {dd['trap_count']}/7"
        else:
            s["trap_count"] = None
            s["trap_flags"] = []
            s["drop_tag"] = ""
            s["event_z"] = None
            s["event_desc"] = ""
            s["danger_desc"] = ""

    # ── 8. Sector rotation ──
    rrg = sector_rotation(prices_df)

    # ── Market stats → [{k, v}] format ──
    market_stats: list[dict] = []
    spy_row = prices_df.loc[prices_df.index == "SPY"]
    if not spy_row.empty:
        spy_r = spy_row.iloc[0]
        ret21 = round(float(spy_r.get("ret_21", 0) or 0) * 100, 2)
        ret252 = round(float(spy_r.get("ret_252", 0) or 0) * 100, 2)
        market_stats.append({"k": "标普月涨跌", "v": f"{ret21:+.2f}%"})
        market_stats.append({"k": "标普年涨跌", "v": f"{ret252:+.2f}%"})

    # ── 9. Generate digest ──
    zone_stocks = [s for s in ranked if s["in_zone"]]
    digest_info = _compute_digest_score(prices_df)
    digest = generate_digest(zone_stocks, rrg, market_stats, digest_info)

    # ── 10. Funnel counts ──
    funnel = [
        {"n": len(universe), "l": "universe"},
        {"n": len(stocks), "l": "通过质量"},
        {"n": len(altman_pass), "l": "低估"},
        {"n": len(ranked), "l": "观察"},
        {"n": len(zone_stocks), "l": "入场区"},
    ]

    # ── 11. Watch list (near buy zone, not in zone) ──
    watch = _build_watch_list(ranked)

    # ── 12. Leaders (top stocks from hot-sector RRG quadrants) ──
    leaders = _build_leaders(ranked, rrg)

    # ── 13. Build output ──
    output = _build_output(zone_stocks, rrg, market_stats, digest, watch, leaders, funnel)
    _write_output(output, ranked, rrg, dry)


def _compute_range(week_str: str) -> str:
    """Compute date range string from ISO week, e.g. '2026-06-22 ~ 2026-06-27'."""
    try:
        # Parse ISO week string like "2026-W26"
        year, wnum = week_str.split("-W")
        # Monday of that week
        jan4 = datetime(int(year), 1, 4)
        start = jan4 + timedelta(weeks=int(wnum) - 1, days=-jan4.weekday())
        end = start + timedelta(days=4)  # Friday
        return f"{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}"
    except Exception:
        return week_str


def _clean_zone_stock(s: dict) -> dict:
    """Format a zone (IN_ZONE) stock per §DATA schema."""
    # tag: map drop_tag emoji to g/r
    raw_tag = s.get("drop_tag", "")
    if "🟢" in raw_tag or raw_tag == "g":
        tag = "g"
    elif "🔴" in raw_tag or raw_tag == "r":
        tag = "r"
    else:
        tag = "g" if s.get("trap_count", 0) < 2 else "r"

    # momentum_ok: price > ma200 OR ret_21 > 0
    close = s.get("close")
    ma200 = s.get("ma200")
    ret_21 = s.get("ret_21")
    try:
        close_f = float(close) if close else 0
        ma200_f = float(ma200) if ma200 else 0
        ret21_f = float(ret_21) if ret_21 else 0
    except (TypeError, ValueError):
        close_f = ma200_f = ret21_f = 0
    momentum_ok = (ma200_f > 0 and close_f > ma200_f) or ret21_f > 0

    # flags: concatenated human-readable string
    trap_count = s.get("trap_count", 0) or 0
    trap_flags = s.get("trap_flags", [])
    pio_str = s.get("piotroski_str", "")
    alt_str = s.get("altman_z_str", "")
    flags_parts = []
    if trap_count > 0 or trap_flags:
        flags_parts.append(f"危险信号 {trap_count}/7")
    if pio_str:
        flags_parts.append(pio_str)
    if alt_str:
        flags_parts.append(alt_str)
    flags = " · ".join(flags_parts) if flags_parts else "无风险信号"

    # reason: one-line human-readable
    fv = s.get("fair_value")
    buy = s.get("buy_price")
    pe = s.get("pe")
    reason_parts = [f"低估"]
    try:
        if fv and close_f:
            discount = (1 - close_f / float(fv)) * 100
            reason_parts[0] = f"低估·折{discount:.0f}%"
    except (TypeError, ValueError):
        pass
    try:
        if pe:
            reason_parts.append(f"PE {float(pe):.1f}")
    except (TypeError, ValueError):
        pass
    if tag == "r":
        reason_parts.append("有风险信号")
    reason = "·".join(reason_parts)

    return {
        "sym": s.get("ticker_yf"),
        "name": s.get("name"),
        "tag": tag,
        "price": s.get("close"),
        "buy": s.get("buy_price"),
        "low": s.get("low_52w"),
        "high": s.get("high_52w"),
        "momentum_ok": momentum_ok,
        "reason": reason,
        "flags": flags,
        "news": [],
    }


def _build_watch_list(ranked: list[dict]) -> list[dict]:
    """Stocks near buy zone but not in zone. Price < buy * 1.15."""
    watch = []
    for s in ranked:
        if s.get("in_zone"):
            continue
        buy = s.get("buy_price")
        close = s.get("close")
        if buy is None or close is None:
            continue
        try:
            buy_f = float(buy)
            close_f = float(close)
        except (TypeError, ValueError):
            continue
        if buy_f <= 0:
            continue
        # Near buy zone: price within 15% above buy price
        if close_f <= buy_f * 1.15:
            gap_pct = (close_f - buy_f) / buy_f * 100
            pe = s.get("pe")
            roe = s.get("roe")
            try:
                pe_val = round(float(pe), 1) if pe else None
            except (TypeError, ValueError):
                pe_val = None
            try:
                roe_val = f"{float(roe)*100:.0f}%" if roe else None
            except (TypeError, ValueError):
                roe_val = None
            watch.append({
                "sym": s.get("ticker_yf"),
                "name": s.get("name"),
                "price": s.get("close"),
                "buy": s.get("buy_price"),
                "gap": f"{gap_pct:+.1f}%",
                "pe": pe_val,
                "roe": roe_val,
            })
    return watch[:15]  # cap


def _build_leaders(ranked: list[dict], rrg: list[dict]) -> str:
    """Pre-formatted leaders string from hot-sector RRG stocks.
    Format: "科技 XLK: NVDA · MSFT; 医疗 XLV: UNH"
    """
    # Identify hot sectors (Leading or Improving)
    hot_etfs = set()
    for r in rrg:
        if r.get("q") in ("Leading", "Improving"):
            # Extract ETF ticker from "sector ETF" string like "科技 XLK"
            parts = r.get("s", "").split()
            if parts:
                hot_etfs.add(parts[-1])  # last word is ETF ticker

    if not hot_etfs:
        return ""

    # Map ETF → sector name from SPDR_MAP inverse
    etf_to_sector = {v: k for k, v in SPDR_MAP.items()}
    # Actually, SPDR_MAP is ETF→name, so:
    etf_to_name = SPDR_MAP  # e.g., XLK → "Technology"

    sector_groups: dict[str, list[dict]] = {}
    for s in ranked:
        sector = s.get("sector", "")
        ticker = s.get("ticker_yf", "")
        # Find which ETF this sector maps to
        for etf, name in SPDR_MAP.items():
            if name == sector and etf in hot_etfs:
                sector_groups.setdefault(f"{name} {etf}", []).append(s)
                break

    # Pick top 2 per hot sector by ret_21 (recent momentum proxy)
    parts = []
    for sector_label, stocks_in in sector_groups.items():
        sorted_stocks = sorted(
            stocks_in,
            key=lambda x: float(x.get("ret_21", 0) or 0),
            reverse=True,
        )
        syms = [s.get("ticker_yf", "") for s in sorted_stocks[:2] if s.get("ticker_yf")]
        if syms:
            parts.append(f"{sector_label}: {' · '.join(syms)}")

    return "; ".join(parts)


def _build_output(
    zone_stocks: list[dict],
    rrg: list[dict],
    market_stats: list[dict],
    digest: dict,
    watch: list[dict],
    leaders: str,
    funnel: list[dict],
) -> dict:
    """Assemble the final JSON output per §DATA schema."""
    now = datetime.utcnow()
    week_str = now.strftime("%G-W%V")

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "range": _compute_range(week_str),
        "digest": digest,
        "market_stats": market_stats,
        "rrg": rrg,
        "leaders": leaders,
        "zone": [_clean_zone_stock(s) for s in zone_stocks],
        "watch": watch,
        "funnel": funnel,
    }


def _write_output(
    output: dict | list,
    ranked: list,
    rrg: list,
    dry: bool,
) -> None:
    """Write output JSON to hermes_site/data/value/{week}.json and update index."""
    if dry:
        print(json.dumps(output if isinstance(output, dict) else {}, ensure_ascii=False, indent=2))
        return

    week_str = datetime.utcnow().strftime("%G-W%V")
    value_dir = DATA / "value"
    value_dir.mkdir(parents=True, exist_ok=True)

    out_path = value_dir / f"{week_str}.json"
    save_json(out_path, output)
    logger.info("Output written: %s", out_path)

    # Update index
    index_path = value_dir / "index.json"
    zone_count = len(output.get("zone", [])) if isinstance(output, dict) else 0
    funnel = output.get("funnel", []) if isinstance(output, dict) else []
    total_ranked = next((f["n"] for f in funnel if f.get("l") == "观察"), 0)
    update_index(index_path, {
        "id": week_str,
        "week": week_str,
        "file": f"{week_str}.json",
        "generated_at": output.get("generated_at", "") if isinstance(output, dict) else "",
        "total_ranked": total_ranked,
        "total_in_zone": zone_count,
    })
    logger.info("Index updated: %s", index_path)

    # Journal summary
    logger.info(
        "Pipeline A summary: week=%s total_ranked=%s total_in_zone=%s",
        week_str,
        total_ranked,
        zone_count,
    )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    dry_mode = "--dry" in sys.argv
    try:
        run_all(dry=dry_mode)
        logger.info("Pipeline A — DONE")
    except Exception as exc:
        logger.exception("Pipeline A FAILED: %s", exc)
        sys.exit(1)
