#!/usr/bin/env python3
"""
Pipeline C — Smart Money Tracker
==================================
Daily pipeline that aggregates institutional moves, insider buying,
ARK holdings, Reddit buzz, and (optionally) X/Twitter narratives.

Runs at 09:00 MYT Tue–Sat (after US session close).

Dependencies: scripts/utils.py (ROOT, CACHE, DATA, load_json, save_json,
             load_config, setup_logger, get_env, retry, update_index)
Input:       tracked_entities.json, cache/
Output:      smart/{date}.json, smart/index.json, Telegram notification

Trust levels:
  🟢 official  — SEC filings, ARK CSVs
  🟡 crowd     — Reddit cashtags
  🔴 unverified — X/Twitter, self-reported claims

NEVER fabricate.  Missing data → skip + log warning.
1 LLM call max.  SEC ≤10 req/s.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils import (  # noqa: E402
    ROOT as _ROOT,
    CACHE,
    DATA,
    load_json,
    save_json,
    load_config,
    setup_logger,
    get_env,
    retry,
    update_index,
    call_llm,
)

# Ensure CACHE and SMART dirs exist
SMART = DATA / "smart"
SMART.mkdir(parents=True, exist_ok=True)

log = setup_logger("pipeline_c_smart")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEC_HEADERS = {"User-Agent": "Hermes/1.0 henrylee@snsoft.my"}
SEC_RATE_DELAY = 0.12  # ≤10 req/s
OPENFIGI_BASE = "https://api.openfigi.com/v3/mapping"
ARK_BASE = "https://ARK Invest ETF - Holdings"
ARK_FUNDS_URL = "https://ark-funds-us.s3.amazonaws.com/public/funds/{fund}/holdings.csv"
ARK_CSV_URLS = {
    "ARKK": "https://arkfunds.us/api/etf/fund/holdings/ARKK",
    "ARKW": "https://arkfunds.us/api/etf/fund/holdings/ARKW",
    "ARKG": "https://arkfunds.us/api/etf/fund/holdings/ARKG",
    "ARKQ": "https://arkfunds.us/api/etf/fund/holdings/ARKQ",
    "ARKF": "https://arkfunds.us/api/etf/fund/holdings/ARKF",
}
ARK_ALT_URLS = {
    "ARKK": "https://www.ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
    "ARKW": "https://www.ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
    "ARKG": "https://www.ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_GENOMIC_REVOLUTION_MULTISECTOR_ETF_ARKG_HOLDINGS.csv",
    "ARKQ": "https://www.ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_AUTONOMOUS_TECHNOLOGY_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
    "ARKF": "https://www.ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv",
}
REDDIT_UA = "Hermes/1.0 (by /u/hermes_bot)"
OPENINSIDER_URL = (
    "http://openinsider.com/screener?"
    "fd=7&xp=1&o=insider_purchases_per_insider_14d"
)
X_BASE = "https://api.x.com/2"

# Reddit stop words (single letters + common English + finance noise)
STOPWORDS = {
    "THE", "A", "AN", "I", "IT", "IS", "IN", "AT", "OF", "TO", "ON", "BY",
    "MY", "ME", "HE", "BE", "WE", "SO", "AS", "OR", "DO", "AM", "UP",
    "IF", "NO", "US", "GO", "OW", "OH", "OK", "YO", "DO", "IF", "AN",
    "AND", "ARE", "BUT", "NOT", "FOR", "ALL", "CAN", "HAS", "HAD", "HIS",
    "HER", "HOW", "ITS", "MAY", "NEW", "NOW", "OLD", "OUR", "OUT", "OWN",
    "PUT", "RAN", "SAY", "SET", "SHE", "TOO", "USE", "WAS", "WHO", "WHY",
    "YOU", "ANY", "FAR", "LOT", "WAY", "BIG", "OLD", "TOP", "LOW", "HOT",
    "DID", "GET", "GOT", "LET", "SAY", "RUN", "SAW", "TEN", "MEN", "PEN",
    "CEO", "IPO", "ATH", "ETF", "DD", "YOLO", "HODL", "MOON", "FUD",
    "IMO", "TBH", "TB", "DD", "FOMO", "BRO", "IMO", "NFA", "SEC",
    "GDP", "CPI", "FED", "P/E", "EPS", "BTC", "SPX", "VIX", "PPI",
}

# Michael Burry warning — he deregistered Scion 2025-11, sells bearish newsletter
BURRY_WARN = (
    "⚠️ Burry 自2025-11注销Scion后出售看空Newsletter，"
    "切勿跟随其空头头寸。"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_str() -> str:
    """Return current datetime as 'YYYY-MM-DD HH:MM'."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _today_str() -> str:
    """Return current date as 'YYYY-MM-DD'."""
    return datetime.now().strftime("%Y-%m-%d")


def _this_week_str() -> str:
    """Return ISO week as 'YYYY-WNN'."""
    today = datetime.now()
    return f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"


def _sec_get(url: str, session: Any = None) -> str | None:
    """GET with SEC rate-limit headers. Returns text or None."""
    import requests  # noqa: E402 — lazy to keep startup fast
    try:
        resp = requests.get(url, headers=SEC_HEADERS, timeout=30)
        resp.raise_for_status()
        time.sleep(SEC_RATE_DELAY)
        return resp.text
    except Exception as e:
        log.warning("SEC GET failed: %s — %s", url, e)
        return None


def _sec_get_json(url: str) -> dict | list | None:
    """GET JSON from SEC with rate-limit headers."""
    text = _sec_get(url)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("JSON decode failed for %s: %s", url, e)
        return None


