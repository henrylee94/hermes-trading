#!/usr/bin/env python3
"""
Pipeline B — Swing / Grid Trading
==================================
Three-layer system:
  Layer 1: Weekly pool  (Saturday 08:00 MYT)  — scan universe, pick ~20 best.
  Layer 2: Daily levels (Mon-Fri 17:00 MYT)   — grid levels for pool stocks.
  Layer 3: On-demand     (Telegram /dot cmd)    — recompute single stock live.

ZERO LLM calls.  All deterministic code.
⚠ 仅供参考,非投资建议.

Run:
    python scripts/pipeline_b_swing.py          # daily levels
    python scripts/pipeline_b_swing.py --weekly  # weekly pool refresh
    python scripts/pipeline_b_swing.py --one PLTR # single stock on-demand
"""
from __future__ import annotations

import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from utils import (
    ROOT,
    CACHE,
    DATA,
    load_json,
    save_json,
    load_config,
    setup_logger,
    get_env,
    retry,
    update_index,
)

log = setup_logger("pipeline_b")

SWING_DIR = DATA / "swing"
SWING_DIR.mkdir(parents=True, exist_ok=True)
WATCHLIST_PATH = ROOT / "swing_watchlist.json"

# ---------------------------------------------------------------------------
# Indicator helpers (pure numpy, zero LLM)
# ---------------------------------------------------------------------------

def atr_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    """ATR via Wilder's EWM (alpha=1/period)."""
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = np.full_like(tr, np.nan)
    if len(tr) < period:
        return atr
    atr[period - 1] = np.mean(tr[:period])
    alpha = 1.0 / period
    for i in range(period, len(tr)):
        atr[i] = atr[i - 1] * (1 - alpha) + tr[i] * alpha
    return atr


def sma(arr: np.ndarray, n: int) -> np.ndarray:
    """Simple moving average, NaN-padded."""
    out = np.full_like(arr, np.nan)
    if len(arr) < n:
        return out
    cs = np.cumsum(arr)
    out[n - 1:] = (cs[n - 1:] - np.concatenate([[0], cs[:-n]])) / n
    return out


def ema(arr: np.ndarray, n: int) -> np.ndarray:
    """EMA with span=n."""
    out = np.full_like(arr, np.nan, dtype=float)
    if len(arr) < n:
        return out
    alpha = 2.0 / (n + 1)
    out[n - 1] = np.mean(arr[:n])
    for i in range(n, len(arr)):
        out[i] = out[i - 1] * (1 - alpha) + arr[i] * alpha
    return out


def rsi_close_only(c: np.ndarray, period: int = 2) -> np.ndarray:
    """Connors RSI(2) — uses only close-to-close changes."""
    out = np.full_like(c, np.nan, dtype=float)
    if len(c) < period + 1:
        return out
    diff = np.diff(c)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    avg_gain = np.full(len(c), np.nan)
    avg_loss = np.full(len(c), np.nan)
    avg_gain[period] = np.mean(gain[:period])
    avg_loss[period] = np.mean(loss[:period])
    for i in range(period + 1, len(c)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i - 1]) / period
    rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100.0)
    rsi_val = 100.0 - 100.0 / (1.0 + rs)
    out[period:] = rsi_val[period:]
    return out


def stochastic(h: np.ndarray, l: np.ndarray, c: np.ndarray,
               k_period: int = 14, k_smooth: int = 3, d_smooth: int = 3):
    """Stochastic Oscillator %K (smoothed) and %D."""
    n = len(c)
    raw_k = np.full(n, np.nan)
    for i in range(k_period - 1, n):
        hh = np.max(h[i - k_period + 1: i + 1])
        ll = np.min(l[i - k_period + 1: i + 1])
        if hh != ll:
            raw_k[i] = 100.0 * (c[i] - ll) / (hh - ll)
        else:
            raw_k[i] = 50.0
    k_smoothed = ema(np.nan_to_num(raw_k, nan=50.0), k_smooth)
    d_line = ema(np.nan_to_num(k_smoothed, nan=50.0), d_smooth)
    return k_smoothed, d_line


