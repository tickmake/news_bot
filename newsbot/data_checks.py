"""Check the nightly data loads and warn in Slack when something is off (#77).

Bad data makes bad scores, and so far some problems were silent for weeks.
After each load this looks for: stocks that could not be loaded, stocks with
no new price while the rest of their market moved on, one-day jumps over 40%
(often an unadjusted split), and a stock whose currency changed. One Slack
message lists the new problems; a problem already reported is not repeated.
"""

from datetime import date
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from newsbot import company_numbers, messaging, price_store, stock_list
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import STATE, TRADE_HISTORY_LOCK

JUMP_PCT = 40.0
_MAX_REMEMBERED = 500


def _last_two_bars() -> Dict[str, List[Tuple[str, float, Optional[str]]]]:
    """symbol -> its newest two (date, close, currency) rows, newest first."""
    with TRADE_HISTORY_LOCK:
        rows = price_store._connection().execute(
            """
            SELECT symbol, session_date, close, currency FROM (
                SELECT symbol, session_date, close, currency,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY session_date DESC) AS n
                FROM daily_prices) WHERE n <= 2 ORDER BY symbol, session_date DESC
            """).fetchall()
    bars: Dict[str, List[Tuple[str, float, Optional[str]]]] = {}
    for symbol, day, close, currency in rows:
        bars.setdefault(symbol, []).append((day, close, currency))
    return bars


def price_problems(summary: Dict[str, Any], listed: Optional[List[Dict[str, str]]] = None) -> List[Tuple[str, str]]:
    """(key, text) per problem with the saved prices."""
    listed = listed if listed is not None else stock_list.load()
    expected = {row["ticker"]: row for row in listed}
    bars = _last_two_bars()
    problems: List[Tuple[str, str]] = []
    for symbol in summary.get("failed", []):
        problems.append((f"failed:{symbol}:{STATE.data.get('last_price_load_day', '')}",
                         f"{escape(symbol)}: prices could not be loaded"))
    newest_by_region: Dict[str, str] = {}
    for symbol, row in expected.items():
        if symbol in bars:
            region = row["region"]
            newest_by_region[region] = max(newest_by_region.get(region, ""), bars[symbol][0][0])
    for symbol, row in expected.items():
        history = bars.get(symbol)
        if not history:
            problems.append((f"none:{symbol}", f"{escape(symbol)}: no prices saved at all"))
            continue
        last_day, last_close, last_currency = history[0]
        if last_day < newest_by_region.get(row["region"], last_day):
            problems.append((f"stale:{symbol}:{last_day}",
                             f"{escape(symbol)}: no new price since {last_day}, while others in {row['region']} have one"))
        if len(history) == 2:
            prev_day, prev_close, prev_currency = history[1]
            change = (last_close / prev_close - 1.0) * 100.0 if prev_close else 0.0
            if abs(change) > JUMP_PCT:
                problems.append((f"jump:{symbol}:{last_day}",
                                 f"{escape(symbol)}: price moved {change:+.0f}% in one day "
                                 f"({prev_close:,.2f} → {last_close:,.2f} on {last_day}). Often a split"))
            if prev_currency and last_currency and prev_currency != last_currency:
                problems.append((f"currency:{symbol}:{last_day}",
                                 f"{escape(symbol)}: currency changed from {prev_currency} to {last_currency}"))
        if last_currency and last_currency != row["currency"]:
            problems.append((f"listcurrency:{symbol}:{last_currency}",
                             f"{escape(symbol)}: prices are in {last_currency}, the stock list says {row['currency']}"))
    return problems


def warn(title: str, problems: List[Tuple[str, str]]) -> int:
    """Post the problems not reported before as one Slack message. Returns how many."""
    sent: List[str] = list(STATE.data.get("data_warnings_sent") or [])
    fresh = [(key, text) for key, text in problems if key not in sent]
    if not fresh:
        return 0
    lines = [f"⚠️ <b>{escape(title)}</b>", ""] + [f"• {text}" for _, text in fresh[:40]]
    if len(fresh) > 40:
        lines.append(f"…and {len(fresh) - 40} more. See the bot logs.")
    if not messaging.send_slack_message("\n".join(lines)):
        return 0
    STATE.data["data_warnings_sent"] = (sent + [key for key, _ in fresh])[-_MAX_REMEMBERED:]
    STATE.save()
    return len(fresh)


def job_load_prices_and_check() -> bool:
    """The nightly price load followed by its check. Never raises."""
    summary = price_store.job_load_prices()
    try:
        if not summary:
            warned = warn("Nightly price load crashed", [
                ("crash:prices:" + _today(), "The price load stopped with an error. See the bot logs.")])
        else:
            STATE.data["last_price_load_day"] = _today()
            warned = warn("Nightly price load: problems found", price_problems(summary))
    except Exception as exc:
        SYS_LOG.error("price_check_failed detail=%s", exc)
        return False
    APP_LOG.info("price_check warned=%d", warned)
    return True


def job_load_company_numbers_and_check() -> bool:
    """The weekly company-number load followed by its check. Never raises."""
    summary = company_numbers.job_load_company_numbers()
    try:
        problems = [(f"numbers:{symbol}:{_today()}", f"{escape(symbol)}: company numbers could not be loaded")
                    for symbol in summary.get("failed", [])]
        if not summary:
            problems = [("crash:numbers:" + _today(), "The company-number load stopped with an error. See the bot logs.")]
        warned = warn("Weekly company-number load: problems found", problems)
    except Exception as exc:
        SYS_LOG.error("numbers_check_failed detail=%s", exc)
        return False
    APP_LOG.info("numbers_check warned=%d", warned)
    return True


def _today() -> str:
    return date.today().isoformat()
