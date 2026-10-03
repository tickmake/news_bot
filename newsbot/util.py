"""Small helpers shared across modules."""

import hashlib
import time
from datetime import datetime
from html import escape
from typing import Any, Callable, List, Optional

from newsbot.logsetup import SYS_LOG


# Statuses worth another attempt: rate limiting and server-side hiccups. Every
# other 4xx is the caller's mistake and will fail identically each time.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_SECONDS = 30.0


def _retry_after_seconds(response: Any) -> Optional[float]:
    """Seconds a server asked us to wait, capped so a hostile value cannot stall a worker."""
    try:
        value = float(response.headers.get("Retry-After", ""))
    except (AttributeError, TypeError, ValueError):
        return None
    if value < 0:
        return None
    return min(value, _MAX_RETRY_AFTER_SECONDS)


def _with_retry(action: Callable[[], Any], label: str, retries: int = 3, backoff_seconds: float = 1.0) -> Any:
    """Run `action`, retrying exceptions and retryable HTTP responses.

    Callers here check `raise_for_status()` themselves *after* this returns, so
    an HTTP 429/5xx never raised inside the loop and was never retried. A
    retryable response is now retried too (honouring Retry-After); when the
    attempts run out it is returned as-is, so the caller's own status handling
    still produces the error. Other 4xx responses are returned immediately.
    """
    attempt = 0
    while True:
        try:
            result = action()
        except Exception as exc:
            attempt += 1
            if attempt >= retries:
                SYS_LOG.error("retry_exhausted op=%s attempts=%s detail=%s", label, attempt, exc)
                raise
            sleep_for = backoff_seconds * (2 ** (attempt - 1))
            SYS_LOG.warning("retry op=%s attempt=%s sleep=%s detail=%s", label, attempt, sleep_for, exc)
            time.sleep(sleep_for)
            continue
        status = getattr(result, "status_code", None)
        if not isinstance(status, int) or status not in _RETRYABLE_STATUSES:
            return result
        attempt += 1
        if attempt >= retries:
            SYS_LOG.error("retry_exhausted op=%s attempts=%s status=%s", label, attempt, status)
            return result
        sleep_for = _retry_after_seconds(result)
        if sleep_for is None:
            sleep_for = backoff_seconds * (2 ** (attempt - 1))
        SYS_LOG.warning("retry op=%s attempt=%s sleep=%s status=%s", label, attempt, sleep_for, status)
        time.sleep(sleep_for)


def _daily_pick(options: List[str], context: str, date_key: Optional[str] = None) -> str:
    if not options:
        return ""
    if not date_key:
        date_key = datetime.today().strftime("%Y-%m-%d")
    digest = hashlib.sha256(f"{date_key}:{context}".encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(options)
    return options[index]


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return text[: max_len - 1] + "…"


def _render_pre_table(title: str, headers: List[str], rows: List[List[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    divider = "-+-".join("-" * width for width in widths)
    header_row = " | ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers))
    body_rows = [" | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)) for row in rows]
    table_text = "\n".join([header_row, divider, *body_rows])
    return f"{escape(title)}\n<pre>{escape(table_text)}</pre>"
