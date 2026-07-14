#!/usr/bin/env python3
"""
Hermes Telegram Bot
====================
Interactive Telegram bot for Hermes investment assistant.

Commands:
  /help            — list all commands
  /zone            — show IN_ZONE stocks from value/{week}.json
  /watch SYM       — show one stock vs buy price
  /dalao           — latest smart-money crossref from smart/{date}.json
  /dot [SYM]       — recompute live 做T levels (指定个股或全部)
  /add SYM         — add to swing_watchlist.json pins
  /remove SYM      — remove from swing_watchlist.json pins

Dependencies:
  - scripts/utils.py (ROOT, DATA, load_json, save_json, load_config, setup_logger, get_env)
  - python-telegram-bot v20+ (async)
  - TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from .env

All replies in plain Chinese, beginner-friendly.
End with ⚠ 仅供参考,非投资建议.
"""
from __future__ import annotations

import fcntl
import json
import os
import urllib.request
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import ROOT, CACHE, DATA, load_json, save_json, load_config, setup_logger, get_env  # noqa: E402

# ---------------------------------------------------------------------------
# Constants & Paths
# ---------------------------------------------------------------------------
SMART_DIR = ROOT / "smart"
SWING_DIR = ROOT / "swing"
WATCHLIST_PATH = ROOT / "swing_watchlist.json"
LOCK_PATH = CACHE / "telegram_bot.lock"
PID_PATH = CACHE / "telegram_bot.pid"

DISCLAIMER = "⚠ 仅供参考,非投资建议"

log = setup_logger("telegram_bot")

# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

def acquire_lock():
    """Acquire file lock to prevent multiple bot instances."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = open(LOCK_PATH, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except OSError:
        # Another instance is running
        existing_pid = None
        if PID_PATH.exists():
            try:
                existing_pid = int(PID_PATH.read_text().strip())
            except ValueError:
                pass
        if existing_pid:
            try:
                os.kill(existing_pid, 0)
                log.error("Another bot instance is running (PID %d). Exiting.", existing_pid)
                print(f"错误: 另一个 bot 实例正在运行 (PID {existing_pid})")
                sys.exit(1)
            except ProcessLookupError:
                # PID doesn't exist, stale lock — remove it
                log.warning("Stale PID %d, removing lock", existing_pid)
                PID_PATH.unlink(missing_ok=True)
                LOCK_PATH.unlink(missing_ok=True)
                return acquire_lock()
        else:
            log.error("Another bot instance is running. Exiting.")
            print("错误: 另一个 bot 实例正在运行")
            sys.exit(1)


def release_lock(lock_fd):
    """Release file lock."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        LOCK_PATH.unlink(missing_ok=True)
        PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Permission check
# ---------------------------------------------------------------------------

def is_authorized(update) -> bool:
    """Check if message is from authorized chat."""
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    authorized = get_env("TELEGRAM_CHAT_ID", required=False) or ""
    if not authorized:
        # No restriction set — allow all (development mode)
        return True
    return chat_id == str(authorized)


# ---------------------------------------------------------------------------
# Helper: load latest JSON from a directory by date
# ---------------------------------------------------------------------------

def _load_latest_from_dir(directory: Path, pattern: str = "*.json") -> Optional[dict]:
    """Load the most recent JSON file from a directory (by filename sort)."""
    if not directory.exists():
        return None
    files = sorted(directory.glob(pattern), reverse=True)
    for f in files:
        if f.name in ("index.json",):
            continue
        try:
            return load_json(f)
        except Exception as e:
            log.warning("Failed to load %s: %s", f, e)
            continue
    return None


def _this_week_str() -> str:
    """Return ISO week as 'YYYY-WNN'."""
    today = datetime.now()
    return f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"


def _load_value_data() -> Optional[dict]:
    """Load latest value/{week}.json (try current week, then previous)."""
    value_dir = DATA / "value"
    week_str = _this_week_str()
    path = value_dir / f"{week_str}.json"
    if path.exists():
        try:
            return load_json(path)
        except Exception:
            pass
    # Try previous week
    from datetime import timedelta
    prev = datetime.now() - timedelta(days=7)
    prev_week = f"{prev.isocalendar()[0]}-W{prev.isocalendar()[1]:02d}"
    path = value_dir / f"{prev_week}.json"
    if path.exists():
        try:
            return load_json(path)
        except Exception:
            pass
    return None