def _figi_batch(cusips: list[str]) -> dict[str, str]:
    """Resolve CUSIPs → tickers via OpenFIGI (batch ≤10).
    Returns {cusip: ticker} for resolved ones.
    """
    import requests  # noqa: E402
    if not cusips:
        return {}

    figi_key = os.getenv("OPENFIGI_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if figi_key:
        headers["X-OPENFIGI-APIKEY"] = figi_key

    results: dict[str, str] = {}
    batch_size = 100 if figi_key else 10

    for i in range(0, len(cusips), batch_size):
        batch = cusips[i : i + batch_size]
        payload = [
            {"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"}
            for c in batch
        ]
        try:
            resp = requests.post(
                OPENFIGI_BASE,
                json=payload,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            for entry, cusip in zip(data, batch):
                if isinstance(entry, dict) and "data" in entry:
                    # Prefer common-stock US match
                    best = None
                    for hit in entry["data"]:
                        if hit.get("securityType") == "Common Stock":
                            best = hit
                            break
                    if best is None and entry["data"]:
                        best = entry["data"][0]
                    if best:
                        results[cusip] = best.get("ticker", "")
        except Exception as e:
            log.warning("OpenFIGI batch failed: %s", e)
        if i + batch_size < len(cusips):
            time.sleep(0.5)

    return results


def _load_json_file(path: Path) -> dict | list:
    """Load JSON, return {} on missing/error."""
    if not path.exists():
        return {}
    try:
        return load_json(path)
    except Exception as e:
        log.warning("Failed to load %s: %s", path, e)
        return {}


def _save_cache(path: Path, data: Any) -> None:
    """Save to cache with parent-dir creation."""
    save_json(path, data)


def _format_usd(value: float | int) -> str:
    """Format USD amount to Chinese-friendly string: 240万, 1.2亿."""
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    elif value >= 10_000:
        return f"{value / 10_000:.0f}万"
    else:
        return f"${value:,.0f}"


# ═══════════════════════════════════════════════════════════════════════════
# 1. SEC EDGAR 13F HOLDINGS
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_cik(name: str, entities: dict) -> str | None:
    """Resolve fund name → CIK.  Check entities cache first, then EDGAR."""
    # Check tracked_entities.json for cached CIK
    for fund in entities.get("funds_13f", []):
        if fund["name"] == name and fund.get("cik"):
            return fund["cik"]

    # Search EDGAR — use the ATOM search feed
    search_url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?"
        f"action=getcompany&company={name.replace(' ', '+')}"
        "&type=13F-HR&output=atom&count=5"
    )
    text = _sec_get(search_url)
    if text is None:
        return None

    # Parse CIK from Atom XML: <id>https://www.sec.gov/CIK0001067983</id>
    match = re.search(r"CIK(\d{10})", text)
    if match:
        cik = match.group(1)
        # Update entities cache
        for fund in entities.get("funds_13f", []):
            if fund["name"] == name:
                fund["cik"] = cik
        log.info("  Resolved CIK for %s: %s", name, cik)
        return cik

    log.warning("  Could not resolve CIK for %s", name)
    return None


def _fetch_13f_filings(cik: str) -> list[dict]:
    """Fetch latest 2 13F-HR filing URLs for a CIK.
    Returns list of {accession, date, url} dicts.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _sec_get_json(url)
    if data is None:
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    filings = []
    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            filings.append({
                "accession": accessions[i].replace("-", ""),
                "date": dates[i],
                "doc": primary_docs[i],
                "form": form,
            })
            if len(filings) >= 2:
                break

    return filings


def _parse_13f_holdings(cik: str, accession: str) -> dict[str, dict]:
    """Fetch and parse 13F information table.
    Returns {cusip: {name, cusip, value, shares}}.
    """
    # Build the information table URL
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}"
        f"/{accession}/"
    )
    # We need to find the info table document — it usually has 'infotable' or
    # 'primary_doc' but the naming varies. Try the submissions endpoint for docs.
    sub_url = (
        f"https://data.sec.gov/submissions/CIK{cik}.json"
    )
    data = _sec_get_json(sub_url)
    if data is None:
        return {}

    # The filing documents are in the filing index page — fetch it
    idx_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}"
        f"/{accession}/index.json"
    )
    idx_data = _sec_get_json(idx_url)
    if idx_data is None:
        return {}

    # Find the information table XML
    items = idx_data.get("directory", {}).get("item", [])
    info_doc = None
    for item in items:
        name = item.get("name", "")
        if "infotable" in name.lower() or "info_table" in name.lower():
            info_doc = name
            break
        if name.endswith(".xml") and not info_doc:
            info_doc = name

    if not info_doc:
        # Fallback: look for any XML that might be the info table
        for item in items:
            if item.get("name", "").endswith(".xml"):
                info_doc = item["name"]
                break

    if not info_doc:
        log.warning("  No info table found for CIK %s accession %s", cik, accession)
        return {}

    xml_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}"
        f"/{accession}/{info_doc}"
    )
    xml_text = _sec_get(xml_url)
    if xml_text is None:
        return {}

    return _parse_13f_xml(xml_text)


def _parse_13f_xml(xml_text: str) -> dict[str, dict]:
    """Parse 13F information table XML → {cusip: {name, cusip, value, shares}}.
    Simple regex parsing to avoid XML library dependency.
    """
    holdings: dict[str, dict] = {}

    # Split by <ns1:infoTable> or <infoTable> tags
    rows = re.split(r"<[^>]*:infoTable[^>]*>|<infoTable[^>]*>", xml_text)
    for row in rows:
        if "</" not in row:
            continue

        name_m = re.search(
            r"<[^>]*nameOfIssuer[^>]*>([^<]+)<", row
        )
        cusip_m = re.search(
            r"<[^>]*cusip[^>]*>([A-Z0-9]{9})<", row
        )
        value_m = re.search(
            r"<[^>]*value[^>]*>(\d+)<", row
        )
        shares_m = re.search(
            r"<[^>]*sshPrnamt[^>]*>(\d+)<", row
        )
        share_type_m = re.search(
            r"<[^>]*sshPrnamtType[^>]*>([^<]+)<", row
        )

        if not cusip_m:
            continue

        cusip = cusip_m.group(1)
        holdings[cusip] = {
            "name": name_m.group(1).strip() if name_m else "",
            "cusip": cusip,
            "value": int(value_m.group(1)) * 1000 if value_m else 0,  # x1000
            "shares": int(shares_m.group(1)) if shares_m else 0,
        }

    return holdings


def _diff_13f(
    current: dict[str, dict],
    prior: dict[str, dict],
) -> list[dict]:
    """Diff two quarters' holdings.  Returns list of {ticker, name, action, usd, ...}."""
    results = []

    for cusip, cur in current.items():
        prev = prior.get(cusip)
        ticker = cur.get("_ticker", cusip)  # placeholder
        entry = {
            "cusip": cusip,
            "ticker": ticker,
            "name": cur["name"],
            "shares": cur["shares"],
            "usd": _format_usd(cur["value"]),
            "trust": "official",
        }

        if prev is None:
            entry["action"] = "NEW"
        else:
            prev_val = prev.get("value", 0) or 1
            change = (cur["value"] - prev_val) / prev_val
            if change > 0.10:
                entry["action"] = "ADD"
            elif change < -0.10:
                entry["action"] = "TRIM"
            else:
                continue  # No significant change

        results.append(entry)

    # Check for EXITs (in prior but not in current)
    for cusip, prev in prior.items():
        if cusip not in current:
            results.append({
                "cusip": cusip,
                "ticker": prev.get("_ticker", cusip),
                "name": prev["name"],
                "shares": 0,
                "usd": _format_usd(0),
                "action": "EXIT",
                "trust": "official",
            })

    return results


def fetch_13f(entities: dict, cusip_cache: dict) -> list[dict]:
    """Fetch and diff 13F holdings for all tracked funds.
    Returns list of fund_move dicts.
    """
    log.info("─" * 40)
    log.info("STEP 1: SEC EDGAR 13F Holdings")
    log.info("─" * 40)

    all_moves: list[dict] = []
    all_new_cusips: list[str] = []

    for fund_info in entities.get("funds_13f", []):
        name = fund_info["name"]
        log.info("  Processing: %s", name)

        cik = _resolve_cik(name, entities)
        if not cik:
            log.warning("    Skip %s — no CIK", name)
            continue

        filings = _fetch_13f_filings(cik)
        if len(filings) < 1:
            log.warning("    No 13F filings found for %s (CIK %s)", name, cik)
            continue

        # Parse current quarter
        current = _parse_13f_holdings(cik, filings[0]["accession"])
        log.info("    Current quarter: %d positions (filed %s)", len(current), filings[0]["date"])

        # Parse prior quarter if available
        prior: dict[str, dict] = {}
        if len(filings) >= 2:
            prior_cache = CACHE / f"13f_{cik}_{filings[1]['accession']}.json"
            if prior_cache.exists():
                prior = _load_json_file(prior_cache)
                log.info("    Prior quarter: %d positions (from cache)", len(prior))
            else:
                prior = _parse_13f_holdings(cik, filings[1]["accession"])
                if prior:
                    _save_cache(prior_cache, prior)
                log.info("    Prior quarter: %d positions (fetched)", len(prior))

        # Collect CUSIPs needing ticker resolution
        all_cusips = set(current.keys()) | set(prior.keys())
        missing = [c for c in all_cusips if c not in cusip_cache]
        all_new_cusips.extend(missing)

        # Resolve tickers from cache
        for cusip in all_cusips:
            if cusip in cusip_cache:
                current.setdefault(cusip, {})["_ticker"] = cusip_cache[cusip]
                if cusip in prior:
                    prior[cusip]["_ticker"] = cusip_cache[cusip]

        # Diff
        moves = _diff_13f(current, prior)
        for m in moves:
            m["fund"] = name
        all_moves.extend(moves)

        # Cache current quarter for next run
        cur_cache = CACHE / f"13f_{cik}_{filings[0]['accession']}.json"
        _save_cache(cur_cache, current)

    # Batch-resolve CUSIPs via OpenFIGI
    if all_new_cusips:
        log.info("  Resolving %d new CUSIPs via OpenFIGI...", len(all_new_cusips))
        new_tickers = _figi_batch(all_new_cusips)
        cusip_cache.update(new_tickers)
        log.info("  Resolved %d / %d", len(new_tickers), len(all_new_cusips))

    # Apply resolved tickers to moves
    for m in all_moves:
        if m["cusip"] in cusip_cache:
            m["ticker"] = cusip_cache[m["cusip"]]

    log.info("  Total 13F moves: %d", len(all_moves))
    return all_moves


# ═══════════════════════════════════════════════════════════════════════════
# 2. FORM 4 INSIDER BUYS
# ═══════════════════════════════════════════════════════════════════════════

def _load_company_ciks() -> dict[str, str]:
    """Load company tickers → CIK mapping from SEC."""
    cache_path = CACHE / "company_cikers.json"
    if cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < 24 * 7:  # Refresh weekly
            data = _load_json_file(cache_path)
            if data:
                return data

    url = "https://www.sec.gov/files/company_tickers.json"
    data = _sec_get_json(url)
    if data is None:
        return {}

    # data is {0: {cik_str, ticker, title}, ...}
    result = {}
    for _k, v in data.items():
        ticker = v.get("ticker", "")
        cik = str(v.get("cik_str", "")).zfill(10)
        if ticker and cik:
            result[ticker] = cik

    _save_cache(cache_path, result)
    return result


def fetch_form4_insiders(entities: dict) -> list[dict]:
    """Fetch recent Form 4 insider purchases.
    Returns list of insider_buys dicts.
    """
    log.info("─" * 40)
    log.info("STEP 2: Form 4 Insider Buys")
    log.info("─" * 40)

    insider_buys: list[dict] = []

    # Use openinsider.com as primary source — easier to parse
    import requests  # noqa: E402
    try:
        resp = requests.get(
            OPENINSIDER_URL,
            headers=SEC_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        log.warning("  openinsider.com fetch failed: %s", e)
        # Fallback: try SEC EDGAR directly for our tracked entities
        return _fetch_form4_edgar(entities)

    # Parse the HTML table — look for insider purchases
    from html.parser import HTMLParser  # noqa: E402

    class InsiderParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_table = False
            self.in_row = False
            self.in_cell = False
            self.current_row: list[str] = []
            self.rows: list[list[str]] = []

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self.in_table = True
            elif tag == "tr" and self.in_table:
                self.in_row = True
                self.current_row = []
            elif tag in ("td", "th") and self.in_row:
                self.in_cell = True

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self.in_cell:
                self.in_cell = False
            elif tag == "tr" and self.in_row:
                self.in_row = False
                if self.current_row:
                    self.rows.append(self.current_row)
            elif tag == "table":
                self.in_table = False

        def handle_data(self, data):
            if self.in_cell:
                self.current_row.append(data.strip())

    parser = InsiderParser()
    parser.feed(resp.text)

    # Parse rows — openinsider has specific columns
    # Typical: ticker, company, insider, title, trade_type, price, shares, value, owned, Δown, date
    purchases_by_company: dict[str, list[dict]] = defaultdict(list)

    for row in parser.rows:
        if len(row) < 10:
            continue
        try:
            ticker = row[0].strip()
            company = row[1].strip()
            insider = row[2].strip()
            title = row[3].strip()
            trade_type = row[4].strip()
            price_str = row[5].replace("$", "").replace(",", "").strip()
            shares_str = row[6].replace(",", "").strip()
            value_str = row[7].replace("$", "").replace(",", "").strip()
            date_str = row[10].strip() if len(row) > 10 else ""

            # Filter: purchase only ("P" or "Purchase")
            if trade_type.upper() not in ("P", "PURCHASE", "S-BUY", "M-SHARES"):
                continue

            if not ticker:
                continue

            price = float(price_str) if price_str else 0
            shares = int(shares_str) if shares_str else 0
            value = int(value_str) if value_str else 0

            purchases_by_company[ticker.upper()].append({
                "ticker": ticker.upper(),
                "name": company,
                "insider": insider,
                "title": title,
                "shares": shares,
                "usd": value,
                "price": price,
                "date": date_str,
            })
        except (ValueError, IndexError) as e:
            continue  # Skip malformed rows

    # Cluster detection: ≥2 insiders in 14 days
    today = datetime.now()
    for ticker, buys in purchases_by_company.items():
        recent = []
        for b in buys:
            try:
                if b["date"]:
                    dt = datetime.strptime(b["date"], "%m/%d/%Y")
                    if (today - dt).days <= 14:
                        recent.append(b)
            except ValueError:
                recent.append(b)  # Include if date unparseable

        if len(recent) >= 2:
            total_usd = sum(b["usd"] for b in recent)
            insiders = list({b["insider"] for b in recent})
            insider_buys.append({
                "ticker": ticker,
                "name": recent[0]["name"],
                "insiders": len(insiders),
                "usd": _format_usd(total_usd),
                "cluster": True,
                "trust": "official",
                "details": [
                    f"{b['insider']}({b['title']}): {b['shares']}股"
                    for b in recent
                ],
            })
        elif len(recent) == 1:
            b = recent[0]
            insider_buys.append({
                "ticker": ticker,
                "name": b["name"],
                "insiders": 1,
                "usd": _format_usd(b["usd"]),
                "cluster": False,
                "trust": "official",
                "details": [f"{b['insider']}({b['title']}): {b['shares']}股"],
            })

    log.info("  Insider purchases found: %d companies", len(insider_buys))
    return insider_buys


def _fetch_form4_edgar(entities: dict) -> list[dict]:
    """Fallback: fetch Form 4 from EDGAR for tracked company CIKs."""
    log.info("  Falling back to EDGAR for Form 4...")
    company_ciks = _load_company_ciks()
    results: list[dict] = []

    # Only check a subset of tickers to stay within rate limits
    checked = 0
    for ticker, cik in list(company_ciks.items())[:50]:
        url = (
            f"https://data.sec.gov/submissions/CIK{cik}.json"
        )
        data = _sec_get_json(url)
        if data is None:
            continue

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])

        # Check last 2 days
        cutoff = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        for i, form in enumerate(forms):
            if form == "4" and dates[i] >= cutoff:
                # Fetch and parse this Form 4
                acc = accessions[i].replace("-", "")
                doc_url = (
                    f"https://www.sec.gov/Archives/edgar/data/{cik}"
                    f"/{acc}/{primary_docs[i]}"
                )
                xml = _sec_get(doc_url)
                if xml:
                    purchase = _parse_form4_xml(xml, ticker)
                    if purchase:
                        results.append(purchase)
                break  # Only latest per company

        checked += 1
        if checked >= 20:
            break  # Rate limit

    return results