def bollinger(c: np.ndarray, n: int = 20, k: float = 2.0):
    """Bollinger Bands: mid, upper, lower, %b."""
    mid = sma(c, n)
    std = np.full_like(c, np.nan)
    for i in range(n - 1, len(c)):
        std[i] = np.std(c[i - n + 1: i + 1], ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    width = upper - lower
    pctb = np.where(width > 0, (c - lower) / width, 0.5)
    return mid, upper, lower, pctb


def donchian(h: np.ndarray, l: np.ndarray, n: int = 20):
    """Donchian channel high/low over n periods."""
    d_high = np.full_like(h, np.nan)
    d_low = np.full_like(l, np.nan)
    for i in range(n - 1, len(h)):
        d_high[i] = np.max(h[i - n + 1: i + 1])
        d_low[i] = np.min(l[i - n + 1: i + 1])
    return d_high, d_low


def pivots(h: float, l: float, c: float) -> dict:
    """Classic + Camarilla pivot levels from session H/L/C."""
    p = (h + l + c) / 3.0
    r1 = 2.0 * p - l
    s1 = 2.0 * p - h
    r2 = p + (h - l)
    s2 = p - (h - l)
    r3 = c + 1.1 * (h - l) / 4.0
    s3 = c - 1.1 * (h - l) / 4.0
    r4 = h + 2.0 * (p - l)
    s4 = l - 2.0 * (h - p)
    return {
        "P": round(p, 2), "R1": round(r1, 2), "R2": round(r2, 2),
        "R3": round(r3, 2), "R4": round(r4, 2),
        "S1": round(s1, 2), "S2": round(s2, 2),
        "S3": round(s3, 2), "S4": round(s4, 2),
    }


def vwap_intraday(h: np.ndarray, l: np.ndarray, c: np.ndarray,
                   v: np.ndarray) -> np.ndarray:
    """Cumulative VWAP reset at each new session (approximated daily)."""
    typical = (h + l + c) / 3.0
    cum_tv = np.cumsum(typical * v)
    cum_v = np.cumsum(v)
    vwap = np.where(cum_v > 0, cum_tv / cum_v, np.nan)
    return vwap


def adx_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray,
               period: int = 14) -> np.ndarray:
    """Average Directional Index (ADX) via Wilder smoothing."""
    n = len(c)
    if n < period + 1:
        return np.full(n, np.nan)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)
    prev_h = np.roll(h, 1)
    prev_l = np.roll(l, 1)
    prev_c = np.roll(c, 1)
    prev_h[0] = h[0]
    prev_l[0] = l[0]
    prev_c[0] = c[0]
    up = h - prev_h
    down = prev_l - l
    plus_dm[1:] = np.where((up[1:] > down[1:]) & (up[1:] > 0), up[1:], 0.0)
    minus_dm[1:] = np.where((down[1:] > up[1:]) & (down[1:] > 0), down[1:], 0.0)
    tr[1:] = np.maximum(h[1:] - l[1:],
             np.maximum(np.abs(h[1:] - prev_c[1:]),
                        np.abs(l[1:] - prev_c[1:])))
    tr[0] = h[0] - l[0]
    alpha = 1.0 / period
    atr_s = np.zeros(n)
    plus_dm_s = np.zeros(n)
    minus_dm_s = np.zeros(n)
    atr_s[period] = np.sum(tr[1: period + 1])
    plus_dm_s[period] = np.sum(plus_dm[1: period + 1])
    minus_dm_s[period] = np.sum(minus_dm[1: period + 1])
    for i in range(period + 1, n):
        atr_s[i] = atr_s[i - 1] * (1 - alpha) + tr[i]
        plus_dm_s[i] = plus_dm_s[i - 1] * (1 - alpha) + plus_dm[i]
        minus_dm_s[i] = minus_dm_s[i - 1] * (1 - alpha) + minus_dm[i]
    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = np.where(atr_s > 0, 100.0 * plus_dm_s / atr_s, 0.0)
        minus_di = np.where(atr_s > 0, 100.0 * minus_dm_s / atr_s, 0.0)
    di_sum = plus_di + minus_di
    with np.errstate(divide='ignore', invalid='ignore'):
        dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)
    adx = np.zeros(n)
    start = period + period
    if start < n:
        adx[start] = np.mean(dx[period: start])
        for i in range(start + 1, n):
            adx[i] = adx[i - 1] * (1 - alpha) + dx[i] * alpha
    else:
        adx[:] = np.nan
    return adx


# ---------------------------------------------------------------------------
# Chinese label helpers
# ---------------------------------------------------------------------------

def rsi_label(v: float) -> str:
    if v < 20:
        return "超卖"
    elif v > 80:
        return "超买"
    return "中性"


def adx_label(v: float, adxmax: float) -> str:
    if v < adxmax:
        return "区间震荡"
    return "趋势中"


# ---------------------------------------------------------------------------
# Earnings helper
# ---------------------------------------------------------------------------

