"""Where each supported exchange is and when its regular session opens.

The forward test needs one fact per exchange: the instant a session's opening
price is struck, so that a signal is never scored from an open that happened
before the signal was recorded (docs/audit/forward-test-fix-plan.md, 5.2).

Times are the start of regular trading in exchange-local time. An exchange
whose opening auction can print a little after this (Xetra, up to 09:04) is
still safe: the rule only needs this time to be no later than the real open.

A suffix that is not listed is unsupported on purpose. Its signals are
classified invalid instead of being scored against a guessed timetable.
"""

from datetime import date, datetime, time
from typing import Dict, NamedTuple, Optional, Tuple
from zoneinfo import ZoneInfo


class Exchange(NamedTuple):
    zone: ZoneInfo
    opens: time


# Yahoo suffix (without the dot) -> zoneinfo key, regular open. "" is the US.
_BY_SUFFIX: Dict[str, Tuple[str, time]] = {
    "": ("America/New_York", time(9, 30)),
    "OL": ("Europe/Oslo", time(9, 0)),
    "DE": ("Europe/Berlin", time(9, 0)),
    "PA": ("Europe/Paris", time(9, 0)),
    "AS": ("Europe/Amsterdam", time(9, 0)),
    "BR": ("Europe/Brussels", time(9, 0)),
    "MI": ("Europe/Rome", time(9, 0)),
    "MC": ("Europe/Madrid", time(9, 0)),
}


def exchange_for(symbol: str) -> Optional[Exchange]:
    """The exchange a Yahoo symbol trades on, or None when it is not supported."""
    suffix = symbol.rsplit(".", 1)[1].upper() if "." in symbol else ""
    entry = _BY_SUFFIX.get(suffix)
    if entry is None:
        return None
    return Exchange(ZoneInfo(entry[0]), entry[1])


def open_at(exchange: Exchange, day: date) -> datetime:
    """The timezone-aware instant that exchange's session opens on `day`."""
    return datetime.combine(day, exchange.opens, tzinfo=exchange.zone)