def _parse_form4_xml(xml_text: str, ticker: str) -> dict | None:
    """Parse Form 4 XML for purchase transactions."""
    # Check if transaction code is "P" (purchase)
    if not re.search(r"<transactionCode>P</transactionCode>", xml_text):
        return None

    shares_m = re.search(r"<sshPrnamt[^>]*>(\d+)<", xml_text)
    name_m = re.search(r"<rptOwnerName>([^<]+)</rptOwnerName>", xml_text)

    if shares_m:
        return {
            "ticker": ticker,
            "name": ticker,
            "insiders": 1,
            "usd": "未知",
            "cluster": False,
            "trust": "official",
            "details": [
                f"{name_m.group(1)}: {shares_m.group(1)}股"
                if name_m else f"Insider: {shares_m.group(1)}股"
            ],
        }
    return None


# ═══════════════════════════════════════════════════════════════════════════
# 3. ARK HOLDINGS
# ═══════════════════════════════════════════════════════════════════════════

def fetch_ark(entities: dict) -> tuple[list[dict], bool]:
    """Fetch ARK fund holdings and diff vs cache.
    Returns (ark_moves, ark_ok).
    """
    log.info("─" * 40)
    log.info("STEP 3: ARK Holdings")
    log.info("─" * 40)

    import requests  # noqa: E402
    all_moves: list[dict] = []
    ark_ok = True

    for fund in entities.get("ark_funds", []):
        log.info("  Fetching ARK %s...", fund)

        csv_data = None
        # Try primary URL
        for url_template in [ARK_CSV_URLS.get(fund), ARK_ALT_URLS.get(fund)]:
            if not url_template:
                continue
            try:
                resp = requests.get(
                    url_template,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=30,
                )
                if resp.status_code == 200 and resp.text.strip():
                    csv_data = resp.text
                    break
            except Exception:
                continue

        if csv_data is None:
            log.warning("    ARK %s: no CSV data (404 or empty)", fund)
            ark_ok = False
            continue

        # Parse CSV
        current_holdings: dict[str, dict] = {}
        try:
            reader = csv.DictReader(io.StringIO(csv_data))
            for row in reader:
                # ARK CSV has 'ticker', 'name', 'shares', 'weight' etc.
                # Normalize column names (ARK changed format multiple times)
                ticker = (
                    row.get("ticker", "")
                    or row.get("Ticker", "")
                    or row.get("TICKER", "")
                ).strip()
                shares_str = (
                    row.get("shares", "")
                    or row.get("Shares", "")
                    or row.get("Shares/Position", "")
                ).strip().replace(",", "")

                if not ticker:
                    continue

                try:
                    shares = int(shares_str) if shares_str else 0
                except ValueError:
                    shares = 0

                current_holdings[ticker] = {
                    "shares": shares,
                    "name": (
                        row.get("name", "")
                        or row.get("Name", "")
                        or row.get("Company Name", "")
                    ).strip(),
                }
        except Exception as e:
            log.warning("    CSV parse error for ARK %s: %s", fund, e)
            continue

        log.info("    ARK %s: %d positions", fund, len(current_holdings))

        # Load previous cache
        prev_path = CACHE / f"ark_{fund}_prev.csv"
        prev_holdings: dict[str, dict] = {}
        if prev_path.exists():
            try:
                with open(prev_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        ticker = (
                            row.get("ticker", "")
                            or row.get("Ticker", "")
                            or row.get("TICKER", "")
                        ).strip()
                        shares_str = (
                            row.get("shares", "")
                            or row.get("Shares", "")
                            or row.get("Shares/Position", "")
                        ).strip().replace(",", "")
                        if ticker:
                            try:
                                prev_holdings[ticker] = {
                                    "shares": int(shares_str) if shares_str else 0,
                                }
                            except ValueError:
                                pass
            except Exception:
                pass

        # Diff: find new buys and sells
        for ticker, cur in current_holdings.items():
            prev = prev_holdings.get(ticker)
            if prev is None:
                action = "买入"
            elif cur["shares"] > prev["shares"] * 1.05:
                action = "加仓"
            elif cur["shares"] < prev["shares"] * 0.95:
                action = "减仓"
            else:
                continue  # No significant change

            all_moves.append({
                "ticker": ticker,
                "name": cur.get("name", ticker),
                "action": action,
                "fund": f"ARK {fund}",
                "trust": "official",
            })

        # Check for exits
        for ticker in prev_holdings:
            if ticker not in current_holdings:
                all_moves.append({
                    "ticker": ticker,
                    "name": ticker,
                    "action": "卖出",
                    "fund": f"ARK {fund}",
                    "trust": "official",
                })

        # Save current as prev for next run
        try:
            with open(prev_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["ticker", "shares", "name"])
                writer.writeheader()
                for t, h in current_holdings.items():
                    writer.writerow({
                        "ticker": t,
                        "shares": h["shares"],
                        "name": h.get("name", ""),
                    })
        except Exception as e:
            log.warning("    Failed to save ARK cache: %s", e)

    log.info("  Total ARK moves: %d", len(all_moves))
    return all_moves, ark_ok


# ═══════════════════════════════════════════════════════════════════════════
# 4. REDDIT CASHTAG COUNTER
# ═══════════════════════════════════════════════════════════════════════════

def fetch_reddit(entities: dict) -> tuple[list[dict], bool]:
    """Count $cashtags and ALL-CAPS tickers from Reddit hot posts.
    Returns (crowd_items, reddit_ok).
    """
    log.info("─" * 40)
    log.info("STEP 4: Reddit Cashtags")
    log.info("─" * 40)

    import requests  # noqa: E402
    ticker_counts: Counter = Counter()
    reddit_ok = True

    for sub in entities.get("reddit_subs", []):
        log.info("  Scanning r/%s...", sub)
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit=100"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": REDDIT_UA},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("  r/%s fetch failed: %s", sub, e)
            reddit_ok = False
            continue

        posts = data.get("data", {}).get("children", [])
        for post in posts:
            text = (
                post.get("data", {}).get("title", "")
                + " "
                + post.get("data", {}).get("selftext", "")
            )
            # Find $CASHTAG patterns
            cashtags = re.findall(r"\$([A-Z]{1,5})\b", text)
            for tag in cashtags:
                if tag not in STOPWORDS:
                    ticker_counts[tag] += 1

            # Find standalone ALL-CAPS words that look like tickers (2-5 chars)
            all_caps = re.findall(r"\b([A-Z]{2,5})\b", text)
            for word in all_caps:
                if (
                    word not in STOPWORDS
                    and not re.search(r"\d", word)
                    and word not in ticker_counts
                ):
                    # Only add if it's plausible as a ticker
                    ticker_counts[word] += 1

    # Top 10
    top10 = ticker_counts.most_common(10)
    crowd = []
    for ticker, count in top10:
        stance = "看涨" if count > 20 else "关注"
        crowd.append({
            "ticker": ticker,
            "mentions": count,
            "stance": stance,
            "trust": "crowd",
        })

    log.info("  Top Reddit tickers: %s", [c["ticker"] for c in crowd])
    return crowd, reddit_ok


