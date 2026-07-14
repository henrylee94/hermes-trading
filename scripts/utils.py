"""Shared utilities for Hermes pipelines."""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Project root (parent.parent of this script → Hermes/)
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
LOGS = ROOT / "logs"
DATA = ROOT / "hermes_site" / "data"
JOURNAL = ROOT / "journal"


def get_env(key: str, required: bool = True) -> str | None:
    """Get an environment variable, loading .env first.

    Args:
        key: Name of the environment variable.
        required: If True (default), raise RuntimeError when missing.

    Returns:
        The value of the env var, or None if not required and missing.
    """
    load_dotenv(ROOT / ".env")
    val = os.getenv(key)
    if required and not val:
        raise RuntimeError(f"Missing env var: {key}. Set it in .env")
    return val


def setup_logger(name: str) -> logging.Logger:
    """Create a logger that writes to both console and a daily log file.

    Log file: logs/hermes_YYYYMMDD.log

    Args:
        name: Logger name (typically the module or pipeline name).

    Returns:
        Configured Logger instance.
    """
    LOGS.mkdir(parents=True, exist_ok=True)
    log_file = LOGS / f"hermes_{datetime.now().strftime('%Y%m%d')}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        # File handler — full timestamps and level
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
        )
        logger.addHandler(fh)

        # Console handler — timestamp + name + message
        ch = logging.StreamHandler()
        ch.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(message)s")
        )
        logger.addHandler(ch)

    return logger


def load_json(path: Path) -> dict | list:
    """Load and return parsed JSON from *path*."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict | list) -> None:
    """Write *data* to *path* as pretty-printed JSON.

    Parent directories are created automatically.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config() -> dict:
    """Shortcut: load and return the project config.json."""
    return load_json(ROOT / "config.json")


def retry(
    fn,
    retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    logger: logging.Logger | None = None,
):
    """Call *fn* up to *retries* times with exponential backoff.

    Args:
        fn: Zero-argument callable to execute.
        retries: Maximum number of attempts (default 3).
        delay: Initial wait time in seconds between retries.
        backoff: Multiplier applied to *delay* after each failure.
        logger: Optional logger; warnings are emitted on each failure.

    Returns:
        The return value of *fn* on success.

    Raises:
        The last exception from *fn* if all attempts fail.
    """
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if logger:
                logger.warning(
                    f"Attempt {attempt + 1}/{retries} failed: {e}"
                )
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= backoff


def append_journal(
    date: str,
    pipeline: str,
    ticker: str,
    action: str,
    price: float,
) -> None:
    """Append a recommendation row to journal/recommendations.csv.

    Creates the CSV with headers on first write.

    Args:
        date: Recommendation date (ISO format).
        pipeline: Pipeline name (e.g. 'long', 'swing').
        ticker: Stock ticker symbol.
        action: Recommended action (e.g. 'BUY', 'SELL').
        price: Price at time of recommendation.
    """
    JOURNAL.mkdir(parents=True, exist_ok=True)
    csv_path = JOURNAL / "recommendations.csv"
    exists = csv_path.exists()

    with open(csv_path, "a", encoding="utf-8") as f:
        if not exists:
            f.write("date,pipeline,ticker,action,price,actual_outcome\n")
        f.write(f"{date},{pipeline},{ticker},{action},{price},\n")


def call_llm(prompt: str, temperature: float = 0.2) -> str | None:
    """Call mimo LLM via Anthropic-compatible API. Returns text or None."""
    try:
        load_dotenv(ROOT / ".env")  # ensure .env is loaded
        import requests
        api_key = os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        base_url = os.getenv("LLM_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/anthropic")
        if not api_key:
            log.warning("call_llm: no API key configured")
            return None
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": os.getenv("LLM_MODEL", "mimo-v2.5-pro"),
            "max_tokens": 2000,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        url = f"{base_url}/v1/messages"
        resp = requests.post(url, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        # Find text block (mimo may return thinking blocks first)
        for block in data.get("content", []):
            if block.get("type") == "text":
                return block["text"]
        log.warning("call_llm: no text block in response, content types: %s",
                     [b.get("type") for b in data.get("content", [])])
        return None
    except Exception as e:
        log.warning("call_llm failed: %s", e)
        return None


def update_index(index_path: Path, new_entry: dict) -> None:
    """Prepend *new_entry* to an index JSON list.

    If a previous entry has the same ``id`` field it is replaced.
    Creates the file (as ``[]``) if it does not exist.

    Args:
        index_path: Path to the index JSON file.
        new_entry: Dict to insert (must contain an ``id`` key).
    """
    index_path.parent.mkdir(parents=True, exist_ok=True)

    if index_path.exists():
        idx = load_json(index_path)
    else:
        idx = []

    # Remove duplicate by id
    idx = [e for e in idx if e.get("id") != new_entry.get("id")]
    idx.insert(0, new_entry)
    save_json(index_path, idx)