def _load_smart_data() -> Optional[dict]:
    """Load latest smart/{date}.json."""
    return _load_latest_from_dir(SMART_DIR, pattern="2*.json")


def _load_swing_watchlist() -> dict:
    """Load swing_watchlist.json, create if missing."""
    if WATCHLIST_PATH.exists():
        try:
            return load_json(WATCHLIST_PATH)
        except Exception:
            pass
    return {"auto": [], "pins": []}


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def _finnhub_price(sym: str) -> float | None:
    """Get live price from Finnhub. Returns None on failure."""
    try:
        key = get_env("FINNHUB_API_KEY", "")
        if not key:
            return None
        url = f"https://finnhub.io/api/v1/quote?symbol={sym}&token={key}"
        r = urllib.request.urlopen(url, timeout=10)
        d = json.loads(r.read())
        return d.get("c")  # current price
    except Exception:
        return None


def _fmt_stock_line(s: dict, live_price: float = None) -> str:
    """Format a single stock result from pipeline_b into Chinese text.
    
    Shows ONE actionable recommendation (first buy/sell pair), not all 8 levels.
    """
    sym = s.get("sym", "?")
    name = s.get("name", sym)
    price = live_price if live_price else s.get("price")
    regime = s.get("regime", "trend")

    if s.get("price_only") or regime == "trend":
        pause = s.get("pause_reason", "暂停做T")
        return f"🔴 {sym} {name} ${price or '?'} — {pause}"

    box_low = s.get("box_low", "?")
    box_high = s.get("box_high", "?")
    shares = s.get("shares", 0)
    stop = s.get("stop", "?")
    step = s.get("step", "?")
    rsi = s.get("rsi", "")
    adx = s.get("adx", "")
    earn = s.get("earn_days")

    lines = []
    lines.append(f"✅ {sym} {name} ${price:.2f}" if price else f"✅ {sym} {name}")
    lines.append(f"   区间 [{box_low}–{box_high}] | {shares}股 | 止损${stop} | 步长${step}")

    # Show first buy/sell pair as the primary action
    buy_levels = s.get("buy_levels", [])
    sell_levels = s.get("sell_levels", [])

    if buy_levels:
        b1 = buy_levels[0]
        s1 = sell_levels[0] if sell_levels else None
        profit = f"+${(s1['price'] - price) * shares:,.0f}" if s1 and price else "?"
        lines.append(f"   💡 挂买 ${b1['price']} → 卖 ${s1['price'] if s1 else '?'} ({profit})")
    elif sell_levels:
        s1 = sell_levels[0]
        lines.append(f"   💡 等回落至 ${s1['price']} 以下再买")

    # Summary of remaining levels
    if len(buy_levels) > 1 or len(sell_levels) > 1:
        extra_b = len(buy_levels) - 1
        extra_s = len(sell_levels) - 1
        parts = []
        if extra_b:
            parts.append(f"下方还有{extra_b}档")
        if extra_s:
            parts.append(f"上方还有{extra_s}档")
        lines.append(f"   📊 {' | '.join(parts)}")

    # Earnings warning
    if earn is not None and earn < 7:
        lines.append(f"   ⚠️ 距财报仅{earn}天")

    return "\n".join(lines)


def _format_usd(value: float | int) -> str:
    """Format USD amount to Chinese-friendly string."""
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    elif value >= 10_000:
        return f"{value / 10_000:.0f}万"
    else:
        return f"${value:,.0f}"


# ---------------------------------------------------------------------------
# Telegram send helper (for cron use)
# ---------------------------------------------------------------------------

