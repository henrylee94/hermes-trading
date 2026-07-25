"""AI Analyst — wraps TradingAgents for the hermes-trading dashboard.

Runs multi-agent stock analysis via mimo-v2.5 (OpenAI-compatible).
Full version: all agents, 1 debate round, 1 risk round.
Lite version: market + sentiment + news + fundamentals analysts only, no debate.

Stores results in SQLite for comparison and history.
"""
import json
import os
import re
import sqlite3
import sys
import time
import threading
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "ai_analyst.db"


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

def _get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_analyses (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            version         TEXT NOT NULL,       -- 'full' or 'lite'
            trade_date      TEXT NOT NULL,        -- auto-set to today
            status          TEXT NOT NULL DEFAULT 'running',
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            duration_s      REAL,
            -- Reports
            signal          TEXT,
            decision        TEXT,
            market_report   TEXT,
            sentiment_report TEXT,
            news_report     TEXT,
            fundamentals_report TEXT,
            investment_plan TEXT,
            debate_summary  TEXT,
            -- Structured summaries (extracted for UI)
            bull_points     TEXT,   -- JSON list: key bull arguments
            bear_points     TEXT,   -- JSON list: key bear arguments
            key_metrics     TEXT,   -- JSON: {pe, revenue, target_price, ...}
            -- Metrics
            token_usage     TEXT,   -- JSON: {input_tokens, output_tokens, total}
            cost_info       TEXT,
            agent_count     INTEGER,
            config_used     TEXT,
            error_msg       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ai_ticker ON ai_analyses(ticker);
        CREATE INDEX IF NOT EXISTS idx_ai_status ON ai_analyses(status);
    """)
    # Migration: add new columns if missing
    try:
        conn.execute("SELECT bull_points FROM ai_analyses LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE ai_analyses ADD COLUMN bull_points TEXT")
        conn.execute("ALTER TABLE ai_analyses ADD COLUMN bear_points TEXT")
        conn.execute("ALTER TABLE ai_analyses ADD COLUMN key_metrics TEXT")
    conn.close()


def save_analysis(result: dict) -> int:
    conn = _get_db()
    cur = conn.execute("""
        INSERT INTO ai_analyses
        (ticker, version, trade_date, status, started_at, finished_at,
         duration_s, signal, decision, market_report, sentiment_report,
         news_report, fundamentals_report, investment_plan, debate_summary,
         bull_points, bear_points, key_metrics,
         token_usage, cost_info, agent_count, config_used, error_msg)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        result.get("ticker"),
        result.get("version"),
        result.get("trade_date"),
        result.get("status", "running"),
        result.get("started_at"),
        result.get("finished_at"),
        result.get("duration_s"),
        result.get("signal"),
        result.get("decision"),
        result.get("market_report"),
        result.get("sentiment_report"),
        result.get("news_report"),
        result.get("fundamentals_report"),
        result.get("investment_plan"),
        result.get("debate_summary"),
        json.dumps(result.get("bull_points")) if result.get("bull_points") else None,
        json.dumps(result.get("bear_points")) if result.get("bear_points") else None,
        json.dumps(result.get("key_metrics")) if result.get("key_metrics") else None,
        json.dumps(result.get("token_usage")) if result.get("token_usage") else None,
        json.dumps(result.get("cost_info")) if result.get("cost_info") else None,
        result.get("agent_count"),
        json.dumps(result.get("config_used")) if result.get("config_used") else None,
        result.get("error_msg"),
    ))
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def update_analysis(row_id: int, updates: dict):
    conn = _get_db()
    sets, vals = [], []
    for k, v in updates.items():
        sets.append(f"{k} = ?")
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    vals.append(row_id)
    conn.execute(f"UPDATE ai_analyses SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def get_analysis(row_id: int) -> dict | None:
    conn = _get_db()
    row = conn.execute("SELECT * FROM ai_analyses WHERE id = ?", (row_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_analyses(limit: int = 50, ticker: str = None) -> list[dict]:
    conn = _get_db()
    if ticker:
        rows = conn.execute(
            "SELECT * FROM ai_analyses WHERE ticker = ? AND status != 'error' ORDER BY id DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ai_analyses WHERE status != 'error' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_tickers() -> list[dict]:
    """Return distinct tickers with analysis counts."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT ticker,
               COUNT(*) as total,
               SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done,
               MAX(finished_at) as last_analysis
        FROM ai_analyses
        WHERE status != 'error'
        GROUP BY ticker
        ORDER BY last_analysis DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_token_summary() -> dict:
    """Aggregate token usage across all completed analyses."""
    conn = _get_db()
    row = conn.execute("""
        SELECT COUNT(*) as total_analyses,
               SUM(CASE WHEN version='full' THEN 1 ELSE 0 END) as full_count,
               SUM(CASE WHEN version='lite' THEN 1 ELSE 0 END) as lite_count,
               AVG(duration_s) as avg_duration
        FROM ai_analyses WHERE status = 'done'
    """).fetchone()
    conn.close()
    result = dict(row)

    # Sum tokens from JSON
    conn2 = _get_db()
    rows = conn2.execute("SELECT token_usage FROM ai_analyses WHERE status='done' AND token_usage IS NOT NULL").fetchall()
    conn2.close()
    total_input, total_output = 0, 0
    for r in rows:
        try:
            tu = json.loads(r["token_usage"])
            total_input += tu.get("input_tokens", 0)
            total_output += tu.get("output_tokens", 0)
        except (json.JSONDecodeError, TypeError):
            pass
    result["total_input_tokens"] = total_input
    result["total_output_tokens"] = total_output
    result["total_tokens"] = total_input + total_output
    result["avg_duration"] = round(result["avg_duration"] or 0, 1)
    return result


# ---------------------------------------------------------------------------
# Structured summary extraction
# ---------------------------------------------------------------------------

def _extract_key_metrics(final_state: dict, signal: str) -> dict:
    """Pull key numeric metrics from reports for the summary card."""
    metrics = {"signal": str(signal)}

    # Try to extract from fundamentals report
    fund = _extract_text(final_state.get("fundamentals_report")) or ""
    # Revenue
    m = re.search(r"Revenue[:\s]*\$?([\d,.]+)\s*(B|M|K|billion|million)?", fund, re.I)
    if m:
        metrics["revenue"] = f"${m.group(1)}{m.group(2) or ''}"
    # PE
    m = re.search(r"(Forward\s+)?P/?E[:\s]*([\d.]+)", fund, re.I)
    if m:
        metrics["pe"] = m.group(2)
    # EPS
    m = re.search(r"EPS[:\s]*\$?([\d.]+)", fund, re.I)
    if m:
        metrics["eps"] = f"${m.group(1)}"
    # Market cap
    m = re.search(r"Market\s+Cap[:\s]*\$?([\d,.]+)\s*(B|T|M|billion|trillion|million)?", fund, re.I)
    if m:
        metrics["market_cap"] = f"${m.group(1)}{m.group(2) or ''}"

    # From market report — latest price
    market = _extract_text(final_state.get("market_report")) or ""
    m = re.search(r"(?:Latest\s+Close|Close|Price)[:\s]*\$?([\d.]+)", market, re.I)
    if m:
        metrics["price"] = f"${m.group(1)}"

    # From investment plan
    plan = _extract_text(final_state.get("investment_plan")) or ""
    m = re.search(r"(?:Target|Price\s+Target|Fair\s+Value)[:\s]*\$?([\d.]+)", plan, re.I)
    if m:
        metrics["target_price"] = f"${m.group(1)}"

    return metrics


def _extract_bullet_points(text: str, max_points: int = 5) -> list[str]:
    """Extract key bullet points from a report section.
    Looks for existing bullet patterns, numbered lists, or key sentences."""
    if not text:
        return []

    points = []
    lines = text.split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Match bullet patterns: •, -, *, 1., 2., etc.
        m = re.match(r"^[\u2022\-\*]\s*(.+)", line)
        if m:
            points.append(m.group(1).strip())
            continue
        m = re.match(r"^\d+[\.\)]\s*(.+)", line)
        if m:
            points.append(m.group(1).strip())
            continue
        # Match bold key statements **...**
        m = re.match(r"^\*\*(.+?)\*\*", line)
        if m and len(m.group(1)) > 20:
            points.append(m.group(1).strip())
            continue

    # Deduplicate and limit
    seen = set()
    unique = []
    for p in points:
        key = p[:50].lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
        if len(unique) >= max_points:
            break

    return unique


def _extract_bull_bear_points(final_state: dict) -> tuple[list[str], list[str]]:
    """Extract bull and bear arguments from debate state."""
    bull_points = []
    bear_points = []

    debate = final_state.get("investment_debate_state", {})

    # Bull arguments
    bull_history = debate.get("bull_history", [])
    if isinstance(bull_history, str):
        bull_history = [bull_history]
    for entry in bull_history:
        text = _extract_text(entry) if not isinstance(entry, str) else entry
        if text:
            bull_points.extend(_extract_bullet_points(text, 3))

    # Bear arguments
    bear_history = debate.get("bear_history", [])
    if isinstance(bear_history, str):
        bear_history = [bear_history]
    for entry in bear_history:
        text = _extract_text(entry) if not isinstance(entry, str) else entry
        if text:
            bear_points.extend(_extract_bullet_points(text, 3))

    # If no debate (lite mode), extract from investment plan
    if not bull_points:
        plan = _extract_text(final_state.get("investment_plan")) or ""
        bull_points = _extract_bullet_points(plan, 4)

    return bull_points[:5], bear_points[:5]


# ---------------------------------------------------------------------------
# TradingAgents runner
# ---------------------------------------------------------------------------

_jobs: dict = {}
_jobs_lock = threading.Lock()


def _run_analysis(job_id: int, ticker: str, version: str, trade_date: str):
    """Run TradingAgents in a background thread."""
    # Set env vars BEFORE importing TradingAgents
    os.environ["OPENAI_COMPATIBLE_API_KEY"] = os.getenv(
        "XIAOMI_API_KEY",
        os.getenv("OPENAI_COMPATIBLE_API_KEY", ""),
    )
    os.environ["TRADINGAGENTS_LLM_PROVIDER"] = "openai_compatible"
    os.environ["TRADINGAGENTS_DEEP_THINK_LLM"] = "mimo-v2.5"
    os.environ["TRADINGAGENTS_QUICK_THINK_LLM"] = "mimo-v2.5"
    os.environ["TRADINGAGENTS_LLM_BACKEND_URL"] = "https://token-plan-sgp.xiaomimimo.com/v1"
    os.environ["TRADINGAGENTS_CHECKPOINT_ENABLED"] = "false"

    if version == "lite":
        os.environ["TRADINGAGENTS_MAX_DEBATE_ROUNDS"] = "0"
        os.environ["TRADINGAGENTS_MAX_RISK_ROUNDS"] = "0"
    else:
        os.environ["TRADINGAGENTS_MAX_DEBATE_ROUNDS"] = "1"
        os.environ["TRADINGAGENTS_MAX_RISK_ROUNDS"] = "1"

    started_at = datetime.now().isoformat()
    result_id = None

    try:
        result_id = save_analysis({
            "ticker": ticker,
            "version": version,
            "trade_date": trade_date,
            "status": "running",
            "started_at": started_at,
            "config_used": {
                "provider": "openai_compatible",
                "model": "mimo-v2.5",
                "debate_rounds": int(os.environ.get("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "1")),
                "risk_rounds": int(os.environ.get("TRADINGAGENTS_MAX_RISK_ROUNDS", "1")),
            },
        })

        with _jobs_lock:
            _jobs[job_id]["result_id"] = result_id

        # Import and run TradingAgents
        sys.path.insert(0, str(ROOT / "venv_new" / "lib" / "python3.14" / "site-packages"))
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG

        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = "openai_compatible"
        config["backend_url"] = "https://token-plan-sgp.xiaomimimo.com/v1"
        config["deep_think_llm"] = "mimo-v2.5"
        config["quick_think_llm"] = "mimo-v2.5"
        config["checkpoint_enabled"] = False
        if version == "lite":
            config["max_debate_rounds"] = 0
            config["max_risk_discuss_rounds"] = 0
        else:
            config["max_debate_rounds"] = 1
            config["max_risk_discuss_rounds"] = 1

        ta = TradingAgentsGraph(debug=False, config=config)
        start_time = time.time()
        final_state, signal = ta.propagate(ticker, trade_date)
        duration = time.time() - start_time

        # Extract reports
        market_report = _extract_text(final_state.get("market_report"))
        sentiment_report = _extract_text(final_state.get("sentiment_report"))
        news_report = _extract_text(final_state.get("news_report"))
        fundamentals_report = _extract_text(final_state.get("fundamentals_report"))
        investment_plan = _extract_text(final_state.get("investment_plan"))
        final_decision = _extract_text(final_state.get("final_trade_decision"))

        # Debate summaries
        bull_bear = final_state.get("investment_debate_state", {})
        risk_debate = final_state.get("risk_debate_state", {})
        debate_parts = []
        if bull_bear.get("judge_decision"):
            debate_parts.append(f"[投资辩论裁判]\n{_extract_text(bull_bear['judge_decision'])}")
        if risk_debate.get("judge_decision"):
            debate_parts.append(f"[风险辩论裁判]\n{_extract_text(risk_debate['judge_decision'])}")
        debate_summary = "\n\n".join(debate_parts) if debate_parts else None

        # Structured extraction for UI
        key_metrics = _extract_key_metrics(final_state, signal)
        bull_points, bear_points = _extract_bull_bear_points(final_state)

        agent_count = _count_agents(version)
        token_usage = _estimate_tokens(final_state, version)

        finished_at = datetime.now().isoformat()
        update_analysis(result_id, {
            "status": "done",
            "finished_at": finished_at,
            "duration_s": round(duration, 1),
            "signal": str(signal),
            "decision": final_decision,
            "market_report": market_report,
            "sentiment_report": sentiment_report,
            "news_report": news_report,
            "fundamentals_report": fundamentals_report,
            "investment_plan": investment_plan,
            "debate_summary": debate_summary,
            "bull_points": bull_points,
            "bear_points": bear_points,
            "key_metrics": key_metrics,
            "token_usage": token_usage,
            "agent_count": agent_count,
        })

        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["duration"] = round(duration, 1)

    except Exception as e:
        tb = traceback.format_exc()
        finished_at = datetime.now().isoformat()
        if result_id:
            update_analysis(result_id, {
                "status": "error",
                "finished_at": finished_at,
                "error_msg": f"{e}\n{tb}",
            })
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)


def _extract_text(val) -> str:
    """Extract text from various LangChain/AI message formats."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("content", val.get("output", json.dumps(val, default=str)))
    content = getattr(val, "content", None)
    if content:
        return str(content)
    return str(val)


def _count_agents(version: str) -> int:
    base = 4  # market, sentiment, news, fundamentals
    if version == "full":
        return base + 4  # bull, bear, trader, risk
    return base


def _estimate_tokens(final_state: dict, version: str) -> dict:
    all_text = ""
    for key in ["market_report", "sentiment_report", "news_report",
                 "fundamentals_report", "investment_plan", "final_trade_decision"]:
        val = final_state.get(key)
        if val:
            all_text += _extract_text(val) or ""

    debate = final_state.get("investment_debate_state", {})
    for key in ["bull_history", "bear_history", "judge_decision"]:
        val = debate.get(key)
        if val:
            all_text += _extract_text(val) or ""

    risk = final_state.get("risk_debate_state", {})
    for key in ["aggressive_history", "conservative_history", "neutral_history", "judge_decision"]:
        val = risk.get(key)
        if val:
            all_text += _extract_text(val) or ""

    output_tokens = len(all_text) // 4
    input_multiplier = 3 if version == "full" else 2
    input_tokens = output_tokens * input_multiplier
    total = input_tokens + output_tokens

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total": total,
        "estimated": True,
        "note": "Estimated from output length.",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_analysis(ticker: str, version: str = "full") -> int:
    """Start an analysis job in background. Returns job_id."""
    trade_date = datetime.now().strftime("%Y-%m-%d")

    ticker = ticker.upper().strip()
    if not ticker or not ticker.isalpha():
        raise ValueError(f"Invalid ticker: {ticker}")
    if version not in ("full", "lite"):
        raise ValueError(f"Invalid version: {version}. Use 'full' or 'lite'.")

    init_db()

    job_id = int(time.time() * 1000) % 1_000_000_000
    with _jobs_lock:
        _jobs[job_id] = {
            "ticker": ticker,
            "version": version,
            "trade_date": trade_date,
            "status": "running",
            "result_id": None,
        }

    thread = threading.Thread(
        target=_run_analysis,
        args=(job_id, ticker, version, trade_date),
        daemon=True,
    )
    thread.start()
    return job_id


def get_job_status(job_id: int) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return None
    result = None
    if job.get("result_id"):
        result = get_analysis(job["result_id"])
    return {**job, "result": result}


def get_history(ticker: str = None, limit: int = 50) -> list[dict]:
    init_db()
    return list_analyses(limit, ticker)


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
    job_id = start_analysis("NVDA", "lite")
    print(f"Job started: {job_id}")
    while True:
        time.sleep(5)
        status = get_job_status(job_id)
        print(f"Status: {status['status']}")
        if status["status"] in ("done", "error"):
            if status.get("result"):
                r = status["result"]
                print(f"Signal: {r.get('signal')}")
                print(f"Duration: {r.get('duration_s')}s")
            break
