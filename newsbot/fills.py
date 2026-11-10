"""Real trades the owner placed at the broker, typed in with /fill.

Everything is stored in NOK at the day's exchange rate, and the rate itself is
kept, so the broker's currency fee can be subtracted later (#86).
"""

import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from newsbot import market_data
from newsbot.history_store import TRADE_HISTORY
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import TRADE_HISTORY_LOCK

USAGE = (
    "Usage: /fill BUY|SELL TICKER SHARES PRICE FEE_NOK [YYYY-MM-DD] [!]\n"
    "Example: /fill BUY EQNR.OL 40 285.40 59\n"
    "PRICE is in the stock's own currency. Add ! at the end to skip the checks."
)

# Ticker suffix -> trading currency. No suffix means a US listing.
_SUFFIX_CURRENCY = {
    "OL": "NOK", "ST": "SEK", "CO": "DKK",
    "DE": "EUR", "F": "EUR", "PA": "EUR", "AS": "EUR", "MI": "EUR", "MC": "EUR",
    "HE": "EUR", "BR": "EUR", "LS": "EUR", "VI": "EUR", "IR": "EUR",
}

# A price this far from the day's close is more likely a typo than a fill.
MAX_PRICE_GAP_PCT = 15.0

_TABLE = """
    CREATE TABLE IF NOT EXISTS real_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        side TEXT NOT NULL,
        symbol TEXT NOT NULL,
        shares INTEGER NOT NULL,
        price REAL NOT NULL,
        currency TEXT NOT NULL,
        fx_to_nok REAL NOT NULL,
        fee_nok REAL NOT NULL,
        trade_date TEXT NOT NULL,
        checks_skipped INTEGER NOT NULL,
        recorded_at TEXT NOT NULL
    )
"""


def _connection() -> sqlite3.Connection:
    """The bot's own database connection; callers hold TRADE_HISTORY_LOCK."""
    conn = TRADE_HISTORY._connection()
    conn.execute(_TABLE)
    return conn


def currency_for(symbol: str) -> Optional[str]:
    if "." not in symbol:
        return "USD"
    return _SUFFIX_CURRENCY.get(symbol.rsplit(".", 1)[1])


def _close_on_or_before(symbol: str, day: date) -> Optional[float]:
    """Last daily close up to and including `day`, or None if Yahoo has none."""
    history = market_data._fetch_ticker_history(symbol, period="1y")
    if history is None or len(history) == 0:
        return None
    closes = history["Close"]
    closes = closes[[stamp.date() <= day for stamp in closes.index]].dropna()
    if len(closes) == 0:
        return None
    value = float(closes.iloc[-1])
    return value if math.isfinite(value) and value > 0 else None


def _parse(argument: str) -> Tuple[Dict[str, Any], bool]:
    """Turn the typed words into a trade, or raise ValueError with a plain reason."""
    words = argument.split()
    forced = bool(words) and words[-1] == "!"
    if forced:
        words = words[:-1]
    if len(words) not in (5, 6):
        raise ValueError(USAGE)
    side, symbol = words[0].upper(), words[1].upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("The first word must be BUY or SELL.")
    try:
        shares = int(words[2])
        price = float(words[3].replace(",", "."))
        fee = float(words[4].replace(",", "."))
    except ValueError:
        raise ValueError("SHARES must be a whole number, PRICE and FEE_NOK numbers.") from None
    if shares <= 0 or not math.isfinite(price) or price <= 0 or not math.isfinite(fee) or fee < 0:
        raise ValueError("SHARES and PRICE must be above 0, and FEE_NOK 0 or more.")
    today = date.today()
    trade_day = today
    if len(words) == 6:
        try:
            trade_day = date.fromisoformat(words[5])
        except ValueError:
            raise ValueError("The date must look like 2026-11-10.") from None
        if trade_day > today or trade_day < today - timedelta(days=300):
            raise ValueError("The date must be today or within the last 300 days.")
    trade = {"side": side, "symbol": symbol, "shares": shares, "price": price,
             "fee_nok": fee, "trade_date": trade_day.isoformat()}
    return trade, forced


def _shares_held(conn: sqlite3.Connection, symbol: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE side WHEN 'BUY' THEN shares ELSE -shares END), 0) "
        "FROM real_trades WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return int(row[0])


