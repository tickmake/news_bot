"""Telegram command routing, polling and liveness heartbeats."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import escape
from typing import Dict, List, Optional, Tuple

import requests.exceptions

from newsbot import backtest_reports, charts, history_store, market_data, messaging, news_feeds, reports, screener
from newsbot.briefing import _send_briefing
from newsbot.config import SETTINGS
from newsbot.history_store import _record_published_picks
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.reports import build_health_report
from newsbot.state import STATE
from newsbot.util import _with_retry


def _command_help_text() -> str:
    return (
        "Supported commands:\n"
        "/now - Send full briefing now\n"
        "/news - Send only news headlines (no live quotes or trade candidates)\n"
        "/recommendations - Send the trade screener's current picks, with charts for the top few\n"
        "/stocks - Send this week's top picks, ranked by qualifying consistency\n"
        "/watchlist - Send every configured watchlist symbol and its verdict\n"
        "/analyze TICKER - Send a deep-dive report for one symbol (e.g. /analyze AAPL)\n"
        "/performance - Walk-forward test of trade rules vs. market benchmarks\n"
        "/diagnostics - Historical signal slices and same-date ranking comparison\n"
        "/pickperformance - Fixed-session outcomes of actually delivered picks\n"
        "/health - Send bot health report\n"
    )


def _handle_command(command: str, chat_id: str) -> None:
    parts = command.strip().split(maxsplit=1)
    normalized = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    if normalized == "/now":
        _send_briefing(datetime.now(), chat_id)
    elif normalized == "/news":
        norway_text, _norway_selections = news_feeds.get_norwegian_morning_news()
        global_text, _global_selections = news_feeds.get_global_news()
        business_text, _business_selections = market_data._business_news_section()
        payload = (norway_text + global_text + business_text).strip()
        messaging._broadcast_message(payload, chat_id=chat_id)
    elif normalized == "/recommendations":
        text, outcome, labels = screener.build_trade_candidates_payload()
        if messaging._broadcast_message(text, chat_id=chat_id):
            _record_published_picks(outcome.qualified[:5])
        # After the text, never instead of it: the charts decorate the
        # numbers, and Slack cannot receive them at all.
        charts.send_candidate_charts(outcome, labels, chat_id=chat_id)
    elif normalized == "/stocks":
        messaging._broadcast_message(reports.build_weekly_top_picks(), chat_id=chat_id)
    elif normalized == "/watchlist":
        messaging._broadcast_message(screener.build_watchlist_report(), chat_id=chat_id)
    elif normalized == "/analyze":
        if not argument:
            messaging._broadcast_message("Usage: /analyze TICKER (e.g. /analyze AAPL)", chat_id=chat_id)
        else:
            messaging._broadcast_message(reports.build_ticker_analysis(argument), chat_id=chat_id)
    elif normalized == "/performance":
        messaging._broadcast_message(backtest_reports.build_trade_performance_report(), chat_id=chat_id)
    elif normalized == "/pickperformance":
        messaging._broadcast_message(history_store.build_published_pick_report(), chat_id=chat_id)
    elif normalized == "/diagnostics":
        messaging._broadcast_message(backtest_reports.build_trade_diagnostics_report(), chat_id=chat_id)
    elif normalized == "/health":
        messaging._broadcast_message(build_health_report(), chat_id=chat_id)
    else:
        messaging._broadcast_message(_command_help_text(), chat_id=chat_id)


def _authorized_chat_ids() -> set:
    """Chats allowed to issue commands: the configured chat and health chat.

    Anyone can message a bot, and every command spends market-data quota and
    mirrors its reply to Slack, so unknown senders are ignored.
    """
    allowed = {SETTINGS.telegram_chat_id, SETTINGS.health_ping_chat_id}
    return {str(chat) for chat in allowed if chat}


# Commands run off the poller thread so a multi-minute /performance cannot
# starve /health and the rest. Two workers, and one in-flight run per command.
_COMMAND_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="command")
_COMMANDS_IN_FLIGHT: set = set()
_COMMANDS_LOCK = threading.Lock()


def _save_inflight_locked() -> None:
    """Persist the commands now running, so a restart can say what it interrupted.

    The Telegram offset moves as soon as a command is dispatched (otherwise a
    crash would redeliver it for ever), so a restart mid-command would drop it
    without a word. Callers hold _COMMANDS_LOCK.
    """
    STATE.data["inflight_commands"] = [list(item) for item in sorted(_COMMANDS_IN_FLIGHT)]
    STATE.save()


def notify_interrupted_commands() -> int:
    """At startup, tell each chat which command a restart cut short. Returns the count."""
    with _COMMANDS_LOCK:
        leftover = STATE.data.get("inflight_commands") or []
        if not leftover:
            return 0
        STATE.data["inflight_commands"] = []
        STATE.save()
    sent = 0
    for item in leftover:
        try:
            chat_id, command = str(item[0]), str(item[1])
        except (IndexError, TypeError):
            continue
        SYS_LOG.warning("command_interrupted cmd=%s chat_id=%s", command.split()[0] if command else "", chat_id)
        if messaging.send_telegram_message(
            f"The bot restarted while running <code>{escape(command)}</code>. "
            "Send it again if you still want it.",
            chat_id=chat_id,
        ):
            sent += 1
    return sent


def _run_command_safely(command: str, chat_id: str, key: str, flight: Tuple[str, str]) -> None:
    try:
        _handle_command(command, chat_id)
    except Exception as exc:
        APP_LOG.error("command_failed cmd=%s detail=%s", key, exc)
        try:
            messaging.send_telegram_message(f"Sorry, {key} failed. Check the bot logs.", chat_id=chat_id)
        except Exception:
            pass
    finally:
        with _COMMANDS_LOCK:
            _COMMANDS_IN_FLIGHT.discard(flight)
            _save_inflight_locked()


def _dispatch_command(command: str, chat_id: str) -> None:
    parts = command.strip().split(maxsplit=1)
    key = parts[0].lower() if parts else ""
    # /analyze differs by argument, so only identical invocations collide.
    flight = (chat_id, command.strip().lower())
    with _COMMANDS_LOCK:
        if flight in _COMMANDS_IN_FLIGHT:
            busy = True
        else:
            _COMMANDS_IN_FLIGHT.add(flight)
            _save_inflight_locked()
            busy = False
    if busy:
        messaging.send_telegram_message(f"{key} is already running; wait for it to finish.", chat_id=chat_id)
        return
    _COMMAND_POOL.submit(_run_command_safely, command, chat_id, key, flight)


def poll_telegram_commands(long_poll_timeout: int = 0) -> bool:
    if not SETTINGS.command_poll_enabled or not SETTINGS.telegram_token:
        return True
    try:
        long_poll_timeout = max(0, long_poll_timeout)
        params = {"timeout": long_poll_timeout, "limit": 20}
        if STATE.telegram_update_offset > 0:
            params["offset"] = STATE.telegram_update_offset + 1

        # The HTTP read timeout must outlast the server-side long-poll hold.
        request_timeout = SETTINGS.request_timeout_seconds + long_poll_timeout

        def _fetch() -> requests.Response:
            return requests.get(
                f"https://api.telegram.org/bot{SETTINGS.telegram_token}/getUpdates",
                params=params,
                timeout=request_timeout,
            )

        response = _with_retry(_fetch, "telegram_get_updates")
        response.raise_for_status()
        payload = response.json()
        updates = payload.get("result", [])
        for update in updates:
            # Per-update isolation: one bad update must not abort the batch
            # before the offset moves, or Telegram redelivers it forever.
            update_id = 0
            try:
                update_id = int(update.get("update_id", 0))
                message = update.get("message") or update.get("edited_message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                text = message.get("text") or ""
                if text.startswith("/"):
                    if chat_id not in _authorized_chat_ids():
                        SYS_LOG.warning(
                            "command_rejected reason=unauthorized_chat chat_id=%s cmd=%s",
                            chat_id,
                            text.split()[0],
                        )
                    else:
                        APP_LOG.info(
                            "command_received cmd=%s chat_id=%s", text.split()[0], chat_id
                        )
                        _dispatch_command(text, chat_id)
            except Exception as exc:
                SYS_LOG.error("command_update_failed detail=%s", exc)
            if update_id > STATE.telegram_update_offset:
                STATE.telegram_update_offset = update_id
                STATE.save()
        return True
    except Exception as exc:
        SYS_LOG.error("command_poll_failed detail=%s", exc)
        return False


# How old a heartbeat may get before the container reports unhealthy. The
# scheduler allowance is generous because a briefing holds a scheduler worker
# for minutes (the LLM ranker alone can take 240s); the poller beats every
# long-poll cycle, so a short allowance still tolerates retries.
HEARTBEAT_MAX_AGE_SECONDS: Dict[str, int] = {"scheduler": 900, "poller": 300}


def _heartbeat_path(name: str) -> str:
    directory = os.path.dirname(os.path.abspath(SETTINGS.state_file)) or "."
    return os.path.join(directory, f".news_bot_heartbeat_{name}")


def _touch_heartbeat(name: str) -> None:
    """Record that a loop is alive. Never raises: a full disk must not kill it."""
    try:
        with open(_heartbeat_path(name), "w", encoding="utf-8") as file_obj:
            file_obj.write(str(time.time()))
    except OSError as exc:
        SYS_LOG.debug("heartbeat_write_failed name=%s detail=%s", name, exc)


def heartbeat_problems(now: Optional[float] = None) -> List[str]:
    """Reasons the bot looks hung or dead; empty means healthy.

    A running process is not enough: a poller thread that died, or a scheduler
    wedged behind a stuck job, leaves the container "up" while doing nothing.
    """
    current = time.time() if now is None else now
    required = ["scheduler"]
    if SETTINGS.command_poll_enabled and SETTINGS.telegram_token:
        required.append("poller")
    problems: List[str] = []
    for name in required:
        try:
            age = current - os.path.getmtime(_heartbeat_path(name))
        except OSError:
            problems.append(f"{name}: no heartbeat")
            continue
        if age > HEARTBEAT_MAX_AGE_SECONDS[name]:
            problems.append(f"{name}: last beat {age:.0f}s ago")
    return problems


def run_healthcheck() -> int:
    """Exit status for `python news_bot.py --healthcheck` (Docker HEALTHCHECK)."""
    problems = heartbeat_problems()
    if problems:
        print("unhealthy: " + "; ".join(problems))
        return 1
    print("healthy")
    return 0


def run_command_long_polling_loop(stop_event: Optional[threading.Event] = None) -> None:
    """Continuously long-poll Telegram for commands until stopped.

    Long-polling responds to commands near-instantly (instead of waiting for a
    fixed interval) and issues far fewer requests when idle.
    """
    timeout = max(1, SETTINGS.command_long_poll_timeout_seconds)
    while stop_event is None or not stop_event.is_set():
        ok = poll_telegram_commands(long_poll_timeout=timeout)
        if ok:
            # Only a successful poll counts: an unreachable Telegram for
            # minutes should show as unhealthy, not hide behind a live thread.
            _touch_heartbeat("poller")
        if not ok:
            # Back off briefly on failure so we do not hammer the API.
            if stop_event is not None:
                stop_event.wait(5)
            else:
                time.sleep(5)


def _prepare_telegram_long_polling() -> None:
    if not SETTINGS.telegram_token:
        return
    try:
        webhook_info = _with_retry(
            lambda: requests.get(
                f"https://api.telegram.org/bot{SETTINGS.telegram_token}/getWebhookInfo",
                timeout=SETTINGS.request_timeout_seconds,
            ),
            "telegram_get_webhook_info",
            retries=2,
        )
        webhook_info.raise_for_status()
        payload = webhook_info.json()
        webhook_url = ((payload.get("result") or {}).get("url") or "").strip()
        if webhook_url:
            SYS_LOG.warning("telegram_webhook_detected removing_for_long_polling")
            _with_retry(
                lambda: requests.post(
                    f"https://api.telegram.org/bot{SETTINGS.telegram_token}/deleteWebhook",
                    json={"drop_pending_updates": False},
                    timeout=SETTINGS.request_timeout_seconds,
                ),
                "telegram_delete_webhook",
                retries=2,
            )
    except Exception as exc:
        SYS_LOG.warning("telegram_webhook_check_failed detail=%s", exc)