# ═══════════════════════════════════════════════════════════════════════════
# 5. X / TWITTER (optional)
# ═══════════════════════════════════════════════════════════════════════════

def fetch_x_tweets(entities: dict) -> tuple[list[dict], bool, list[dict]]:
    """Fetch recent tweets from tracked X accounts.
    Returns (tweets_raw, x_ok, raw_for_llm).

    Requires X_API_KEY env var.  If absent, returns ([], False, []).
    """
    log.info("─" * 40)
    log.info("STEP 5: X / Twitter")
    log.info("─" * 40)

    try:
        x_key = get_env("X_API_KEY", required=False)
    except Exception:
        x_key = None

    if not x_key:
        log.info("  X_API_KEY not set — skipping X/Twitter")
        return [], False, []

    import requests  # noqa: E402

    tweets_raw: list[dict] = []
    raw_for_llm: list[dict] = []

    for handle in entities.get("x_accounts", []):
        log.info("  Fetching @%s...", handle)
        headers = {"Authorization": f"Bearer {x_key}"}

        # Resolve user ID
        try:
            user_resp = requests.get(
                f"{X_BASE}/users/by/username/{handle}",
                headers=headers,
                timeout=15,
            )
            user_resp.raise_for_status()
            user_data = user_resp.json()
            user_id = user_data["data"]["id"]
        except Exception as e:
            log.warning("  @%s: user resolve failed: %s", handle, e)
            continue

        # Get tweets
        try:
            tweets_resp = requests.get(
                f"{X_BASE}/users/{user_id}/tweets",
                headers=headers,
                params={
                    "max_results": 20,
                    "exclude": "retweets,replies",
                    "tweet.fields": "created_at,text",
                },
                timeout=15,
            )
            tweets_resp.raise_for_status()
            tweets_data = tweets_resp.json()
        except Exception as e:
            log.warning("  @%s: tweets fetch failed: %s", handle, e)
            continue

        tweets = tweets_data.get("data", [])
        tweet_texts = []
        for t in tweets:
            text = t.get("text", "")
            tweet_texts.append(text)
            # Extract tickers from tweets
            tickers = list(set(re.findall(r"\$([A-Z]{1,5})\b", text)))

        raw_for_llm.append({
            "handle": handle,
            "tweets": tweet_texts[:5],  # Last 5 tweets for LLM
            "tickers": tickers,
        })

    return tweets_raw, True, raw_for_llm


