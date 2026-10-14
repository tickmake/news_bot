"""Is market data actually arriving? Alert when it is not.

A running process is not a working one: if Yahoo throttles the bot or changes an
endpoint, every screen comes back empty, the container stays "healthy", and the
only symptom is a briefing that quietly says nothing qualified. Each screen
records how many symbols produced usable data and how fresh the newest price is;
a run of bad screens sends one alert, a recovery sends one all-clear.
"""

from datetime import datetime
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from newsbot import messaging
from newsbot.config import SETTINGS
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import STATE, STATE_LOCK

_KEY = "market_data_health"


def assess(
    analysed: int, no_data: int, sessions: Optional[Dict[str, str]], today: datetime
) -> Tuple[bool, List[str]]:
    """(bad, reasons) for one screen. Too small a sample is never bad.

    A run cut short by its deadline has few analysed symbols, so the sample
    floor also keeps a slow-but-working feed from looking like an outage.

    Freshness is judged per market, because taking the newest session across
    the whole universe let a single fresh US symbol stand in for every stale
    one behind it: on 2026-10-13 Oslo, Xetra and Euronext were all missing
    Monday while New York was not, and this check reported one day old. The
    laggard decides, so a market going dark is visible even while others run.
    """
    if analysed < max(1, SETTINGS.market_data_min_sample):
        return False, []
    reasons: List[str] = []
    if no_data / analysed > SETTINGS.market_data_alert_ratio:
        reasons.append(f"{no_data} of {analysed} symbols returned no usable price data")
    for market, session in sorted((sessions or {}).items()):
        try:
            age = (today.date() - datetime.strptime(session, "%Y-%m-%d").date()).days
        except ValueError:
            continue
        if age > SETTINGS.market_data_max_stale_days:
            reasons.append(f"{market}: newest price session is {session} ({age} days old)")
    return bool(reasons), reasons


def _market_of(symbol: str) -> str:
    """Exchange suffix, or "US" for the unsuffixed symbols Yahoo uses there."""
    _, dot, suffix = symbol.rpartition(".")
    return suffix.upper() if dot and suffix else "US"


def _sessions_by_market(outcome: Any) -> Dict[str, str]:
    """Newest session each market produced; symbols in one share a calendar."""
    newest: Dict[str, str] = {}
    rows = list(outcome.qualified) + [metrics for metrics, _failed in outcome.rejected]
    for metrics in rows:
        market = _market_of(metrics.symbol)
        if metrics.session > newest.get(market, ""):
            newest[market] = metrics.session
    return newest


def _newest_session(outcome: Any) -> Optional[str]:
    """The freshest session anywhere, for display rather than for judgement."""
    sessions = _sessions_by_market(outcome).values()
    return max(sessions) if sessions else None


def _alert_chat() -> Optional[str]:
    return SETTINGS.health_ping_chat_id or SETTINGS.telegram_chat_id or None


def record_screen(outcome: Any, now: Optional[datetime] = None) -> None:
    """Record one screen's data health and alert on transitions. Never raises."""
    try:
        _record_screen(outcome, now or datetime.now())
    except Exception as exc:
        SYS_LOG.warning("data_health_record_failed detail=%s", exc)


def _record_screen(outcome: Any, now: datetime) -> None:
    sessions = _sessions_by_market(outcome)
    newest = max(sessions.values()) if sessions else None
    bad, reasons = assess(outcome.analysed, outcome.no_data, sessions, now)
    sampled = outcome.analysed >= max(1, SETTINGS.market_data_min_sample)
    total_outage = sampled and outcome.no_data == outcome.analysed

    with STATE_LOCK:
        previous: Dict[str, Any] = dict(STATE.data.get(_KEY) or {})
        was_alerting = bool(previous.get("alerting"))
        # Too small a sample says nothing: leave the streak and alert state as they were.
        streak = (previous.get("bad_streak", 0) + 1 if bad else 0) if sampled else previous.get("bad_streak", 0)
        alerting = was_alerting
        message: Optional[str] = None
        last_alert = previous.get("alerted_at")

        if bad:
            if not was_alerting and (streak >= SETTINGS.market_data_alert_after or total_outage):
                alerting, message = True, _alert_text(reasons, outcome, newest, reminder=False)
            elif was_alerting and _reminder_due(last_alert, now):
                message = _alert_text(reasons, outcome, newest, reminder=True)
        elif sampled and was_alerting:
            alerting = False
            message = (
                "✅ <b>Market data recovered</b>\n"
                f"{outcome.analysed - outcome.no_data} of {outcome.analysed} symbols returned data "
                f"on the latest screen (newest session {escape(newest or 'n/a')})."
            )

        STATE.data[_KEY] = {
            "at": now.isoformat(timespec="seconds"),
            "analysed": outcome.analysed,
            "no_data": outcome.no_data,
            "newest_session": newest,
            "sessions_by_market": sessions,
            "bad": bad,
            "reasons": reasons,
            "bad_streak": streak,
            "alerting": alerting,
            "alerted_at": now.isoformat(timespec="seconds") if message and alerting else last_alert,
        }
        STATE.save()

    if message and SETTINGS.market_data_alerts_enabled:
        chat_id = _alert_chat()
        APP_LOG.warning("market_data_alert alerting=%s reasons=%s", alerting, "; ".join(reasons))
        if chat_id:
            messaging._broadcast_message(message, chat_id=chat_id)


def _reminder_due(alerted_at: Optional[str], now: datetime) -> bool:
    if not alerted_at:
        return True
    try:
        sent = datetime.fromisoformat(alerted_at)
    except ValueError:
        return True
    return (now - sent).total_seconds() >= SETTINGS.market_data_alert_reminder_hours * 3600


def _alert_text(reasons: List[str], outcome: Any, newest: Optional[str], reminder: bool) -> str:
    title = "⚠️ <b>Market data still failing</b>" if reminder else "⚠️ <b>Market data problem</b>"
    lines = [title]
    lines.extend(f"• {escape(reason)}" for reason in reasons)
    lines.append(
        "Trade screens, /recommendations and /stocks are unreliable until this clears. "
        "Likely causes: Yahoo throttling or an endpoint change; check the bot logs for "
        "<code>yf_history</code> retries."
    )
    return "\n".join(lines)


def describe() -> str:
    """One line for /health."""
    with STATE_LOCK:
        info = dict(STATE.data.get(_KEY) or {})
    if not info:
        return "Market data: no screen run yet"
    usable = info.get("analysed", 0) - info.get("no_data", 0)
    detail = (
        f"{usable}/{info.get('analysed', 0)} symbols with data, "
        f"newest session {info.get('newest_session') or 'n/a'}, last screen {info.get('at', '?')}"
    )
    if info.get("alerting"):
        return f"Market data: DEGRADED — {'; '.join(info.get('reasons') or [])} ({detail})"
    if info.get("bad"):
        return f"Market data: suspect (1 bad screen) — {'; '.join(info.get('reasons') or [])} ({detail})"
    return f"Market data: ok ({detail})"