def record_fill(argument: str) -> str:
    """Check and save one trade. Returns the reply text, saved or refused."""
    try:
        trade, forced = _parse(argument)
    except ValueError as exc:
        return escape(str(exc))
    symbol, trade_day = trade["symbol"], date.fromisoformat(trade["trade_date"])
    currency = currency_for(symbol)
    if currency is None:
        return (f"Not saved: I don't know which currency {escape(symbol)} trades in. "
                "Oslo, Stockholm, Copenhagen, euro markets and US stocks are supported.")
    fx = 1.0
    if currency != "NOK":
        fx = _close_on_or_before(f"{currency}NOK=X", trade_day)
        if fx is None:
            return f"Not saved: no {currency}/NOK exchange rate found. Try again later."
    if not forced:
        close = _close_on_or_before(symbol, trade_day)
        if close is None:
            return (f"Not saved: Yahoo has no prices for {escape(symbol)}. Check the ticker "
                    "(Oslo stocks end in .OL). Add ! at the end to save it anyway.")
        gap = (trade["price"] / close - 1.0) * 100.0
        if abs(gap) > MAX_PRICE_GAP_PCT:
            return (f"Not saved: {trade['price']:g} is {gap:+.0f}% away from the close of "
                    f"{close:.2f} {currency}. Check the price. Add ! at the end to save it anyway.")
    with TRADE_HISTORY_LOCK:
        conn = _connection()
        if not forced and trade["side"] == "SELL":
            held = _shares_held(conn, symbol)
            if trade["shares"] > held:
                return (f"Not saved: you sell {trade['shares']} {escape(symbol)} but the log "
                        f"shows {held} held. Add ! at the end to save it anyway.")
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO real_trades (side, symbol, shares, price, currency, fx_to_nok,
                    fee_nok, trade_date, checks_skipped, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trade["side"], symbol, trade["shares"], trade["price"], currency, fx,
                 trade["fee_nok"], trade["trade_date"], int(forced),
                 datetime.now(timezone.utc).isoformat()),
            )
        trade_id = cursor.lastrowid
    APP_LOG.info("real_trade_saved id=%s side=%s symbol=%s forced=%s",
                 trade_id, trade["side"], symbol, forced)
    saved = dict(trade, id=trade_id, currency=currency, fx_to_nok=fx)
    return "Saved:\n" + _describe(saved)


def _describe(trade: Dict[str, Any]) -> str:
    value_nok = trade["shares"] * trade["price"] * trade["fx_to_nok"]
    rate = (f" (1 {trade['currency']} = {trade['fx_to_nok']:.4f} NOK)"
            if trade["currency"] != "NOK" else "")
    return (
        f"#{trade['id']} {trade['trade_date']} {trade['side']} {trade['shares']} "
        f"{escape(trade['symbol'])} at {trade['price']:.2f} {trade['currency']}{rate}\n"
        f"    worth {value_nok:,.0f} NOK, fee {trade['fee_nok']:,.0f} NOK"
    )


def list_fills(limit: int = 20) -> str:
    with TRADE_HISTORY_LOCK:
        cursor = _connection().execute(
            "SELECT * FROM real_trades ORDER BY id DESC LIMIT ?", (limit,)
        )
        columns = [column[0] for column in cursor.description]
        rows: List[Dict[str, Any]] = [dict(zip(columns, row, strict=True)) for row in cursor]
    if not rows:
        return "No real trades saved yet. Add one with /fill."
    return f"<b>Last {len(rows)} real trades</b> (newest first)\n" + "\n".join(
        _describe(row) for row in rows
    )


def delete_last_fill() -> str:
    with TRADE_HISTORY_LOCK:
        conn = _connection()
        cursor = conn.execute("SELECT * FROM real_trades ORDER BY id DESC LIMIT 1")
        columns = [column[0] for column in cursor.description]
        row = cursor.fetchone()
        if row is None:
            return "Nothing to delete: no real trades saved."
        last = dict(zip(columns, row, strict=True))
        with conn:
            conn.execute("DELETE FROM real_trades WHERE id = ?", (last["id"],))
    SYS_LOG.warning("real_trade_deleted id=%s symbol=%s", last["id"], last["symbol"])
    return "Deleted:\n" + _describe(last)