def summarize_x_with_llm(raw_for_llm: list[dict]) -> list[dict]:
    """Summarize X data with 1 LLM call.  Returns influencers list.
    All items tagged unverified + note about self-reported claims.
    """
    if not raw_for_llm:
        return []

    # Build prompt for LLM
    prompt_parts = ["请分析以下 X/Twitter 账号最近的推文，输出 JSON 数组："]
    prompt_parts.append(
        '每项格式: {"handle", "name", "summary"(中文≤30字), '
        '"tickers"[], "stance"(看涨/看跌/中性)}'
    )
    prompt_parts.append("")

    for acct in raw_for_llm:
        prompt_parts.append(f"=== @{acct['handle']} ===")
        for i, tweet in enumerate(acct["tweets"]):
            prompt_parts.append(f"  Tweet {i+1}: {tweet[:200]}")
        prompt_parts.append("")

    prompt_parts.append("输出纯 JSON，不要 markdown。")
    prompt = "\n".join(prompt_parts)

    # Call LLM — mimo via Anthropic-compatible API
    try:
        content = call_llm(prompt, temperature=0.2)
        if not content:
            log.warning("  No LLM API key found — skipping X narrative")
            return []

        # Strip markdown code fences if present
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)

        results = json.loads(content)
    except Exception as e:
        log.warning("  LLM call failed: %s — returning raw data", e)
        # Fallback: return raw summaries without LLM
        results = []
        for acct in raw_for_llm:
            all_tickers = list(set(
                t for t in acct.get("tickers", [])
                if t not in STOPWORDS
            ))
            results.append({
                "handle": acct["handle"],
                "name": acct["handle"],
                "summary": "需要人工查看",
                "tickers": all_tickers,
                "stance": "中性",
            })

    # Add trust and notes
    influencers = []
    for r in results:
        entry = {
            "handle": r.get("handle", ""),
            "name": r.get("name", r.get("handle", "")),
            "summary": r.get("summary", ""),
            "tickers": r.get("tickers", []),
            "stance": r.get("stance", "中性"),
            "trust": "unverified",
            "note": "自报战绩,无法核实",
        }

        # Special notes for known accounts
        if entry["handle"].lower() == "michaeljburry":
            entry["note"] = BURRY_WARN
        elif entry["handle"].lower() == "aleabitoreddit":
            entry["name"] = "Serenity"
            entry["note"] = "@aleabitoreddit = Serenity, 自报战绩,无法核实"
        elif entry["handle"].lower() == "lizthomasstrat":
            entry["note"] = "@LizThomasStrat (非@LizYoungStrat)"

        influencers.append(entry)

    return influencers