def send_push(text: str) -> bool:
    """Send a Telegram message to the configured chat. Returns True on success.

    This function is designed for cron/scheduler use outside the bot context.
    It uses the raw HTTP API (requests) instead of python-telegram-bot.
    """
    try:
        bot_token = get_env("TELEGRAM_BOT_TOKEN", required=True)
        chat_id = get_env("TELEGRAM_CHAT_ID", required=True)
    except RuntimeError as e:
        log.error("Cannot send push: %s", e)
        return False

    import requests  # noqa: E402
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("Push sent to Telegram")
            return True
        else:
            log.warning("Telegram push failed: %s", resp.text[:200])
            return False
    except Exception as e:
        log.warning("Telegram push error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_help(update, context) -> None:
    """List all available commands."""
    if not is_authorized(update):
        return
    log.info("Command /help from %s", update.effective_chat.id)

    text = (
        "📋 *Hermes 指令列表*\n\n"
        "💰 /zone — 查看当前入场区的股票\n"
        "👀 /watch SYM — 查看某只股票 vs 买入价\n"
        "🏦 /dalao — 最新大佬动态\n"
        "📊 /dot 或 /swing — 实时计算做T区间\n"
        "📌 /add SYM — 加入做T池\n"
        "📌 /remove SYM — 从做T池移除\n"
        "❓ /help — 显示本帮助\n\n"
        "示例:\n"
        "  /watch GOOGL\n"
        "  /add NVDA\n"
        "  /remove SNDK\n\n"
        f"{DISCLAIMER}"
    )
    await update.message.reply_text(text, parse_mode="HTML",
                                    disable_web_page_preview=True)


async def cmd_zone(update, context) -> None:
    """Show IN_ZONE stocks from latest value/{week}.json."""
    if not is_authorized(update):
        return
    log.info("Command /zone from %s", update.effective_chat.id)

    try:
        value_data = _load_value_data()
        if not value_data:
            await update.message.reply_text(
                "暂无数据，hermes 还没跑过这个管线。\n\n"
                "请先运行 Pipeline A 生成 value 数据。\n\n"
                f"{DISCLAIMER}"
            )
            return

        zone_items = value_data.get("zone", [])
        if not zone_items:
            await update.message.reply_text(
                "暂无入场区股票。\n\n"
                f"{DISCLAIMER}"
            )
            return

        lines = ["💰 *入场区:*\n"]
        for item in zone_items:
            if not isinstance(item, dict):
                continue
            ticker = item.get("ticker", "?")
            price = item.get("price", "?")
            buy_price = item.get("buy_price") or item.get("target", "?")
            risk = item.get("risk", "")
            health = item.get("health", "")
            note = item.get("note", "")
            entered = item.get("in_zone", False) or item.get("entered", False)

            status_icon = "🟢"
            entered_text = " ← 已进入" if entered else ""

            lines.append(
                f"{status_icon} *{ticker}* 现\\${price} / 推荐\\${buy_price}{entered_text}"
            )
            detail_parts = []
            if risk:
                detail_parts.append(f"破产风险 {risk}")
            if health:
                detail_parts.append(f"财务健康 {health}")
            if note:
                detail_parts.append(note)
            if detail_parts:
                lines.append(f"  ↳ {' · '.join(detail_parts)}")
            lines.append("")

        lines.append(DISCLAIMER)
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
        )

    except Exception as e:
        log.error("cmd_zone error: %s", e)
        await update.message.reply_text(
            "获取数据失败，请稍后重试。\n\n" + DISCLAIMER
        )


