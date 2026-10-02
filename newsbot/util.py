"""Small helpers shared across modules."""

import hashlib
import time
from datetime import datetime
from html import escape
from typing import Any, Callable, List, Optional

from newsbot.logsetup import SYS_LOG


def _with_retry(action: Callable[[], Any], label: str, retries: int = 3, backoff_seconds: float = 1.0) -> Any:
    attempt = 0
    while True:
        try:
            return action()
        except Exception as exc:
            attempt += 1
            if attempt >= retries:
                SYS_LOG.error("retry_exhausted op=%s attempts=%s detail=%s", label, attempt, exc)
                raise
            sleep_for = backoff_seconds * (2 ** (attempt - 1))
            SYS_LOG.warning("retry op=%s attempt=%s sleep=%s detail=%s", label, attempt, sleep_for, exc)
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