# ═══════════════════════════════════════════════════════════════════════════
# 6. CROSS-REFERENCE WITH WATCHLIST
# ═══════════════════════════════════════════════════════════════════════════

def crossref_with_value(
    fund_moves: list[dict],
    insider_buys: list[dict],
    ark_moves: list[dict],
    crowd: list[dict],
    influencers: list[dict],
) -> list[dict]:
    """Intersect all signals with Henry's zone+watch tickers from value/{week}.json.
    Returns list of crossref items with plain headlines.
    """
    log.info("─" * 40)
    log.info("STEP 6: Cross-reference with Watchlist")
    log.info("─" * 40)

    week_str = _this_week_str()
    value_path = DATA / "value" / f"{week_str}.json"

    if not value_path.exists():
        # Try previous week
        prev_week = datetime.now() - timedelta(days=7)
        prev_week_str = f"{prev_week.isocalendar()[0]}-W{prev_week.isocalendar()[1]:02d}"
        value_path = DATA / "value" / f"{prev_week_str}.json"

    if not value_path.exists():
        log.warning("  No value/week file found — skipping crossref")
        return []

    try:
        value_data = load_json(value_path)
    except Exception as e:
        log.warning("  Failed to load value data: %s", e)
        return []

    # If value_data is a list (e.g. empty []), no tickers to crossref
    if isinstance(value_data, list):
        if not value_data:
            log.info("  Value data is empty list — no crossref possible")
            return []
        # If list of strings, treat as ticker list
        watch_tickers = {t.upper() for t in value_data if isinstance(t, str)}
        if not watch_tickers:
            return []
    elif isinstance(value_data, dict):
        # Collect zone + watch tickers
        watch_tickers: set[str] = set()
        for section in ["zone", "watch"]:
            for item in value_data.get(section, []):
                if isinstance(item, dict):
                    t = item.get("ticker", "")
                    if t:
                        watch_tickers.add(t.upper())
                elif isinstance(item, str):
                    watch_tickers.add(item.upper())
    else:
        log.info("  Unexpected value data type: %s", type(value_data).__name__)
        return []

    if not watch_tickers:
        log.info("  No zone/watch tickers found in value data")
        return []

    log.info("  Watchlist tickers: %d — %s", len(watch_tickers), sorted(watch_tickers))

    # Build signal map: ticker → list of signal descriptions
    signal_map: dict[str, list[str]] = defaultdict(list)

    for m in fund_moves:
        t = m.get("ticker", "").upper()
        if t in watch_tickers:
            signal_map[t].append(
                f"{m['fund']} {m['action']} {m.get('name', t)} {m.get('usd', '')}"
            )

    for m in insider_buys:
        t = m.get("ticker", "").upper()
        if t in watch_tickers:
            cluster_tag = " 🔥集体买入" if m.get("cluster") else ""
            signal_map[t].append(
                f"{m['insiders']}位高管买入{cluster_tag} ${m['usd']}"
            )

    for m in ark_moves:
        t = m.get("ticker", "").upper()
        if t in watch_tickers:
            signal_map[t].append(
                f"{m['fund']} {m['action']} {m.get('name', t)}"
            )

    for c in crowd:
        t = c.get("ticker", "").upper()
        if t in watch_tickers:
            signal_map[t].append(
                f"Reddit {c['mentions']}次提及({c['stance']})"
            )

    for inf in influencers:
        for t in inf.get("tickers", []):
            if t.upper() in watch_tickers:
                signal_map[t.upper()].append(
                    f"@{inf['handle']}: {inf['summary']}"
                )

    # Build crossref output
    crossref = []
    for ticker in sorted(signal_map.keys()):
        signals = signal_map[ticker]
        if not signals:
            continue

        # Build headline
        headline = f"你观察名单里的 <b>{ticker}</b>:"
        headline += "今日 " + " + ".join(signals[:3])
        if len(signals) > 3:
            headline += f" 等{len(signals)}项动态"

        crossref.append({
            "ticker": ticker,
            "text": headline,
        })

    log.info("  Crossref matches: %d tickers", len(crossref))
    return crossref


