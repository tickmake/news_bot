"""Our own copy of daily prices for every stock in the stock list (#75).

Yahoo drops the history of stocks that leave the market, so a test that
reads Yahoo later quietly forgets the losers. Keeping every bar ourselves,
forever, stops that. The first time a stock is seen, about 2 years are
loaded; after that each night adds the new days.

A bar, once saved, is never changed: if Yahoo later revises it, the saved
row still shows what was known at the time (`fetched_at`). Prices are raw,
with `adj_close` beside them for splits and dividends.
"""

import math
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

from newsbot import stock_list
from newsbot.history_store import TRADE_HISTORY
from newsbot.indicators import _drop_unfinished_bar
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import TRADE_HISTORY_LOCK
from newsbot.util import _with_retry

FIRST_LOAD_PERIOD = "2y"
CATCH_UP_PERIOD = "1mo"

_TABLE = """
    CREATE TABLE IF NOT EXISTS daily_prices (
        symbol TEXT NOT NULL,
        session_date TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL NOT NULL,
        adj_close REAL,
        volume REAL,
        currency TEXT,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (symbol, session_date)
    )
"""


def _connection():
    """The bot's database connection; callers hold TRADE_HISTORY_LOCK."""
    conn = TRADE_HISTORY._connection()
    conn.execute(_TABLE)
    return conn


def latest_dates() -> Dict[str, str]:
    """The newest saved session per symbol."""
    with TRADE_HISTORY_LOCK:
        rows = _connection().execute(
            "SELECT symbol, MAX(session_date) FROM daily_prices GROUP BY symbol").fetchall()
    return dict(rows)


def _number(value: Any) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _fetch(symbol: str, period: str) -> Tuple[Any, Optional[str]]:
    ticker = yf.Ticker(symbol)
    history = _with_retry(
        lambda: ticker.history(period=period, interval="1d", auto_adjust=False, actions=False),
        f"yf_prices_{symbol}", retries=2,
    )
    currency = (getattr(ticker, "history_metadata", None) or {}).get("currency")
    return _drop_unfinished_bar(history), currency


def save_prices(symbol: str, history: Any, currency: Optional[str]) -> int:
    """Save new bars for one symbol. Returns how many were new."""
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for stamp, bar in history.iterrows():
        close = _number(bar.get("Close"))
        if close is None or close <= 0:
            continue
        rows.append((
            symbol, stamp.strftime("%Y-%m-%d"), _number(bar.get("Open")), _number(bar.get("High")),
            _number(bar.get("Low")), close, _number(bar.get("Adj Close")), _number(bar.get("Volume")),
            currency, fetched_at,
        ))
    with TRADE_HISTORY_LOCK:
        conn = _connection()
        before = conn.total_changes
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return conn.total_changes - before


def closes(symbol: str) -> Optional[Any]:
    """Saved closes for one symbol by date, dividend-adjusted where Yahoo gave it; None if none."""
    with TRADE_HISTORY_LOCK:
        rows = _connection().execute(
            "SELECT session_date, COALESCE(adj_close, close) FROM daily_prices"
            " WHERE symbol = ? ORDER BY session_date", (symbol,)).fetchall()
    if not rows:
        return None
    return pd.Series([value for _, value in rows],
                     index=[date.fromisoformat(day) for day, _ in rows], dtype=float)


def load_prices(symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    """Load every listed stock. Returns a summary the data check (#77) reads."""
    symbols = symbols if symbols is not None else stock_list.tickers()
    known = latest_dates()
    summary: Dict[str, Any] = {"new_rows": 0, "failed": [], "no_new_price": []}
    for symbol in symbols:
        try:
            history, currency = _fetch(symbol, CATCH_UP_PERIOD if symbol in known else FIRST_LOAD_PERIOD)
            added = save_prices(symbol, history, currency)
        except Exception as exc:
            SYS_LOG.warning("price_load_failed symbol=%s detail=%s", symbol, exc)
            summary["failed"].append(symbol)
            continue
        summary["new_rows"] += added
        if added == 0:
            summary["no_new_price"].append(symbol)
    return summary


def job_load_prices() -> Dict[str, Any]:
    """Nightly wrapper. Never raises; returns the summary (empty on a crash)."""
    try:
        summary = load_prices()
    except Exception as exc:
        SYS_LOG.error("price_load_crashed detail=%s", exc)
        return {}
    APP_LOG.info(
        "price_load new_rows=%d failed=%d no_new_price=%d",
        summary["new_rows"], len(summary["failed"]), len(summary["no_new_price"]),
    )
    return summary