async def cmd_watch(update, context) -> None:
    """Show one stock vs its buy price from latest value data."""
    if not is_authorized(update):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "请指定股票代码，例如：\n/watch GOOGL\n\n" + DISCLAIMER
        )
        return

    sym = args[0].upper()
    log.info("Command /watch %s from %s", sym, update.effective_chat.id)

    try:
        value_data = _load_value_data()
        if not value_data:
            await update.message.reply_text(
                "暂无数据，hermes 还没跑过这个管线。\n\n" + DISCLAIMER
            )
            return

        # Search in zone + watch sections
        found = None
        for section in ["zone", "watch"]:
            for item in value_data.get(section, []):
                if not isinstance(item, dict):
                    continue
                ticker = item.get("ticker", "").upper()
                if ticker == sym:
                    found = item
                    break
            if found:
                break

        if not found:
            # Try direct stock lookup via pipeline_b
            try:
                from scripts.pipeline_b_swing import compute_one
                cfg = load_config()
                result = compute_one(sym, cfg)
                if result and result.get("price"):
                    price = result["price"]
                    box_low = result.get("box_low", "?")
                    box_high = result.get("box_high", "?")
                    name = result.get("name", sym)
                    regime = result.get("regime", "trend")

                    if regime == "range":
                        status = "🟢 区间震荡,适合做T"
                    else:
                        pause = result.get("pause_reason", "趋势中")
                        status = f"🔴 {pause}"

                    text = (
                        f"👀 *{sym}* ({name})\n"
                        f"现价: \\${price}\n"
                        f"区间: [{box_low}–{box_high}]\n"
                        f"状态: {status}\n\n"
                        f"{DISCLAIMER}"
                    )
                    await update.message.reply_text(
                        text, parse_mode="HTML", disable_web_page_preview=True
                    )
                    return
            except ImportError:
                pass

            await update.message.reply_text(
                f"找不到 {sym}，请检查代码。\n\n" + DISCLAIMER
            )
            return

        # Found in value data
        price = found.get("price", "?")
        buy_price = found.get("buy_price") or found.get("target", "?")
        name = found.get("name", sym)

        # Calculate gap percentage
        gap_text = ""
        try:
            p = float(price)
            b = float(buy_price)
            if b > 0:
                gap = (p - b) / b * 100
                gap_text = f"还差: {abs(gap):.1f}%"
                if gap <= 0:
                    gap_text += " ✅已进入入场区"
        except (ValueError, TypeError):
            pass

        risk = found.get("risk", "")
        health = found.get("health", "")
        note = found.get("note", "")

        status_parts = []
        if risk:
            status_parts.append(f"破产风险 {risk}")
        if health:
            status_parts.append(f"财务健康 {health}")

        text = (
            f"👀 *{sym}* ({name})\n"
            f"现价: \\${price}\n"
            f"推荐买入: \\${buy_price}\n"
        )
        if gap_text:
            text += f"{gap_text}\n"
        if status_parts:
            text += f"状态: {' · '.join(status_parts)}\n"
        if note:
            text += f"备注: {note}\n"
        text += f"\n{DISCLAIMER}"

        await update.message.reply_text(
            text, parse_mode="HTML", disable_web_page_preview=True
        )

    except Exception as e:
        log.error("cmd_watch error: %s", e)
        await update.message.reply_text(
            "获取数据失败，请稍后重试。\n\n" + DISCLAIMER
        )


async def cmd_dalao(update, context) -> None:
    """Show latest smart-money crossref from smart/{date}.json."""
    if not is_authorized(update):
        return
    log.info("Command /dalao from %s", update.effective_chat.id)

    try:
        smart_data = _load_smart_data()
        if not smart_data:
            await update.message.reply_text(
                "暂无数据，hermes 还没跑过这个管线。\n\n"
                "请先运行 Pipeline C 生成 smart 数据。\n\n"
                f"{DISCLAIMER}"
            )
            return

        as_of = smart_data.get("as_of", "")
        date_label = as_of.split(" ")[0] if as_of else datetime.now().strftime("%m-%d")

        lines = [f"★ *大佬 ({date_label})*\n"]

        # ── Crossref (和你相关) ──
        crossref = smart_data.get("crossref", [])
        if crossref:
            lines.append("⭐ *和你相关:*")
            for item in crossref[:8]:
                ticker = item.get("ticker", "?")
                text = item.get("text", "")
                # Strip HTML tags for plain text
                import re
                clean = re.sub(r"<[^>]+>", "", text)
                lines.append(f"  {clean}")
            lines.append("")

        # ── Fund moves (大佬持仓) ──
        fund_moves = smart_data.get("fund_moves", [])
        if fund_moves:
            lines.append("🏦 *大佬持仓:*")
            for item in fund_moves[:6]:
                ticker = item.get("ticker", "?")
                fund = item.get("fund", item.get("name", "?"))
                action = item.get("action", "")
                trust = item.get("trust", "")
                icon = "🟢" if trust == "official" else "🟡" if trust == "crowd" else "⚪"
                action_cn = {"ADD": "加仓", "NEW": "新建仓", "TRIM": "减仓", "EXIT": "清仓"}.get(action, action)
                lines.append(f"  {ticker} — {fund} {action_cn}({icon}{trust})")
            lines.append("")

        # ── Insider buys (高管自购) ──
        insider = smart_data.get("insider_buys", [])
        if insider:
            lines.append("👔 *高管自购:*")
            for item in insider[:6]:
                ticker = item.get("ticker", "?")
                people = item.get("people", item.get("count", "?"))
                amount = item.get("amount", item.get("usd", "?"))
                trust = item.get("trust", "")
                icon = "🟢" if trust == "official" else "🟡" if trust == "crowd" else "⚪"
                lines.append(f"  {ticker} — {people}人 {_format_usd(amount) if isinstance(amount, (int, float)) else amount} 集体买入({icon}{trust})")
            lines.append("")

        # ── ARK (ARK) ──
        ark = smart_data.get("ark", [])
        if ark:
            lines.append("🚀 *ARK:*")
            for item in ark[:6]:
                ticker = item.get("ticker", "?")
                action = item.get("action", "")
                trust = item.get("trust", "")
                icon = "🟢" if trust == "official" else "🟡"
                action_cn = {"ADD": "买入", "NEW": "建仓", "TRIM": "减仓"}.get(action, action)
                lines.append(f"  {ticker} — {action_cn}({icon}{trust})")
            lines.append("")

        if not any([crossref, fund_moves, insider, ark]):
            lines.append("今日暂无新的大佬动态。")

        lines.append(DISCLAIMER)
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
        )

    except Exception as e:
        log.error("cmd_dalao error: %s", e)
        await update.message.reply_text(
            "获取数据失败，请稍后重试。\n\n" + DISCLAIMER
        )