# ═══════════════════════════════════════════════════════════════════════════
# 7. OUTPUT & TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════

def build_output(
    crossref: list[dict],
    fund_moves: list[dict],
    insider_buys: list[dict],
    ark_moves: list[dict],
    crowd: list[dict],
    influencers: list[dict],
    sources_ok: dict,
) -> dict:
    """Build the final smart/{date}.json structure."""
    return {
        "as_of": _now_str(),
        "crossref": crossref,
        "fund_moves": fund_moves,
        "insider_buys": insider_buys,
        "ark": ark_moves,
        "crowd": crowd,
        "influencers": influencers,
        "sources_ok": sources_ok,
    }


def send_telegram_if_needed(output: dict) -> None:
    """Send Telegram notification if crossref is non-empty."""
    crossref = output.get("crossref", [])
    if not crossref:
        log.info("  No crossref matches — skipping Telegram")
        return

    try:
        from dotenv import load_dotenv  # noqa: E402
        load_dotenv(_ROOT / ".env")

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        if not bot_token or not chat_id:
            log.warning("  Telegram credentials not set — skipping notification")
            return

        import requests  # noqa: E402

        # Build message
        lines = ["📊 <b>Smart Money 信号</b>"]
        lines.append(f"⏰ {output['as_of']}")
        lines.append("")

        for item in crossref[:5]:  # Max 5 to avoid message length limits
            lines.append(item["text"])
            lines.append("")

        # Add source status
        ok = output.get("sources_ok", {})
        status_parts = []
        for src in ["edgar", "openfigi", "ark", "reddit", "x"]:
            icon = "✅" if ok.get(src) else "❌"
            status_parts.append(f"{src}{icon}")
        lines.append("数据源: " + " | ".join(status_parts))

        message = "\n".join(lines)

        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("  Telegram notification sent")
        else:
            log.warning("  Telegram send failed: %s", resp.text[:200])

    except Exception as e:
        log.warning("  Telegram error: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    """Run Pipeline C end-to-end."""
    log.info("═" * 60)
    log.info("PIPELINE C — SMART MONEY TRACKER")
    log.info("Started: %s", _now_str())
    log.info("═" * 60)

    t0 = time.time()

    # Load tracked entities
    entities_path = _ROOT / "tracked_entities.json"
    if not entities_path.exists():
        log.error("tracked_entities.json not found — aborting")
        return

    entities = load_json(entities_path)
    log.info("Loaded entities: %d 13F funds, %d ARK, %d Reddit, %d X",
             len(entities.get("funds_13f", [])),
             len(entities.get("ark_funds", [])),
             len(entities.get("reddit_subs", [])),
             len(entities.get("x_accounts", [])))

    # Load CUSIP cache
    cusip_cache_path = CACHE / "cusip_ticker.json"
    cusip_cache = {}
    if cusip_cache_path.exists():
        cusip_cache = _load_json_file(cusip_cache_path)
        log.info("Loaded CUSIP cache: %d entries", len(cusip_cache))

    # Track source status
    sources_ok = {
        "edgar": True,
        "openfigi": True,
        "ark": True,
        "reddit": True,
        "x": False,
    }

    # Step 1: 13F Holdings
    try:
        fund_moves = fetch_13f(entities, cusip_cache)
    except Exception as e:
        log.error("13F fetch failed: %s", e)
        fund_moves = []
        sources_ok["edgar"] = False

    # Step 2: Form 4 Insiders
    try:
        insider_buys = fetch_form4_insiders(entities)
    except Exception as e:
        log.error("Form 4 fetch failed: %s", e)
        insider_buys = []

    # Step 3: ARK Holdings
    try:
        ark_moves, ark_ok = fetch_ark(entities)
        sources_ok["ark"] = ark_ok
    except Exception as e:
        log.error("ARK fetch failed: %s", e)
        ark_moves = []
        sources_ok["ark"] = False

    # Step 4: Reddit
    try:
        crowd, reddit_ok = fetch_reddit(entities)
        sources_ok["reddit"] = reddit_ok
    except Exception as e:
        log.error("Reddit fetch failed: %s", e)
        crowd = []
        sources_ok["reddit"] = False

    # Step 5: X / Twitter
    try:
        tweets_raw, x_ok, raw_for_llm = fetch_x_tweets(entities)
        sources_ok["x"] = x_ok

        if x_ok and raw_for_llm:
            influencers = summarize_x_with_llm(raw_for_llm)
        else:
            influencers = []
    except Exception as e:
        log.error("X/Twitter fetch failed: %s", e)
        influencers = []
        sources_ok["x"] = False

    # Save CUSIP cache
    if cusip_cache:
        _save_cache(cusip_cache_path, cusip_cache)

    # Step 6: Cross-reference
    try:
        crossref = crossref_with_value(
            fund_moves, insider_buys, ark_moves, crowd, influencers
        )
    except Exception as e:
        log.error("Crossref failed: %s", e)
        crossref = []

    # Step 7: Output
    output = build_output(
        crossref, fund_moves, insider_buys, ark_moves,
        crowd, influencers, sources_ok
    )

    # Write smart/{date}.json
    date_str = _today_str()
    output_path = SMART / f"{date_str}.json"
    save_json(output_path, output)
    log.info("Saved output → %s", output_path)

    # Update smart/index.json
    index_path = SMART / "index.json"
    update_index(index_path, {
        "id": date_str,
        "file": f"{date_str}.json",
        "as_of": output["as_of"],
        "crossref_count": len(crossref),
        "sources_ok": sources_ok,
    })

    # Telegram notification
    send_telegram_if_needed(output)

    elapsed = time.time() - t0
    log.info("═" * 60)
    log.info("PIPELINE C COMPLETE — %.1fs", elapsed)
    log.info("  Fund moves: %d", len(fund_moves))
    log.info("  Insider buys: %d", len(insider_buys))
    log.info("  ARK moves: %d", len(ark_moves))
    log.info("  Reddit tickers: %d", len(crowd))
    log.info("  X accounts: %d", len(influencers))
    log.info("  Crossref matches: %d", len(crossref))
    log.info("  Sources: %s", sources_ok)
    log.info("═" * 60)


if __name__ == "__main__":
    main()
