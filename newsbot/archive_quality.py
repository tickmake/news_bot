"""Data-quality class of each forward-test signal (docs/audit/forward-test-fix-plan.md, 3.3).

A signal exists from the moment it was recorded, not from the close it is
dated. Signals recorded before this was enforced were scored from the first
session after the signal date, which could be an open that had already
happened. Every archive row is therefore classified:

  clean                      the entry open came strictly after `recorded_at`
  potentially_contaminated   the entry open came at or before it (old rows only)
  invalid                    a permanent defect; the row can never be evaluated

Classes are written once per classifier version and never changed, so only
permanent facts may be written. A fault that may clear by itself (no bar yet,
a failed fetch, history without a timezone) writes nothing: the row stays
unclassified, is shown as "awaiting entry", and is tried again.
"""

import json
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from newsbot.exchange_hours import exchange_for, open_at

# Bump when the rules below change. Rows are written under this version beside
# the older ones; the view keeps reading the active version until
# `TradeHistoryStore.activate_classifier` moves the pointer.
CLASSIFIER_VERSION = 1

CLEAN = "clean"
POTENTIALLY_CONTAMINATED = "potentially_contaminated"
INVALID = "invalid"

# An unclassified row older than this is reported as overdue.
OVERDUE_AFTER_DAYS = 7


def parse_recorded_at(value: Any) -> datetime:
    """`recorded_at` as an aware datetime. ValueError when it cannot be trusted."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("recorded_at is empty")
    try:
        recorded = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"recorded_at is not a timestamp: {value!r}") from exc
    if recorded.tzinfo is None or recorded.utcoffset() is None:
        raise ValueError(f"recorded_at has no UTC offset: {value!r}")
    return recorded


def static_defect(row: Dict[str, Any]) -> Optional[str]:
    """Reason code of a permanent defect, from the stored row alone, or None.

    Needs no price data, so a defective row is classified on the first pass
    and never waits for an entry bar that will not come. The first failing
    check wins.
    """
    try:
        recorded = parse_recorded_at(row.get("recorded_at"))
    except ValueError:
        return "bad_recorded_at"
    exchange = exchange_for(row["symbol"])
    if exchange is None:
        return "unsupported_exchange"
    try:
        signal_day = date.fromisoformat(row["signal_session"])
    except (TypeError, ValueError):
        return "bad_signal_session"
    if recorded.astimezone(exchange.zone).date() <= signal_day:
        return "recorded_before_signal_closed"
    if row.get("outcome_json"):
        try:
            entry_session = json.loads(row["outcome_json"])["entry_session"]
            if date.fromisoformat(entry_session) <= signal_day:
                return "bad_outcome"
        except (KeyError, TypeError, ValueError):
            return "bad_outcome"
    return None


def entry_quality(row: Dict[str, Any], entry_day: date) -> Tuple[str, str, str]:
    """(quality, reason, entry_open_at) for a row that passed `static_defect`.

    `entry_day` is the session the row enters on. For a row recorded under
    the after-recording policy the open is after `recorded_at` by
    construction; anything else means the selection is broken, so the row is
    invalid instead of quietly counted.
    """
    exchange = exchange_for(row["symbol"])
    if exchange is None:  # static_defect rules this out; kept so a misuse cannot pass
        raise ValueError("unsupported exchange")
    opened = open_at(exchange, entry_day)
    stamp = opened.isoformat()
    if opened > parse_recorded_at(row["recorded_at"]):
        return CLEAN, "entry_after_recording", stamp
    if row.get("entry_policy"):
        return INVALID, "entry_not_after_recording", stamp
    return POTENTIALLY_CONTAMINATED, "entry_before_recording", stamp