async def cmd_dot(update, context) -> None:
    """Recompute live 做T levels. Usage: /dot [TICKER]"""
    if not is_authorized(update):
        return
    log.info("Command /dot from %s args=%s", update.effective_chat.id, context.args)

    try:
        # Import pipeline_b
        try:
            from scripts.pipeline_b_swing import compute_one
        except ImportError:
            sys.path.insert(0, str(ROOT / "scripts"))
            from pipeline_b_swing import compute_one

        cfg = load_config()

        # If ticker specified, compute single stock
        if context.args:
            sym = context.args[0].upper()
            await update.message.reply_text(f"⏳ 正在计算 {sym} 做T区间…")

            # Get live price from Finnhub
            live_price = _finnhub_price(sym)

            result = compute_one(sym, cfg)
            line = _fmt_stock_line(result, live_price=live_price)
            msg = f"📊 {sym} 实时做T分析\n\n{line}\n\n" + DISCLAIMER
            await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
            return

        # No ticker — compute all pool stocks
        await update.message.reply_text("⏳ 正在计算做T区间，请稍候…")

        wl = _load_swing_watchlist()
        auto_syms = [s["sym"] for s in wl.get("auto", []) if isinstance(s, dict) and "sym" in s]
        pin_syms = [s["sym"] for s in wl.get("pins", []) if isinstance(s, dict) and "sym" in s]
        all_syms = list(dict.fromkeys(auto_syms + pin_syms))

        if not all_syms:
            await update.message.reply_text(
                "做T池为空，请先用 /add SYM 添加股票。\n\n" + DISCLAIMER
            )
            return

        now = datetime.now()
        time_str = now.strftime("%H:%M")

        lines = [f"现在适合做T (实时 {time_str}):\n"]

        for i, sym in enumerate(all_syms, 1):
            try:
                result = compute_one(sym, cfg)
                line = _fmt_stock_line(result)
                lines.append(f"① {line}" if i == 1 else f"② {line}" if i == 2
                             else f"③ {line}" if i == 3 else f"④ {line}" if i == 4
                             else f"⑤ {line}" if i == 5 else f"{i}. {line}")
            except Exception as e:
                log.warning("compute_one(%s) failed: %s", sym, e)
                lines.append(f"① {sym} — 计算错误: {e}")

        lines.append("")
        lines.append(DISCLAIMER)
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
        )

    except Exception as e:
        log.error("cmd_dot error: %s", e)
        await update.message.reply_text(
            "计算失败，请稍后重试。\n\n" + DISCLAIMER
        )