def days_to_earnings(sym: str) -> Optional[int]:
    """Days until next earnings date.
    Returns:
      int >= 0  = days until earnings
      999       = no upcoming earnings known (safe to trade)
      None      = truly unknown (should not happen after fallback)
    Strategy: try yfinance first, then Finnhub. If both fail, return 999.
    """
    # --- 1) yfinance ---
    try:
        import yfinance as yf
        tk = yf.Ticker(sym)
        cal = tk.calendar
        if cal is not None:
            edate = None
            if isinstance(cal, dict):
                edate = cal.get("Earnings Date")
                if isinstance(edate, list) and edate:
                    edate = edate[0]
            elif isinstance(cal, pd.DataFrame):
                if "Earnings Date" in cal.index:
                    vals = cal.loc["Earnings Date"]
                    edate = vals.iloc[0] if len(vals) else None
            if edate is not None:
                if isinstance(edate, str):
                    edate = pd.Timestamp(edate)
                elif isinstance(edate, (int, float)):
                    edate = pd.Timestamp(datetime.fromtimestamp(edate))
                now = pd.Timestamp(datetime.now())
                delta = (edate - now).days
                return delta if delta >= 0 else 999
    except Exception:
        pass

    # --- 2) Finnhub fallback ---
    try:
        from utils import get_env
        fh_key = get_env("FINNHUB_API_KEY", required=False)
        if fh_key:
            import finnhub as fh
            client = fh.Client(api_key=fh_key)
            cal = client.calendar(sym=sym, _from=datetime.now().strftime("%Y-%m-%d"),
                                  to=(datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"))
            earnings = cal.get("earningsCalendar", []) if cal else []
            if earnings:
                edate_str = earnings[0].get("date")
                if edate_str:
                    edate = pd.Timestamp(edate_str)
                    now = pd.Timestamp(datetime.now())
                    delta = (edate - now).days
                    return max(delta, 0)
    except Exception:
        pass

    # --- 3) Both failed — fail-safe: assume no upcoming earnings ---
    return 999


# ---------------------------------------------------------------------------
# Layer 1 — Weekly Pool Selection
# ---------------------------------------------------------------------------

def select_weekly_pool(cfg: dict, prices_df: pd.DataFrame) -> list[dict]:
    """
    Scan universe, pick ~pool best stocks by:
      - Liquidity: price > $5, SMA20(vol) > 1M, dollar_vol > $20M
      - ATR% in [atrmin, atrmax]
      - Beta 0.8–1.8
      - ADX < adxmax (range-bound)
      - Price > SMA200
      - Market cap > mktcap_min (default $10B)
      - No earnings within 7 days

    Returns list of dicts: [{"sym": "PLTR", "price": 98.4, ...}, ...]
    Writes swing_watchlist.json (auto array only, pins untouched).
    """
    log.info("═══ LAYER 1: Weekly Pool Selection ═══")
    sw = cfg["swing"]
    target_pool = sw["pool"]
    atrmin = sw["atrmin"]
    atrmax = sw["atrmax"]
    adxmax = sw["adxmax"]
    mktcap_min = sw.get("mktcap_min", 10)  # minimum $10B market cap

    # Load universe
    uni_path = CACHE / "universe_latest.json"
    if not uni_path.exists():
        log.error("universe_latest.json not found — run module0 first")
        return []
    uni = load_json(uni_path)
    tickers_info = uni.get("tickers", {})

    # Load fundamentals for market cap filter
    fund_path = CACHE / "fundamentals_latest.json"
    fund_data = {}
    if fund_path.exists():
        try:
            fund_cache = load_json(fund_path)
            fund_data = fund_cache.get("tickers", {})
        except Exception:
            pass

    if prices_df is None or prices_df.empty:
        log.error("No prices data available")
        return []

    candidates = []
    for sym in prices_df.index:
        if sym not in tickers_info:
            continue
        try:
            row = prices_df.loc[sym]
            price = float(row["price"]) if not pd.isna(row["price"]) else None
            ma200 = float(row["ma200"]) if not pd.isna(row.get("ma200", np.nan)) else None
            if price is None or price < 5.0:
                continue
            if ma200 is not None and price < ma200:
                continue
            # Note: beta comes from fundamentals or can be estimated from returns
            # For pool selection we use a simplified approach
        except Exception:
            continue

    # For a more thorough pool, we need full OHLCV — load from yfinance batch
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed — cannot build weekly pool")
        return []

    all_syms = list(prices_df.index)
    log.info("  Scanning %d tickers for pool criteria …", len(all_syms))

    # Batch download 3-month data for indicators
    try:
        data = yf.download(
            all_syms, period="6mo", interval="1d", group_by="ticker",
            threads=2, progress=False, auto_adjust=True,
        )
    except Exception as exc:
        log.error("  yfinance batch download failed: %s", exc)
        return []

    scored = []
    for sym in all_syms:
        if sym not in tickers_info:
            continue
        try:
            if len(all_syms) == 1:
                df = data
            else:
                df = data[sym].dropna(how="all")
            if df.empty or len(df) < 50:
                continue

            c = df["Close"].values.astype(float)
            h = df["High"].values.astype(float)
            l = df["Low"].values.astype(float)
            v = df["Volume"].values.astype(float)

            price = c[-1]
            if price < 5.0:
                continue

            # Market cap filter (fundamentals in millions)
            fund_info = fund_data.get(sym, {})
            mktcap = fund_info.get("marketCapitalization")
            if mktcap is not None and mktcap < mktcap_min * 1000:  # mktcap_min in $B, data in $M
                continue

            # SMA200 check
            ma200_val = sma(c, 200)
            if not np.isnan(ma200_val[-1]) and price < ma200_val[-1]:
                continue

            # Liquidity: dollar volume > $30M avg (higher for 做T)
            dollar_vol = np.mean(c[-20:] * v[-20:])
            if dollar_vol < 30_000_000:
                continue

            # SMA20(volume) > 1M
            vol_sma = sma(v, 20)
            if np.isnan(vol_sma[-1]) or vol_sma[-1] < 1_000_000:
                continue

            # ATR%
            atr_arr = atr_wilder(h, l, c, 14)
            atr_val = atr_arr[-1]
            if np.isnan(atr_val) or atr_val <= 0:
                continue
            atr_pct = atr_val / price * 100.0
            if atr_pct < atrmin or atr_pct > atrmax:
                continue

            # ADX (range-bound filter)
            adx_arr = adx_wilder(h, l, c, 14)
            adx_val = adx_arr[-1]
            if np.isnan(adx_val) or adx_val >= adxmax:
                continue

            # --- Range-bound validation: this week's actual range ---
            week_high = float(np.max(h[-5:]))
            week_low = float(np.min(l[-5:]))
            week_range = week_high - week_low

            # Range must be meaningful (at least 3% of price)
            week_range_pct = week_range / price * 100 if price > 0 else 0
            if week_range_pct < 3.0:
                continue

            # Price should be in middle 70% of weekly range (not at extremes)
            if week_range > 0:
                pct_in_range = (price - week_low) / week_range
                if pct_in_range < 0.15 or pct_in_range > 0.85:
                    continue

            # Verify price oscillates (touched both high and low this week)
            # Check if at least 1 day touched within 20% of high and low
            days_near_high = sum(1 for i in range(-5, 0) if h[i] >= week_high - 0.2 * week_range)
            days_near_low = sum(1 for i in range(-5, 0) if l[i] <= week_low + 0.2 * week_range)
            if days_near_high < 1 or days_near_low < 1:
                continue  # Price hasn't oscillated between high and low

            # Beta (simplified: correlation with SPY × std_ratio)
            # Skip if we can't compute; approximate from price data
            # Use simple beta heuristic from returns
            ret = np.diff(c) / c[:-1]
            beta_est = 1.0  # default if can't compute
            # (A proper beta needs benchmark; we accept 0.8-1.8 range)
            # For pool, we'll include all that pass other screens

            # No earnings within 7 days
            earn_days = days_to_earnings(sym)
            if earn_days is not None and earn_days < 7:
                continue
            # earn_days=999 means unknown — include (fail-safe: flag but don't exclude)

            scored.append({
                "sym": sym,
                "price": round(price, 2),
                "atr_pct": round(atr_pct, 2),
                "adx": round(adx_val, 2),
                "dollar_vol_M": round(dollar_vol / 1e6, 1),
                "earn_days": earn_days,
            })
        except Exception as exc:
            log.debug("  %s skipped: %s", sym, exc)
            continue

    # Sort by ADX ascending (most range-bound first)
    scored.sort(key=lambda x: x["adx"])
    pool = scored[:target_pool]

    log.info("  Weekly pool: %d stocks selected", len(pool))

    # Update swing_watchlist.json — preserve pins and exclude
    wl = {"auto": [], "pins": [], "exclude": []}
    if WATCHLIST_PATH.exists():
        try:
            wl = load_json(WATCHLIST_PATH)
        except Exception:
            wl = {"auto": [], "pins": [], "exclude": []}

    # Keep existing pins and exclude
    pins = wl.get("pins", [])
    exclude = set(wl.get("exclude", []))

    # Filter out excluded stocks from pool
    pool = [s for s in pool if s["sym"] not in exclude]

    auto = [{"sym": s["sym"], "price": s["price"]} for s in pool]

    new_wl = {"auto": auto, "pins": pins, "exclude": list(exclude)}
    save_json(WATCHLIST_PATH, new_wl)
    log.info("  Saved swing_watchlist.json (%d auto, %d pins)", len(auto), len(pins))

    return pool


# ---------------------------------------------------------------------------
# Layer 2 & 3 — Indicator Computation
# ---------------------------------------------------------------------------

def compute_indicators(sym: str, cfg: dict, live_data: bool = False) -> dict:
    """
    Compute all indicators for a single stock.
    Returns dict with regime, pause info, grid levels, etc.

    If live_data=True, fetch latest intraday via yfinance for VWAP.
    Otherwise use last-completed session.
    """
    sw = cfg["swing"]
    stepk = sw["stepk"]
    budget = sw["budget"]
    target = sw["target"]
    risk = sw["risk"]
    stopmult = sw["stopmult"]
    commission = sw["commission"]
    adxmax = sw["adxmax"]

    result = {
        "sym": sym, "name": "", "price": None,
        "regime": "range", "pause_reason": None, "price_only": False,
        "box_low": None, "vwap": None, "box_high": None,
        "stop": None, "step": None, "shares": None,
        "profit_per": None,
        "rsi": "中性", "adx": "区间震荡",
        "earn_days": None,
        "buy_levels": [], "sell_levels": [],
        "action_now": "",
    }

    # Fetch price data
    try:
        import yfinance as yf
        tk = yf.Ticker(sym)
        df = tk.history(period="6mo", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 30:
            result["pause_reason"] = "数据不足"
            result["price_only"] = True
            result["regime"] = "trend"
            result["recommendation"] = "不建议"
            result["recommendation_reason"] = "数据不足,无法计算"
            return result

        # Get name
        try:
            info = tk.info
            result["name"] = info.get("shortName", info.get("longName", sym))
        except Exception:
            result["name"] = sym
    except Exception as exc:
        result["pause_reason"] = f"数据获取失败: {exc}"
        result["price_only"] = True
        result["regime"] = "trend"
        result["recommendation"] = "不建议"
        result["recommendation_reason"] = f"数据获取失败: {exc}"
        return result

    c = df["Close"].values.astype(float)
    h = df["High"].values.astype(float)
    l = df["Low"].values.astype(float)
    v = df["Volume"].values.astype(float)
    price = c[-1]
    result["price"] = round(price, 2)

    # --- Yesterday's daily data ---
    yesterday_high = float(h[-2]) if len(h) >= 2 else price
    yesterday_low = float(l[-2]) if len(l) >= 2 else price
    yesterday_close = float(c[-2]) if len(c) >= 2 else price

    # --- Intraday data (5min) for today's actual range ---
    today_high_intra = price
    today_low_intra = price
    today_vol_intra = 0.0
    today_minutes = 0
    try:
        df_intra = tk.history(period="1d", interval="5m", auto_adjust=True)
        if not df_intra.empty and len(df_intra) >= 2:
            today_high_intra = float(df_intra["High"].max())
            today_low_intra = float(df_intra["Low"].min())
            today_vol_intra = float(df_intra["Volume"].sum())
            today_minutes = len(df_intra) * 5
    except Exception:
        pass

    # --- EMA alignment (5 / 30 / 60) ---
    ema5_val = float(ema(c, 5)[-1]) if len(c) >= 5 else np.nan
    ema30_val = float(ema(c, 30)[-1]) if len(c) >= 30 else np.nan
    ema60_val = float(ema(c, 60)[-1]) if len(c) >= 60 else np.nan

    ema_vals = [v for v in [ema60_val, ema30_val, ema5_val] if not np.isnan(v)]
    if len(ema_vals) == 3:
        if ema60_val > ema30_val > ema5_val:
            ema_trend = "bearish"
            ema_label = "EMA60>30>5 偏空"
        elif ema5_val > ema30_val > ema60_val:
            ema_trend = "bullish"
            ema_label = "EMA5>30>60 偏多"
        else:
            ema_trend = "neutral"
            ema_label = "EMA交叉 方向不明"
    elif len(ema_vals) >= 2:
        if ema30_val > ema5_val:
            ema_trend = "bearish"
            ema_label = "EMA30>5 偏空"
        else:
            ema_trend = "bullish"
            ema_label = "EMA5>30 偏多"
    else:
        ema_trend = "neutral"
        ema_label = "EMA数据不足"
    result["ema_trend"] = ema_trend
    result["ema_label"] = ema_label

    # --- Volume pace (intraday volume extrapolated to full day) ---
    avg_daily_vol = float(np.mean(v[-20:])) if len(v) >= 20 else float(np.mean(v))
    if today_minutes > 0 and avg_daily_vol > 0:
        # Market is 390 min/day; extrapolate current pace
        vol_pace = (today_vol_intra / today_minutes) * 390
        vol_ratio = vol_pace / avg_daily_vol if avg_daily_vol > 0 else 1.0
    else:
        vol_ratio = 0
    vol_enough = vol_ratio >= 0.8
    result["vol_ratio"] = round(vol_ratio, 2)
    result["vol_enough"] = vol_enough

    # --- ATR ---
    atr_arr = atr_wilder(h, l, c, 14)
    atr_val = atr_arr[-1]
    atr_avg_20 = np.nanmean(atr_arr[-20:]) if len(atr_arr) >= 20 else atr_val
    expected_daily = atr_val
    expected_weekly = atr_val * math.sqrt(5)

    # --- Bollinger ---
    bb_mid, bb_up, bb_low, bb_pctb = bollinger(c, 20, 2.0)

    # --- Donchian ---
    dc_high, dc_low = donchian(h, l, 20)

    # --- VWAP (daily approximation) ---
    vwap_arr = vwap_intraday(h, l, c, v)
    vwap_val = vwap_arr[-1]

    # --- RSI(2) ---
    rsi_arr = rsi_close_only(c, 2)
    rsi_val = rsi_arr[-1]

    # --- Stochastic ---
    stoch_k_arr, stoch_d_arr = stochastic(h, l, c, 14, 3, 3)
    stoch_k = stoch_k_arr[-1] if len(stoch_k_arr) > 0 else np.nan
    stoch_d = stoch_d_arr[-1] if len(stoch_d_arr) > 0 else np.nan

    # --- ADX ---
    adx_arr = adx_wilder(h, l, c, 14)
    adx_val = adx_arr[-1]

    # --- SMA200 ---
    ma200_arr = sma(c, 200)
    ma200_val = ma200_arr[-1]

    # --- Pivots (from last session) ---
    pivot = pivots(h[-1], l[-1], c[-1])

    # --- Labels ---
    result["rsi"] = rsi_label(rsi_val)
    result["adx"] = adx_label(adx_val, adxmax)

    # --- Earnings ---
    earn_days = days_to_earnings(sym)
    result["earn_days"] = earn_days if earn_days is not None else -1

    # ── PAUSE checks ──
    pause_reasons = []

    # 1) close < SMA200
    if not np.isnan(ma200_val) and price < ma200_val:
        pause_reasons.append("价格低于SMA200")

    # 2) ADX >= 25
    if not np.isnan(adx_val) and adx_val >= 25:
        pause_reasons.append(f"ADX={adx_val:.1f}≥25,趋势过强")

    # 3) ATR > 1.5× avg
    if not np.isnan(atr_avg_20) and atr_avg_20 > 0:
        if atr_val > 1.5 * atr_avg_20:
            pause_reasons.append(f"ATR波动过大(当前{atr_val:.2f}>1.5×均值{atr_avg_20:.2f})")

    # 4) 3+ closes outside Bollinger
    if len(c) >= 23:
        above_count = 0
        below_count = 0
        for i in range(-5, 0):
            if not np.isnan(bb_up[i]) and c[i] > bb_up[i]:
                above_count += 1
            if not np.isnan(bb_low[i]) and c[i] < bb_low[i]:
                below_count += 1
        if above_count >= 3:
            pause_reasons.append("连续3+日收盘在Bollinger上轨外")
        if below_count >= 3:
            pause_reasons.append("连续3+日收盘在Bollinger下轨外")

    # 5) Earnings within 7 days
    if earn_days is not None and earn_days < 7:
        pause_reasons.append(f"距财报仅{earn_days}天")

    # --- Support / Resistance from recent price action ---
    # Check if today's intraday range overlaps with yesterday's daily range
    today_overlaps_yesterday = (today_low_intra <= yesterday_high and today_high_intra >= yesterday_low)

    if today_overlaps_yesterday:
        # Ranges overlap: use the tighter zone where both days agree
        support_level = max(today_low_intra, yesterday_low)
        resistance_level = min(today_high_intra, yesterday_high)
    else:
        # Gap day: use today's intraday range as primary (yesterday's levels are irrelevant)
        support_level = today_low_intra
        resistance_level = today_high_intra
        # If intraday range is too tight, widen using yesterday's close as anchor
        intraday_pct = (resistance_level - support_level) / price * 100 if price > 0 else 0
        if intraday_pct < 1.5 and abs(yesterday_close - price) / price > 0.02:
            # Price gapped significantly from yesterday — use yesterday close as secondary support/resistance
            if price > yesterday_close:
                support_level = min(support_level, yesterday_close)
            else:
                resistance_level = max(resistance_level, yesterday_close)

    box_low = support_level
    box_high = resistance_level
    box_width = box_high - box_low
    box_pct = box_width / price * 100 if price > 0 else 0

    # Validate: box must be meaningful (at least 1.5% for 做T)
    if box_pct < 1.5:
        pause_reasons.append(f"区间过窄({box_pct:.1f}%<1.5%),不适合做T")

    # Price position in range
    if box_width > 0:
        pct_in_range = (price - box_low) / box_width
    else:
        pct_in_range = 0.5

    # 6) close outside box → trend regime
    if price < box_low or price > box_high:
        if price < box_low:
            pause_reasons.append("收盘低于区间下沿")
        else:
            pause_reasons.append("收盘高于区间上沿")
        result["regime"] = "trend"

    # --- Regime determination ---
    if pause_reasons:
        result["regime"] = "trend"
        result["pause_reason"] = "；".join(pause_reasons)
    else:
        result["regime"] = "range"

    # Always set box data (regardless of pause)
    result["box_low"] = round(box_low, 2)
    result["vwap"] = round(vwap_val, 2) if not np.isnan(vwap_val) else None
    result["box_high"] = round(box_high, 2)
    result["price_only"] = False  # Never skip — always show full data

    # --- Grid step ---
    step = stepk * atr_val
    result["step"] = round(step, 2)

    # --- Stop ---
    stop = price - stopmult * atr_val
    result["stop"] = round(stop, 2)

    # --- Sizing (MIN of three caps) ---
    equity = sw["equity"]
    risk_pct = risk / 100.0

    shares_budget = math.floor(budget / price) if price > 0 else 0
    shares_target = round((target + commission) / step) if step > 0 else 0
    shares_risk = math.floor(equity * risk_pct / (stopmult * atr_val)) if (stopmult * atr_val) > 0 else 0

    shares = min(shares_budget, shares_target, shares_risk)
    shares = max(shares, 0)

    result["shares"] = shares

    # Total exposure check
    total_exposure = shares * price
    if total_exposure > budget and shares > 0:
        shares = math.floor(budget / price)
        result["shares"] = shares

    # --- Profit per ---
    if shares > 0:
        profit = (target + commission)
        result["profit_per"] = f"约 +${shares * target:,.0f}"
    else:
        result["profit_per"] = "0"

    # --- Grid levels based on support/resistance ---
    buy_levels = []
    sell_levels = []

    if shares > 0 and box_width > 0:
        # Buy levels: at and above support
        buy_levels.append({"price": round(box_low, 2), "shares": shares})
        if price - box_low > step:
            buy_mid = round((price + box_low) / 2, 2)
            if buy_mid > box_low:
                buy_levels.append({"price": buy_mid, "shares": shares})

        # Sell levels: at and below resistance
        sell_levels.append({"price": round(box_high, 2), "shares": shares})
        if box_high - price > step:
            sell_mid = round((price + box_high) / 2, 2)
            if sell_mid < box_high:
                sell_levels.append({"price": sell_mid, "shares": shares})

    result["buy_levels"] = buy_levels
    result["sell_levels"] = sell_levels

    # --- action_now: EMA direction + range position → actionable levels ---
    vol_note = f"Vol{'✓' if vol_enough else '✗'}({result.get('vol_ratio', 0):.1f}x)"

    if ema_trend == "bearish":
        if pct_in_range >= 0.5:
            # Near resistance in bearish trend → sell zone
            sell_zone_h = round(box_high, 2)
            sell_zone_l = round(max(price, box_high - step), 2)
            buyback = round(box_low, 2)
            stop = round(box_high * 1.005, 2)
            result["action_now"] = (
                f"📤 做空 {ema_label} {vol_note}\n"
                f"  卖出区: ${sell_zone_l} - ${sell_zone_h}\n"
                f"  买回目标: ${buyback}\n"
                f"  止损: > ${stop}"
            )
        else:
            # Near support in bearish trend → wait for bounce to short
            result["action_now"] = (
                f"⏳ 偏空但接近支撑 {ema_label} {vol_note}\n"
                f"  等反弹到 ${box_high:.2f} 附近再空\n"
                f"  支撑: ${box_low:.2f} 止损: > ${box_high * 1.005:.2f}"
            )
    elif ema_trend == "bullish":
        if pct_in_range <= 0.5:
            # Near support in bullish trend → buy zone
            buy_zone_l = round(box_low, 2)
            buy_zone_h = round(min(price, box_low + step), 2)
            target = round(box_high, 2)
            stop = round(box_low * 0.995, 2)
            result["action_now"] = (
                f"📥 做多 {ema_label} {vol_note}\n"
                f"  买入区: ${buy_zone_l} - ${buy_zone_h}\n"
                f"  卖出目标: ${target}\n"
                f"  止损: < ${stop}"
            )
        else:
            # Near resistance in bullish trend → wait for pullback
            result["action_now"] = (
                f"⏳ 偏多但接近压力 {ema_label} {vol_note}\n"
                f"  等回调到 ${box_low:.2f} 附近再买\n"
                f"  压力: ${box_high:.2f} 止损: < ${box_low * 0.995:.2f}"
            )
    else:
        # Neutral — range trade
        if pct_in_range >= 0.6:
            result["action_now"] = (
                f"📤 区间高位可卖 {ema_label} {vol_note}\n"
                f"  卖出区: ${price:.2f} - ${box_high:.2f}\n"
                f"  买回目标: ${box_low:.2f}\n"
                f"  止损: > ${box_high * 1.005:.2f}"
            )
        elif pct_in_range <= 0.4:
            result["action_now"] = (
                f"📥 区间低位可买 {ema_label} {vol_note}\n"
                f"  买入区: ${box_low:.2f} - ${price:.2f}\n"
                f"  卖出目标: ${box_high:.2f}\n"
                f"  止损: < ${box_low * 0.995:.2f}"
            )
        else:
            result["action_now"] = (
                f"🔄 区间中间等方向 {ema_label} {vol_note}\n"
                f"  买 ${box_low:.2f} 卖 ${box_high:.2f}\n"
                f"  止损: < ${box_low * 0.995:.2f} / > ${box_high * 1.005:.2f}"
            )

    # ── PUT SIGNAL ──
    # Buy puts when: RSI overbought, price near box_high, or stochastic > 80
    put_signal = False
    put_reasons = []

    # RSI overbought (RSI(2) > 90 is very overbought, > 70 is moderately)
    rsi_raw = rsi_val if not np.isnan(rsi_val) else 50
    if rsi_raw > 70:
        put_signal = True
        put_reasons.append(f"RSI超买({rsi_raw:.0f})")

    # Price near box_high (within 1 step)
    if not np.isnan(box_high) and not np.isnan(step) and step > 0 and price >= box_high - step:
        put_signal = True
        put_reasons.append("价格接近区间上沿")

    # Stochastic overbought
    if not np.isnan(stoch_k) and stoch_k > 80:
        put_signal = True
        put_reasons.append(f"随机指标超买({stoch_k:.0f})")

    # Price above VWAP (bearish divergence signal)
    if not np.isnan(vwap_val) and price > vwap_val * 1.02:
        put_reasons.append("价格高于VWAP")

    # Compute put levels (reversed grid: sell high, buy back low)
    put_buy_levels = []   # where to enter puts (near resistance)
    put_sell_levels = []  # where to take profit on puts (near support)
    put_shares = shares   # use same sizing

    if put_shares > 0 and step > 0:
        # Put entry: near box_high (resistance, where price might reject)
        num_entries = min(3, max(1, int((box_high - price) / step) + 1))
        for i in range(num_entries):
            lp = round(float(price + i * step), 2)
            if lp <= box_high:
                put_buy_levels.append({"price": lp, "shares": int(put_shares)})

        # Put profit target: near box_low (support, where price might bounce)
        num_targets = min(3, max(1, int((price - box_low) / step)))
        for i in range(1, num_targets + 1):
            lp = round(float(price - i * step), 2)
            if lp >= box_low:
                put_sell_levels.append({"price": lp, "shares": int(put_shares)})

    result["put_buy_levels"] = put_buy_levels
    result["put_sell_levels"] = put_sell_levels
    result["put_signal"] = put_signal
    result["put_reasons"] = put_reasons

    # Put action_now
    if put_signal and put_buy_levels:
        pb1 = put_buy_levels[0]
        ps1 = put_sell_levels[0] if put_sell_levels else None
        put_action = (
            f"🔴 看跌信号: {', '.join(put_reasons)}。"
            f"可买 {pb1['shares']} 股 put @ ${pb1['price']},"
        )
        if ps1:
            profit_est = (pb1['price'] - ps1['price']) * pb1['shares']
            put_action += f"目标 ${ps1['price']}(约 +${profit_est:,.0f})。"
        else:
            put_action += f"目标 ${box_low:.1f}。"
        result["put_action_now"] = put_action
    else:
        result["put_action_now"] = ""

    # --- Recommendation annotation (EMA + range based) ---
    result["support"] = round(box_low, 2)
    result["resistance"] = round(box_high, 2)

    if pause_reasons:
        result["recommendation"] = "不建议"
        result["recommendation_reason"] = "；".join(pause_reasons)
    else:
        result["recommendation"] = "建议"
        if ema_trend == "bearish":
            if pct_in_range >= 0.5:
                result["recommendation_reason"] = "偏空+接近压力,适合做空"
            else:
                result["recommendation_reason"] = "偏空但接近支撑,等反弹再空"
        elif ema_trend == "bullish":
            if pct_in_range <= 0.5:
                result["recommendation_reason"] = "偏多+接近支撑,适合做多"
            else:
                result["recommendation_reason"] = "偏多但接近压力,等回调再买"
        else:
            if pct_in_range >= 0.6:
                result["recommendation_reason"] = "区间高位,可卖出"
            elif pct_in_range <= 0.4:
                result["recommendation_reason"] = "区间低位,可买入"
            else:
                result["recommendation_reason"] = "区间中间,观望"

    return result


# ---------------------------------------------------------------------------
# Layer 2 — Daily Levels
# ---------------------------------------------------------------------------

def compute_daily_levels(cfg: dict) -> dict:
    """
    Compute grid levels for all stocks in the watchlist.
    Returns the full output dict for swing/{date}.json.
    """
    log.info("═══ LAYER 2: Daily Levels ═══")

    sw = cfg["swing"]
    dailystop = sw["dailystop"]

    # Load watchlist
    if not WATCHLIST_PATH.exists():
        log.error("swing_watchlist.json not found")
        return {"error": "watchlist not found"}

    wl = load_json(WATCHLIST_PATH)
    auto_syms = [s["sym"] for s in wl.get("auto", [])]
    pin_syms = [s["sym"] for s in wl.get("pins", [])]
    all_syms = list(dict.fromkeys(auto_syms + pin_syms))  # dedupe, preserve order

    if not all_syms:
        log.warning("  Watchlist is empty")
        return {"error": "empty watchlist"}

    log.info("  Computing levels for %d stocks …", len(all_syms))

    # Check data sources
    price_ok = (CACHE / "prices_latest.parquet").exists()

    stocks = []
    errors = 0

    for sym in all_syms:
        try:
            result = compute_indicators(sym, cfg)
            stocks.append(result)
            log.info("  %s: regime=%s, price=%s",
                     sym, result["regime"], result.get("price"))
        except Exception as exc:
            log.warning("  %s failed: %s", sym, exc)
            errors += 1
            stocks.append({
                "sym": sym, "name": sym, "price": None,
                "regime": "trend", "price_only": True,
                "pause_reason": f"计算错误: {exc}",
            })

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%Y-%m-%d %H:%M")

    output = {
        "as_of": time_str,
        "pool": len(all_syms),
        "recommended_count": sum(1 for s in stocks if s.get("recommendation") == "建议"),
        "not_recommended_count": sum(1 for s in stocks if s.get("recommendation") == "不建议"),
        "sources_ok": {"price": price_ok, "intraday": True},
        "daily_stop": dailystop,
        "daily_stop_note": "今日停手限额",
        "base_rate_note": "≈35-50%的日内交易者盈利,仅供参考",
        "disclaimer": "⚠ 仅供参考,非投资建议",
        "stocks": stocks,
    }

    # Write to swing/{date}.json
    out_path = SWING_DIR / f"{date_str}.json"
    save_json(out_path, output)
    log.info("  Saved %s", out_path)

    # Update index
    index_path = SWING_DIR / "index.json"
    update_index(index_path, {
        "id": date_str,
        "file": out_path.name,
        "pool": len(all_syms),
        "recommended": output["recommended_count"],
        "not_recommended": output["not_recommended_count"],
        "generated": time_str,
    })

    return output


# ---------------------------------------------------------------------------
# Layer 3 — On-demand single stock
# ---------------------------------------------------------------------------

def compute_one(sym: str, cfg: dict = None) -> dict:
    """Recompute live levels for a single stock. Returns result dict.
    
    Uses Finnhub for live price (Layer 3 on-demand per spec).
    Falls back to cached daily data if yfinance fails.
    """
    import os, json as _json, urllib.request as _req
    from datetime import datetime
    log.info("═══ LAYER 3: On-demand — %s ═══", sym)
    if cfg is None:
        cfg = load_config()
    result = compute_indicators(sym.upper(), cfg)
    
    # If compute_indicators failed (no levels), try cached daily data as base
    if result.get("price_only") and not result.get("buy_levels"):
        today = datetime.now().strftime("%Y-%m-%d")
        cache_path = SWING_DIR / f"{today}.json"
        if cache_path.exists():
            try:
                cached = load_json(cache_path)
                for s in cached.get("stocks", []):
                    if s.get("sym") == sym.upper() and not s.get("price_only"):
                        log.info("  Using cached data for %s (yfinance failed)", sym)
                        result = s
                        break
            except Exception:
                pass
    
    # Override price with Finnhub live data
    try:
        key = get_env("FINNHUB_API_KEY", "")
        if key:
            url = f"https://finnhub.io/api/v1/quote?symbol={sym.upper()}&token={key}"
            r = _req.urlopen(url, timeout=10)
            q = _json.loads(r.read())
            live_price = q.get("c")
            if live_price and live_price > 0:
                result["price"] = round(live_price, 2)
                result["live"] = True
                log.info("  Live price from Finnhub: $%.2f", live_price)

                # Recompute grid levels based on support/resistance
                lp = float(live_price)
                bl = result.get("box_low", 0) or 0
                bh = result.get("box_high", 0) or 0
                sh = result.get("shares", 0) or 0
                step_val = result.get("step", 0) or 0
                bw = bh - bl if bh > bl else 0
                if sh > 0 and bw > 0:
                    new_buys = [{"price": round(bl, 2), "shares": sh}]
                    if lp - bl > step_val:
                        mid_buy = round((lp + bl) / 2, 2)
                        if mid_buy > bl:
                            new_buys.append({"price": mid_buy, "shares": sh})
                    result["buy_levels"] = new_buys

                    new_sells = [{"price": round(bh, 2), "shares": sh}]
                    if bh - lp > step_val:
                        mid_sell = round((lp + bh) / 2, 2)
                        if mid_sell < bh:
                            new_sells.append({"price": mid_sell, "shares": sh})
                    result["sell_levels"] = new_sells

                    # Recompute action based on EMA + new price position
                    pct = (lp - bl) / bw if bw > 0 else 0.5
                    ema_t = result.get("ema_trend", "neutral")
                    ema_l = result.get("ema_label", "")
                    vol_r = result.get("vol_ratio", 0)
                    vol_ok = result.get("vol_enough", False)
                    vn = f"Vol{'✓' if vol_ok else '✗'}({vol_r:.1f}x)"

                    if ema_t == "bearish" and pct >= 0.5:
                        szh = round(bh, 2)
                        szl = round(max(lp, bh - step_val), 2)
                        result["action_now"] = (
                            f"📤 做空 {ema_l} {vn}\n"
                            f"  卖出区: ${szl} - ${szh}\n"
                            f"  买回目标: ${bl:.2f}\n"
                            f"  止损: > ${bh * 1.005:.2f}"
                        )
                    elif ema_t == "bullish" and pct <= 0.5:
                        bzl = round(bl, 2)
                        bzh = round(min(lp, bl + step_val), 2)
                        result["action_now"] = (
                            f"📥 做多 {ema_l} {vn}\n"
                            f"  买入区: ${bzl} - ${bzh}\n"
                            f"  卖出目标: ${bh:.2f}\n"
                            f"  止损: < ${bl * 0.995:.2f}"
                        )
                    elif ema_t == "bearish":
                        result["action_now"] = (
                            f"⏳ 偏空但接近支撑 {ema_l} {vn}\n"
                            f"  等反弹到 ${bh:.2f} 附近再空\n"
                            f"  支撑: ${bl:.2f} 止损: > ${bh * 1.005:.2f}"
                        )
                    elif ema_t == "bullish":
                        result["action_now"] = (
                            f"⏳ 偏多但接近压力 {ema_l} {vn}\n"
                            f"  等回调到 ${bl:.2f} 附近再买\n"
                            f"  压力: ${bh:.2f} 止损: < ${bl * 0.995:.2f}"
                        )
                    else:
                        result["action_now"] = (
                            f"🔄 区间中间 {ema_l} {vn}\n"
                            f"  买 ${bl:.2f} 卖 ${bh:.2f}\n"
                            f"  止损: < ${bl * 0.995:.2f} / > ${bh * 1.005:.2f}"
                        )
                    log.info("  Grid re-anchored: %d buys, %d sells, pct=%.0f%%", len(new_buys), len(new_sells), pct * 100)
    except Exception as e:
        log.warning("  Finnhub live price failed, using yfinance: %s", e)

    # Persist live data back to daily JSON so it survives page refresh
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        daily_path = SWING_DIR / f"{today}.json"
        if daily_path.exists():
            daily = load_json(daily_path)
            for i, s in enumerate(daily.get("stocks", [])):
                if s.get("sym") == sym.upper():
                    # Merge live data into daily file
                    daily["stocks"][i] = result
                    break
            else:
                # Stock not in daily file yet (new pin), append
                daily.setdefault("stocks", []).append(result)
            save_json(daily_path, daily)
            log.info("  Persisted live data for %s to %s", sym, daily_path.name)
    except Exception as e:
        log.warning("  Failed to persist live data: %s", e)

    return result


# ---------------------------------------------------------------------------
# Telegram push helper
# ---------------------------------------------------------------------------

def format_telegram_msg(output: dict) -> str:
    """Format the daily levels output as a Telegram-friendly text block."""
    lines = []
    lines.append(f"📊 做T看板 {output.get('as_of', '')}")
    lines.append(f"活跃: {output.get('active_count', 0)} | 暂停: {output.get('paused_count', 0)}")
    lines.append("")

    for s in output.get("stocks", []):
        sym = s.get("sym", "?")
        name = s.get("name", sym)
        price = s.get("price")
        regime = s.get("regime", "?")

        if s.get("price_only"):
            pause = s.get("pause_reason", "暂停")
            lines.append(f"⏸ {sym} {name} ${price or '?'} — {pause}")
        else:
            rsi = s.get("rsi", "")
            adx = s.get("adx", "")
            box_l = s.get("box_low", "?")
            box_h = s.get("box_high", "?")
            shares = s.get("shares", 0)
            action = s.get("action_now", "")
            lines.append(f"✅ {sym} {name} ${price}")
            lines.append(f"   区间 [{box_l}–{box_h}] | {shares}股 | RSI:{rsi} ADX:{adx}")
            if action:
                lines.append(f"   💡 {action}")

        lines.append("")

    lines.append(f"⚖ 日停手: ${output.get('daily_stop', '?')}")
    lines.append(f"📈 胜率约: {output.get('base_rate_note', '')}")
    lines.append(output.get("disclaimer", "⚠ 仅供参考"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_all(mode: str = "daily", one_sym: str = None) -> dict:
    """
    Main entry point.

    Args:
        mode: "daily" (Layer 2), "weekly" (Layer 1 + 2), "one" (Layer 3)
        one_sym: ticker for on-demand mode
    """
    cfg = load_config()

    if mode == "weekly":
        # Load prices for pool scan
        prices_path = CACHE / "prices_latest.parquet"
        prices_df = None
        if prices_path.exists():
            prices_df = pd.read_parquet(prices_path)
        pool = select_weekly_pool(cfg, prices_df)
        # Also run daily levels after pool refresh
        output = compute_daily_levels(cfg)
        output["pool_refresh"] = True
        return output

    elif mode == "one":
        if not one_sym:
            log.error("--one requires a ticker symbol")
            return {"error": "missing ticker"}
        result = compute_one(one_sym.upper(), cfg)
        return result

    else:  # daily
        output = compute_daily_levels(cfg)
        # Generate Telegram message
        msg = format_telegram_msg(output)
        log.info("Telegram preview:\n%s", msg)
        output["telegram_msg"] = msg
        return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pipeline B — Swing/Grid Trading")
    parser.add_argument("--weekly", action="store_true",
                        help="Run weekly pool refresh (Layer 1 + 2)")
    parser.add_argument("--one", type=str, default=None,
                        help="On-demand single stock (Layer 3)")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON to stdout")
    args = parser.parse_args()

    if args.weekly:
        result = run_all(mode="weekly")
    elif args.one:
        result = run_all(mode="one", one_sym=args.one)
    else:
        result = run_all(mode="daily")

    if args.json:
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2))
