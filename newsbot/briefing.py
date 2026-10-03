"""Briefing composition and the scheduled jobs."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from newsbot import market_data, messaging, news_feeds, screener
from newsbot.config import EVENING_GREETINGS, INSPIRATIONAL_QUOTES, MORNING_GREETINGS, SETTINGS
from newsbot.history_store import _record_published_picks
from newsbot.indicators import TradeMetrics
from newsbot.logsetup import APP_LOG
from newsbot.news_feeds import Selection, _headline_keys
from newsbot.reports import build_health_report
from newsbot.state import STATE
from newsbot.util import _daily_pick


def build_daily_intro(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    date_key = now.strftime("%Y-%m-%d")
    recipient = escape(SETTINGS.recipient_name.strip() or "Sunil")
    if now.hour < 12:
        greeting = _daily_pick(MORNING_GREETINGS, "morning", date_key)
        quote = _daily_pick(INSPIRATIONAL_QUOTES, "quote", date_key)
        return (
            f"{escape(greeting)}\n"
            f"Hello, {recipient}!\n"
            f"📅 {date_key}\n"
            f"💡 Quote of the day: <i>{escape(quote)}</i>\n"
        )
    greeting = _daily_pick(EVENING_GREETINGS, "evening", date_key)
    return f"{escape(greeting)}\nHello, {recipient}!\n📅 {date_key}\n"


@dataclass
class Briefing:
    """A rendered briefing plus the headline keys it is responsible for.

    Keys are marked seen only after a successful send. Marking them at build
    time meant a failed send permanently suppressed a day of news.
    """

    message: str
    pending: List[Tuple[str, str]] = field(default_factory=list)
    titles: List[str] = field(default_factory=list)
    picks: List[TradeMetrics] = field(default_factory=list)
    # Sections that raised or were skipped for time; the briefing still goes out.
    failed_sections: List[str] = field(default_factory=list)


def _pending_keys(section: str, selections: List[Selection]) -> List[Tuple[str, str]]:
    """Every key for shown headlines and the duplicates collapsed into them."""
    pending: List[Tuple[str, str]] = []
    for selection in selections:
        for candidate in [selection.candidate] + list(selection.duplicates):
            for key in _headline_keys(candidate.title, candidate.url):
                pending.append((section, key))
    return pending


# One briefing at a time. The scheduled job and /now both compose, send and then
# commit headline keys; interleaved, each would show the other's headlines as
# new (duplicate sends) and race on the dedup state.
_BRIEFING_LOCK = threading.Lock()
BRIEFING_LOCK_WAIT_SECONDS = 1800


@contextmanager
def _briefing_slot(blocking: bool) -> Iterator[bool]:
    """Yields whether this caller now holds the briefing lock."""
    if blocking:
        acquired = _BRIEFING_LOCK.acquire(timeout=BRIEFING_LOCK_WAIT_SECONDS)
    else:
        acquired = _BRIEFING_LOCK.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            _BRIEFING_LOCK.release()


def _run_section(
    label: str, build: Callable[[], Tuple[str, List[Selection]]],
    deadline: float, failed: List[str],
) -> Tuple[str, List[Selection]]:
    """One news section, isolated: a failure costs that section, not the briefing."""
    if time.monotonic() >= deadline:
        APP_LOG.warning("briefing_section_skipped section=%s reason=out_of_time", label)
        failed.append(label)
        return f"⚠️ {label} skipped: ran out of time.\n\n", []
    try:
        return build()
    except Exception as exc:
        APP_LOG.error("briefing_section_failed section=%s detail=%s", label, exc)
        failed.append(label)
        return f"⚠️ {label} unavailable this time.\n\n", []


def compose_briefing(now: Optional[datetime] = None) -> Briefing:
    now = now or datetime.now()
    deadline = time.monotonic() + max(10, SETTINGS.briefing_budget_seconds)
    failed: List[str] = []

    # The trade screen waits on market-data HTTP while the news sections wait on
    # feeds and the local LLM, so they overlap. News sections stay sequential:
    # the ranker is a single small model and parallel calls would only slow each.
    screen_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="briefing-screen")
    screen_future = screen_pool.submit(screener.build_trade_candidates_payload)
    try:
        norway_text, norway_selections = _run_section(
            "Norway news", news_feeds.get_norwegian_morning_news, deadline, failed)
        global_text, global_selections = _run_section(
            "Global news", news_feeds.get_global_news, deadline, failed)
        business_text, business_selections = _run_section(
            "Business & markets", market_data.get_business_and_stocks, deadline, failed)

        picks: List[TradeMetrics] = []
        try:
            trade_text, trade_outcome, _labels = screen_future.result(
                timeout=max(5.0, deadline - time.monotonic())
            )
            picks = trade_outcome.qualified[:5]
        except FuturesTimeoutError:
            APP_LOG.error("briefing_section_failed section=trade_candidates detail=timeout")
            failed.append("Trade candidates")
            trade_text = "⚠️ Trade candidates skipped: ran out of time.\n\n"
        except Exception as exc:
            APP_LOG.error("briefing_section_failed section=trade_candidates detail=%s", exc)
            failed.append("Trade candidates")
            trade_text = "⚠️ Trade candidates unavailable this time.\n\n"
    finally:
        screen_pool.shutdown(wait=False, cancel_futures=True)

    message = (
        build_daily_intro(now)
        + "\n"
        + norway_text
        + global_text
        + business_text
        + "\n\n"
        + trade_text
    )
    pending = (
        _pending_keys("norway_news", norway_selections)
        + _pending_keys("global_news", global_selections)
        + _pending_keys("business_news", business_selections)
    )
    all_selections = norway_selections + global_selections + business_selections
    titles = [selection.candidate.title for selection in all_selections]
    return Briefing(
        message=message, pending=pending, titles=titles, picks=picks,
        failed_sections=failed,
    )


def _commit_briefing(briefing: Briefing) -> None:
    """Record what was shown. Call only after a confirmed successful send."""
    _record_published_picks(briefing.picks)
    date_key = datetime.today().strftime("%Y-%m-%d")
    by_section: Dict[str, List[str]] = {}
    for section, key in briefing.pending:
        by_section.setdefault(section, []).append(key)
    for section, keys in by_section.items():
        STATE.mark_headline_seen(section, keys, date_key)
    for title in briefing.titles:
        STATE.record_recent_title(title, date_key)


BRIEFING_HOURS = (7, 19)
# How long after a slot a restarted bot will still send the briefing it missed.
CATCHUP_WINDOW_HOURS = 3


def _latest_slot(now: datetime) -> Optional[Tuple[str, datetime]]:
    """The most recent scheduled slot at or before `now` today, as (key, time)."""
    due = [hour for hour in BRIEFING_HOURS if hour <= now.hour]
    if not due:
        return None
    slot_time = now.replace(hour=max(due), minute=0, second=0, microsecond=0)
    return f"{slot_time:%Y-%m-%d}@{slot_time.hour:02d}", slot_time


def missed_briefing_slot(now: Optional[datetime] = None) -> Optional[str]:
    """Slot key of a scheduled briefing that never went out, if one is recent.

    The in-memory APScheduler job store computes the first fire time from the
    moment of startup, so a restart or redeploy at 07:00:30 skips the 07:00
    briefing without any misfire being recorded. Compare against the slot that
    last *succeeded* instead. A state file with no record at all (first start
    after this feature, or a fresh volume) is not treated as a miss, otherwise
    every first boot would send a duplicate.
    """
    now = now or datetime.now()
    latest = _latest_slot(now)
    if latest is None:
        return None
    key, slot_time = latest
    if (now - slot_time).total_seconds() > CATCHUP_WINDOW_HOURS * 3600:
        return None
    recorded = STATE.data.get("last_scheduled_slot")
    if not recorded or recorded == key:
        return None
    return key


def job_scheduled_briefing() -> bool:
    """The cron entry point: a briefing that also records which slot it served."""
    latest = _latest_slot(datetime.now())
    return job_daily_briefing(slot=latest[0] if latest else None)


def job_catchup_briefing() -> bool:
    slot = missed_briefing_slot()
    if slot is None:
        return True
    APP_LOG.warning("briefing_catchup slot=%s", slot)
    return job_daily_briefing(slot=slot)


def job_daily_briefing(force_hour: Optional[int] = None, slot: Optional[str] = None) -> bool:
    run_time = datetime.now()
    if force_hour is not None:
        run_time = run_time.replace(hour=force_hour, minute=0, second=0, microsecond=0)
    with _briefing_slot(blocking=True) as held:
        if not held:
            APP_LOG.error("briefing_lock_timeout waited=%ds", BRIEFING_LOCK_WAIT_SECONDS)
            return False
        APP_LOG.info("briefing_start at=%s", run_time.isoformat())
        try:
            briefing = compose_briefing(run_time)
            success = messaging._broadcast_message(briefing.message)
        except Exception as exc:
            # Nothing may escape into the scheduler: the status must say what
            # happened, and the next slot must still run.
            APP_LOG.error("briefing_failed detail=%s", exc)
            STATE.data["last_run_status"] = "failed"
            STATE.save()
            return False
        if success:
            _commit_briefing(briefing)
            if slot:
                STATE.data["last_scheduled_slot"] = slot
        else:
            APP_LOG.warning("briefing_send_failed headlines_not_marked=%d", len(briefing.pending))
        if success and briefing.failed_sections:
            STATE.data["last_run_status"] = "partial: " + ", ".join(briefing.failed_sections)
        else:
            STATE.data["last_run_status"] = "success" if success else "failed"
        STATE.save()
        return success


def job_health_ping() -> bool:
    if not SETTINGS.health_ping_enabled:
        return True
    date_key = datetime.today().strftime("%Y-%m-%d")
    if STATE.data.get("last_health_ping_date") == date_key:
        return True
    success = messaging._broadcast_message(build_health_report(), chat_id=SETTINGS.health_ping_chat_id or SETTINGS.telegram_chat_id)
    if success:
        STATE.data["last_health_ping_date"] = date_key
        STATE.save()
    return success


def _send_briefing(now: datetime, chat_id: str) -> None:
    with _briefing_slot(blocking=False) as held:
        if not held:
            messaging.send_telegram_message(
                "A briefing is already being prepared; it will arrive shortly.", chat_id=chat_id
            )
            return
        briefing = compose_briefing(now)
        if messaging._broadcast_message(briefing.message, chat_id=chat_id):
            _commit_briefing(briefing)