# Alias: /swing → /dot
async def cmd_swing(update, context) -> None:
    """Alias for /dot — recompute live levels."""
    await cmd_dot(update, context)


async def cmd_add(update, context) -> None:
    """Add a stock to swing_watchlist.json pins."""
    if not is_authorized(update):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "请指定股票代码，例如：\n/add NVDA\n\n" + DISCLAIMER
        )
        return

    sym = args[0].upper()
    log.info("Command /add %s from %s", sym, update.effective_chat.id)

    try:
        wl = _load_swing_watchlist()
        pins = wl.get("pins", [])

        # Check if already in pins
        existing_syms = [p.get("sym", "").upper() for p in pins if isinstance(p, dict)]
        if sym in existing_syms:
            await update.message.reply_text(
                f"✓ {sym} 已经在做T池中了。\n\n" + DISCLAIMER
            )
            return

        # Add to pins
        new_pin = {"sym": sym}
        pins.append(new_pin)
        wl["pins"] = pins
        save_json(WATCHLIST_PATH, wl)

        log.info("Added %s to swing_watchlist.json pins", sym)
        await update.message.reply_text(
            f"✓ 已加入做T池: {sym}\n\n" + DISCLAIMER
        )

    except Exception as e:
        log.error("cmd_add error: %s", e)
        await update.message.reply_text(
            "操作失败，请稍后重试。\n\n" + DISCLAIMER
        )


async def cmd_remove(update, context) -> None:
    """Remove a stock from swing_watchlist.json pins."""
    if not is_authorized(update):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "请指定股票代码，例如：\n/remove SNDK\n\n" + DISCLAIMER
        )
        return

    sym = args[0].upper()
    log.info("Command /remove %s from %s", sym, update.effective_chat.id)

    try:
        wl = _load_swing_watchlist()
        pins = wl.get("pins", [])

        # Find and remove
        new_pins = [p for p in pins if isinstance(p, dict) and p.get("sym", "").upper() != sym]
        removed = len(new_pins) < len(pins)

        wl["pins"] = new_pins
        save_json(WATCHLIST_PATH, wl)

        if removed:
            log.info("Removed %s from swing_watchlist.json pins", sym)
            await update.message.reply_text(
                f"✓ 已移除做T池: {sym}\n\n" + DISCLAIMER
            )
        else:
            await update.message.reply_text(
                f"找不到 {sym} 在做T池中。\n\n" + DISCLAIMER
            )

    except Exception as e:
        log.error("cmd_remove error: %s", e)
        await update.message.reply_text(
            "操作失败，请稍后重试。\n\n" + DISCLAIMER
        )


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update, context) -> None:
    """Log errors from handlers."""
    log.error("Handler error: %s", context.error)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "出错了，请稍后重试。\n\n" + DISCLAIMER
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Start the Telegram bot."""
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    log.info("═" * 40)
    log.info("HERMES TELEGRAM BOT — Starting")
    log.info("═" * 40)

    # Acquire single-instance lock
    lock_fd = acquire_lock()
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))

    # Load credentials
    try:
        bot_token = get_env("TELEGRAM_BOT_TOKEN", required=True)
        chat_id = get_env("TELEGRAM_CHAT_ID", required=False)
    except RuntimeError as e:
        log.error("Missing configuration: %s", e)
        print(f"错误: {e}")
        release_lock(lock_fd)
        sys.exit(1)

    if chat_id:
        log.info("Restricted to chat_id: %s", chat_id)
    else:
        log.warning("No TELEGRAM_CHAT_ID set — accepting all messages (dev mode)")

    # Build application
    app = ApplicationBuilder().token(bot_token).build()

    # Register command handlers
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("zone", cmd_zone))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("dalao", cmd_dalao))
    app.add_handler(CommandHandler("dot", cmd_dot))
    app.add_handler(CommandHandler("swing", cmd_swing))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))

    # Error handler
    app.add_error_handler(error_handler)

    # Graceful shutdown
    def _shutdown(signum, frame):
        log.info("Received signal %s, shutting down…", signum)
        release_lock(lock_fd)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Bot started, waiting for messages…")
    print("Hermes Telegram Bot 已启动，等待消息…")

    # Start polling
    app.run_polling(
        allowed_updates=["message"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
