"""Score inputs as they were known on a date (#145, Phase 2.1).

Every score in Phase 2 starts here, so a past month is scored only with what
was public at its end: company numbers by `known_from`, and our own saved
prices up to that day. One row per stock per month end goes into
`score_inputs`, which the scores, tests and pages all read.

A row holds the two newest years of numbers known at the month end (a score
compares a year with the one before), the close that day, a month before and
a year before (for momentum), and the exchange rate from the numbers' currency
to the price's (Equinor reports in USD, trades in NOK).
"""

import json
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

import yfinance as yf

from newsbot import company_numbers, price_store, report_card, stock_list
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import TRADE_HISTORY_LOCK

# Ten years of prices are kept (backfilled in November 2026); momentum needs
# a year of them, and annual-report numbers start with the 2020 accounts.
FIRST_MONTH_END = date(2017, 1, 31)

_TABLE = """
    CREATE TABLE IF NOT EXISTS score_inputs (
        symbol TEXT NOT NULL,
        month_end TEXT NOT NULL,
        price_date TEXT,
        close REAL,
        close_1m REAL,
        close_12m REAL,
        price_currency TEXT,
        year_end TEXT,
        numbers_json TEXT,
        previous_year_end TEXT,
        previous_json TEXT,
        numbers_currency TEXT,
        fx REAL,
        built_at TEXT NOT NULL,
        PRIMARY KEY (symbol, month_end)
    )
"""


def _connection():
    """The bot's database connection; callers hold TRADE_HISTORY_LOCK."""
    conn = company_numbers._connection()
    conn.execute(_TABLE)
    return conn


def month_ends(start: date, end: date) -> List[date]:
    """Calendar month ends from `start`'s month up to and including `end`."""
    out = []
    day = date(start.year, start.month, 1)
    while True:
        following = date(day.year + day.month // 12, day.month % 12 + 1, 1)
        last = following - timedelta(days=1)
        if last > end:
            return out
        out.append(last)
        day = following


def _close_on(closes: Any, day: date) -> Optional[tuple]:
    """(date, close) of the last saved session on or before `day`, at most 7 days earlier."""
    if closes is None:
        return None
    known = closes[closes.index <= day]
    if known.empty or (day - known.index[-1]).days > 7:
        return None
    return known.index[-1], float(known.iloc[-1])


def _price_currency(symbol: str) -> Optional[str]:
    with TRADE_HISTORY_LOCK:
        row = price_store._connection().execute(
            "SELECT currency FROM daily_prices WHERE symbol = ? AND currency IS NOT NULL "
            "ORDER BY session_date DESC LIMIT 1", (symbol,)).fetchone()
    return row[0] if row else None


def _fx_closes(pair: str) -> Any:
    """Daily closes of a Yahoo currency pair such as USDNOK=X, by date."""
    history = yf.Ticker(pair).history(period="5y", interval="1d", auto_adjust=False)
    series = history["Close"].dropna()
    series.index = [stamp.date() for stamp in series.index]
    return series


def inputs_for(symbol: str, day: date, closes: Any = None,
               fx_closes: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Everything a score needs for one stock, as known at the end of `day`."""
    closes = price_store.closes(symbol) if closes is None else closes
    year_ends, by_year, numbers_currency = report_card._years(symbol, day)
    price_currency = _price_currency(symbol)
    now = _close_on(closes, day)
    month_ago = _close_on(closes, day - timedelta(days=30))
    year_ago = _close_on(closes, day - timedelta(days=365))

    fx = None
    if numbers_currency and price_currency:
        if numbers_currency == price_currency:
            fx = 1.0
        elif fx_closes is not None:
            try:
                rate = _close_on(fx_closes(f"{numbers_currency}{price_currency}=X"), day)
                fx = rate[1] if rate else None
            except Exception as exc:  # no rate: value scores say "not available"
                SYS_LOG.warning("score_inputs_fx_failed pair=%s%s detail=%s",
                                numbers_currency, price_currency, exc)
    return {
        "symbol": symbol,
        "month_end": day.isoformat(),
        "price_date": now[0].isoformat() if now else None,
        "close": now[1] if now else None,
        "close_1m": month_ago[1] if month_ago else None,
        "close_12m": year_ago[1] if year_ago else None,
        "price_currency": price_currency,
        "year_end": year_ends[0] if year_ends else None,
        "numbers": by_year[year_ends[0]] if year_ends else {},
        "previous_year_end": year_ends[1] if len(year_ends) > 1 else None,
        "previous": by_year[year_ends[1]] if len(year_ends) > 1 else {},
        "numbers_currency": numbers_currency,
        "fx": fx,
    }


def build(symbols: Optional[List[str]] = None, today: Optional[date] = None,
          fx_closes: Callable[[str], Any] = _fx_closes) -> int:
    """Write a row for every finished month end. Returns how many were written.

    Every run rebuilds every month: numbers loaded later (a new stock, a
    filing read late) can still have been public back then, and `known_from`
    is what decides, not when the bot fetched them. A stock's old rows go in
    the same transaction, so a month that no longer has anything known (a
    `known_from` moved later, P1-8) keeps no row from before. ~100 stocks x
    ~25 months is a few seconds.
    """
    today = today or date.today()
    symbols = symbols if symbols is not None else stock_list.tickers()
    wanted = month_ends(FIRST_MONTH_END, today - timedelta(days=1))
    rates: Dict[str, Any] = {}

    def cached_fx(pair: str) -> Any:
        if pair not in rates:
            rates[pair] = fx_closes(pair)
        return rates[pair]

    written = 0
    for symbol in symbols:
        closes = price_store.closes(symbol)
        rows = []
        for day in wanted:
            row = inputs_for(symbol, day, closes, cached_fx)
            if row["close"] is None and not row["numbers"]:
                continue  # nothing known yet
            rows.append(row)
        with TRADE_HISTORY_LOCK:
            conn = _connection()
            with conn:
                conn.execute("DELETE FROM score_inputs WHERE symbol = ?", (symbol,))
                conn.executemany(
                    "INSERT OR REPLACE INTO score_inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(r["symbol"], r["month_end"], r["price_date"], r["close"], r["close_1m"],
                      r["close_12m"], r["price_currency"], r["year_end"], json.dumps(r["numbers"]),
                      r["previous_year_end"], json.dumps(r["previous"]), r["numbers_currency"],
                      r["fx"], today.isoformat()) for r in rows])
        written += len(rows)
    return written


def job_build_score_inputs() -> int:
    """Scheduled wrapper. Never raises."""
    try:
        written = build()
    except Exception as exc:
        SYS_LOG.error("score_inputs_failed detail=%s", exc)
        return 0
    APP_LOG.info("score_inputs written=%d", written)
    return written
