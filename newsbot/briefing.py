"""Briefing composition and the scheduled jobs."""

from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Dict, List, Optional, Tuple

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


def _pending_keys(section: str, selections: List[Selection]) -> List[Tuple[str, str]]:
    """Every key for shown headlines and the duplicates collapsed into them."""
    pending: List[Tuple[str, str]] = []
    for selection in selections:
        for candidate in [selection.candidate] + list(selection.duplicates):
            for key in _headline_keys(candidate.title, candidate.url):
                pending.append((section, key))
    return pending


def compose_briefing(now: Optional[datetime] = None) -> Briefing:
    now = now or datetime.now()
    norway_text, norway_selections = news_feeds.get_norwegian_morning_news()
    global_text, global_selections = news_feeds.get_global_news()
    business_text, business_selections = market_data.get_business_and_stocks()
    trade_text, trade_outcome, _labels = screener.build_trade_candidates_payload()

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
        message=message, pending=pending, titles=titles,
        picks=trade_outcome.qualified[:5],
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


def job_daily_briefing(force_hour: Optional[int] = None) -> bool:
    run_time = datetime.now()
    if force_hour is not None:
        run_time = run_time.replace(hour=force_hour, minute=0, second=0, microsecond=0)
    APP_LOG.info("briefing_start at=%s", run_time.isoformat())
    briefing = compose_briefing(run_time)
    success = messaging._broadcast_message(briefing.message)
    if success:
        _commit_briefing(briefing)
    else:
        APP_LOG.warning("briefing_send_failed headlines_not_marked=%d", len(briefing.pending))
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
    briefing = compose_briefing(now)
    if messaging._broadcast_message(briefing.message, chat_id=chat_id):
        _commit_briefing(briefing)
