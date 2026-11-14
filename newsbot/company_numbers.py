"""Company numbers from annual reports, with the date they became known (#76).

Source order, per #73:
1. The EU annual-report database (ESEF, filings.xbrl.org). Free, official,
   and has the date each report was added to it (`date_added`), which is
   `known_from`. Only when a filing has no usable `date_added` does the legal
   publishing deadline, 4 months after the year end, stand in (P1-8):

       known_from = date_added if valid and available, else year_end + 120 days

   The deadline never moves a later `date_added` earlier. A report added
   after the deadline may have been public before it was added, but the bot
   only knows it was on file from that day; counting it earlier is look-ahead.
2. Yahoo's yearly financials for what ESEF lacks (German companies, items a
   company tags its own way, US stocks). Yahoo has no publication date, so
   `known_from` is set to 4 months after the year end, a conservative guess.

`known_from` exists so a later test cannot use a number before it was public.
Each ESEF row's filing is kept in `esef_number_filings`, so its date can be
checked against that filing again.
"""

import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import yfinance as yf

from newsbot import stock_list
from newsbot.history_store import TRADE_HISTORY
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import TRADE_HISTORY_LOCK

ITEMS = ("revenue", "operating_profit", "net_profit", "total_debt", "cash", "equity",
         "operating_cash_flow", "shares_outstanding",
         # For the /analyze report card's health checks (#124).
         "total_assets", "current_assets", "current_liabilities", "gross_profit")

# ESEF (IFRS) concept per item, first match wins. Debt and shares are rarely
# tagged the same way twice, so they usually come from Yahoo.
# Profit and equity are the shareholders' own part (#111): the plain totals
# include minority owners of subsidiaries, which made Credit Agricole's profit
# ~25% and Enel's equity ~45% too high for P/E, P/B and ROE. Yahoo's rows are
# already the shareholders' part. A company without minority owners (ASML)
# tags only the total, so that is the fallback.
ESEF_CONCEPTS = {
    "revenue": ("ifrs-full:Revenue", "ifrs-full:RevenueFromContractsWithCustomers"),
    "operating_profit": ("ifrs-full:ProfitLossFromOperatingActivities",),
    "net_profit": ("ifrs-full:ProfitLossAttributableToOwnersOfParent", "ifrs-full:ProfitLoss"),
    "total_debt": ("ifrs-full:Borrowings",),
    "cash": ("ifrs-full:CashAndCashEquivalents",),
    "equity": ("ifrs-full:EquityAttributableToOwnersOfParent", "ifrs-full:Equity"),
    "operating_cash_flow": ("ifrs-full:CashFlowsFromUsedInOperatingActivities",),
    "shares_outstanding": ("ifrs-full:NumberOfSharesOutstanding",),
    "total_assets": ("ifrs-full:Assets",),
    "current_assets": ("ifrs-full:CurrentAssets",),
    "current_liabilities": ("ifrs-full:CurrentLiabilities",),
    "gross_profit": ("ifrs-full:GrossProfit",),
}
YAHOO_ROWS = {
    "revenue": ("income_stmt", "Total Revenue"),
    "operating_profit": ("income_stmt", "Operating Income"),
    "net_profit": ("income_stmt", "Net Income"),
    "total_debt": ("balance_sheet", "Total Debt"),
    "cash": ("balance_sheet", "Cash And Cash Equivalents"),
    "equity": ("balance_sheet", "Stockholders Equity"),
    "operating_cash_flow": ("cashflow", "Operating Cash Flow"),
    "shares_outstanding": ("balance_sheet", "Ordinary Shares Number"),
    "total_assets": ("balance_sheet", "Total Assets"),
    "current_assets": ("balance_sheet", "Current Assets"),
    "current_liabilities": ("balance_sheet", "Current Liabilities"),
    "gross_profit": ("income_stmt", "Gross Profit"),
}
YAHOO_KNOWN_AFTER = timedelta(days=120)
# For an ESEF filing without a usable `date_added` only: EU listed companies
# must publish the annual report within 4 months of the year end (Transparency
# Directive, art. 4). Never used to move a known `date_added` earlier (P1-8).
ESEF_UNDATED_KNOWN_AFTER = timedelta(days=120)
# ESEF filing started with the 2020 accounts; earlier years are not there.
YEARS_WANTED = 7
_HEADERS = {"User-Agent": "news-bot (github.com/tickmake/news_bot)", "Accept": "application/json"}
_TIMEOUT = 60

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS company_numbers (
        symbol TEXT NOT NULL,
        year_end TEXT NOT NULL,
        item TEXT NOT NULL,
        value REAL NOT NULL,
        currency TEXT,
        source TEXT NOT NULL,
        known_from TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (symbol, year_end, item)
    )
    """,
    # ESEF filings already read, so the weekly run only downloads new ones.
    "CREATE TABLE IF NOT EXISTS esef_filings_read (filing_id TEXT PRIMARY KEY, symbol TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS company_numbers_version (version INTEGER NOT NULL)",
    # The filing each ESEF row came from, with its `date_added` as received, so
    # `known_from` can be checked without asking filings.xbrl.org again (P1-8).
    # An ESEF row without one was saved before version 4 and is not yet checked.
    """
    CREATE TABLE IF NOT EXISTS esef_number_filings (
        symbol TEXT NOT NULL,
        year_end TEXT NOT NULL,
        item TEXT NOT NULL,
        filing_id TEXT NOT NULL,
        date_added TEXT,
        PRIMARY KEY (symbol, year_end, item)
    )
    """,
)

# Bump when saved rows were read wrongly, and say in _migrate what is redone.
VERSION = 4


def _migrate(conn) -> None:
    """Redo rows an earlier version read wrongly. Callers hold TRADE_HISTORY_LOCK.

    Version 2 (#111): ESEF profit and equity were the totals including minority
    owners. Those rows go, and every filing is read again on the next run, which
    only adds what is missing. Share counts from Yahoo had a currency by mistake.
    Version 3 capped ESEF `known_from` at the 4-month deadline. That back-dated
    late filings (P1-8), so it no longer runs.
    Version 4 (P1-8): saved ESEF rows are re-dated by the filing they came from.
    That needs the filing list from filings.xbrl.org, so it cannot happen here:
    `esef_number_filings` (created above) starts empty, every saved ESEF row
    counts as unchecked, and `load_esef` dates them on its next run.
    """
    row = conn.execute("SELECT MAX(version) FROM company_numbers_version").fetchone()
    if row[0] is not None and row[0] >= VERSION:
        return
    with conn:
        if row[0] is None or row[0] < 2:
            conn.execute("DELETE FROM company_numbers WHERE source = 'esef' AND item IN ('net_profit', 'equity')")
            conn.execute("DELETE FROM esef_filings_read")
            conn.execute("UPDATE company_numbers SET currency = NULL WHERE item = 'shares_outstanding'")
        conn.execute("DELETE FROM company_numbers_version")
        conn.execute("INSERT INTO company_numbers_version VALUES (?)", (VERSION,))
    APP_LOG.info("company_numbers_migrated version=%d", VERSION)


def _connection():
    conn = TRADE_HISTORY._connection()
    for table in _TABLES:
        conn.execute(table)
    _migrate(conn)
    return conn


# filings.xbrl.org answers "429 Too Many Requests" to a fast loop, so every
# request waits a moment, and a 429 waits longer and tries again.
REQUEST_PAUSE_SECONDS = 1.0
RATE_LIMIT_WAITS = (10, 30, 60)


class NotInEsef(Exception):
    """The company has no ESEF entry; Yahoo is the source for it."""


def _get_json(url: str) -> Any:
    for wait in (*RATE_LIMIT_WAITS, None):
        time.sleep(REQUEST_PAUSE_SECONDS)
        response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if response.status_code == 429 and wait is not None:
            SYS_LOG.info("esef_rate_limited wait=%ss", wait)
            time.sleep(wait)
            continue
        if response.status_code == 404:
            raise NotInEsef(url)
        response.raise_for_status()
        return response.json()
    raise RuntimeError("unreachable")


def lei_for_isin(isin: str) -> Optional[str]:
    data = _get_json("https://api.gleif.org/api/v1/lei-records?" + urllib.parse.urlencode({"filter[isin]": isin}))
    records = data.get("data") or []
    return records[0]["id"] if records else None


def esef_values(doc: Dict[str, Any], period_end: str) -> Tuple[Dict[str, Tuple[float, Optional[str]]], str]:
    """The filing's own-year values per item, with currency, from an xBRL-JSON report.

    Only facts without extra dimensions count (the group total, not a segment),
    and only for the filing's own year, not the comparatives beside it.
    """
    day_after = (date.fromisoformat(period_end) + timedelta(days=1)).isoformat() + "T00:00:00"
    found: Dict[str, Tuple[float, Optional[str]]] = {}
    by_concept: Dict[str, Tuple[float, Optional[str]]] = {}
    for fact in (doc.get("facts") or {}).values():
        dims = fact.get("dimensions") or {}
        if set(dims) - {"concept", "entity", "period", "unit"}:
            continue
        period = str(dims.get("period", ""))
        if period != day_after and not period.endswith("/" + day_after):
            continue
        if "/" in period and (date.fromisoformat(day_after[:10]) - date.fromisoformat(period[:10])).days < 300:
            continue  # a quarter or half year, not the full year
        try:
            value = float(fact["value"])
        except (KeyError, TypeError, ValueError):
            continue
        unit = str(dims.get("unit") or "")
        currency = unit.split(":", 1)[1] if unit.startswith("iso4217:") else None
        by_concept.setdefault(dims.get("concept"), (value, currency))
    for item, concepts in ESEF_CONCEPTS.items():
        for concept in concepts:
            if concept in by_concept:
                found[item] = by_concept[concept]
                break
    return found, period_end


def _save(symbol: str, year_end: str, values: Dict[str, Tuple[float, Optional[str]]],
          source: str, known_from: str, filing: Optional[Tuple[str, Any]] = None) -> int:
    """Insert items not already saved for that year. Returns how many were new.

    `filing` is an ESEF filing's (id, date_added); it is recorded for each row
    this filing supplied, not for rows an earlier filing already saved.
    """
    fetched = datetime.now(timezone.utc).isoformat()
    with TRADE_HISTORY_LOCK:
        conn = _connection()
        saved = 0
        with conn:
            for item, (value, currency) in values.items():
                cursor = conn.execute("INSERT OR IGNORE INTO company_numbers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                      (symbol, year_end, item, value, currency, source, known_from, fetched))
                if cursor.rowcount != 1:
                    continue
                saved += 1
                if filing is not None:
                    conn.execute("INSERT OR REPLACE INTO esef_number_filings VALUES (?, ?, ?, ?, ?)",
                                 (symbol, year_end, item, filing[0], _text(filing[1])))
        return saved


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def esef_known_from(year_end: str, date_added: Any) -> str:
    """The day an ESEF filing's numbers count as known (P1-8).

    `date_added`, the day filings.xbrl.org added the filing, when it is a date
    on or after the year end. Missing, unreadable or earlier than the year end
    (impossible for a real filing): year end + 120 days. Never the earlier of
    the two: that would date a late filing before it was on file.
    """
    end = date.fromisoformat(year_end)
    try:
        added = date.fromisoformat(str(date_added)[:10]) if date_added else None
    except ValueError:
        added = None
    if added is None or added < end:
        added = end + ESEF_UNDATED_KNOWN_AFTER
    return added.isoformat()


MAX_FILING_PAGES = 20


def esef_filings(lei: str) -> List[Dict[str, Any]]:
    """Every filing listed for a company, following the API's pages."""
    url: Optional[str] = f"https://filings.xbrl.org/api/entities/{lei}/filings?page%5Bsize%5D=50"
    filings: List[Dict[str, Any]] = []
    for _ in range(MAX_FILING_PAGES):
        if not url:
            return filings
        data = _get_json(url)
        filings += data.get("data") or []
        url = (data.get("links") or {}).get("next")
        if url and not url.startswith("https://filings.xbrl.org/"):
            raise RuntimeError(f"unexpected next page {url}")
    raise RuntimeError(f"more than {MAX_FILING_PAGES} pages of filings for {lei}")


def _ingestion_order(filings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Oldest added first, so the first version of a year's report wins.

    A filing without `date_added` comes last: it only supplies what the dated
    ones lack, so its fallback date never wins over a real one (P1-8). Checking
    saved rows replays this order.
    """
    def key(item: Dict[str, Any]) -> Tuple[bool, str]:
        added = item["attributes"].get("date_added")
        return not added, str(added or "")
    return sorted(filings, key=key)


def _check_saved_dates(symbol: str, filings: List[Dict[str, Any]]) -> int:
    """Date ESEF rows saved before version 4 by the filing they came from (P1-8).

    The filing is found the way it was chosen when saved: the year's filings
    read for this stock are read again in `_ingestion_order`, and the first
    that has the item supplied it, if its value and currency are the ones
    saved. Anything else is left as it is and stays unchecked, never guessed:
    no filing for the year, a value that does not match, or a read filing that
    is no longer listed or no longer has a report (it may have been the one).
    If the real supplier was read later than this order says, an earlier filing
    with the same number takes the row; that number was on file from its date.
    Only `known_from` changes. Returns how many stay unchecked.
    """
    with TRADE_HISTORY_LOCK:
        conn = _connection()
        rows = conn.execute(
            "SELECT n.year_end, n.item, n.value, n.currency, n.known_from FROM company_numbers n "
            "LEFT JOIN esef_number_filings f USING (symbol, year_end, item) "
            "WHERE n.symbol = ? AND n.source = 'esef' AND f.filing_id IS NULL", (symbol,)).fetchall()
        read = {row[0] for row in conn.execute(
            "SELECT filing_id FROM esef_filings_read WHERE symbol = ?", (symbol,))}
    if not rows:
        return 0
    usable = {filing["id"] for filing in filings
              if filing["attributes"].get("json_url") and filing["attributes"].get("period_end")}
    gone = read - usable
    if gone:
        SYS_LOG.warning("esef_dates_unchecked symbol=%s rows=%d reason=read_filings_gone ids=%s",
                        symbol, len(rows), ",".join(sorted(gone)))
        return len(rows)
    by_year: Dict[str, List[Dict[str, Any]]] = {}
    for filing in _ingestion_order(filings):
        attrs = filing["attributes"]
        if filing["id"] in read and attrs.get("json_url") and attrs.get("period_end"):
            by_year.setdefault(attrs["period_end"], []).append(filing)
    documents: Dict[str, Dict[str, Tuple[float, Optional[str]]]] = {}

    def values_in(filing: Dict[str, Any]) -> Dict[str, Tuple[float, Optional[str]]]:
        if filing["id"] not in documents:
            attrs = filing["attributes"]
            doc = _get_json("https://filings.xbrl.org" + attrs["json_url"])
            documents[filing["id"]] = esef_values(doc, attrs["period_end"])[0]
        return documents[filing["id"]]

    dated = []
    for year_end, item, value, currency, known_from in rows:
        source = None
        for filing in by_year.get(year_end, []):
            found = values_in(filing).get(item)
            if found is not None:
                source = filing if found == (value, currency) else None
                break
        if source is None:
            continue
        added = source["attributes"].get("date_added")
        dated.append((esef_known_from(year_end, added), known_from, year_end, item, source["id"], _text(added)))
    with TRADE_HISTORY_LOCK:
        conn = _connection()
        with conn:
            for new, _old, year_end, item, filing_id, added in dated:
                conn.execute("UPDATE company_numbers SET known_from = ? "
                             "WHERE symbol = ? AND year_end = ? AND item = ? AND source = 'esef'",
                             (new, symbol, year_end, item))
                conn.execute("INSERT INTO esef_number_filings VALUES (?, ?, ?, ?, ?)",
                             (symbol, year_end, item, filing_id, added))
    changed = sum(1 for new, old, *_ in dated if new != old)
    left = len(rows) - len(dated)
    APP_LOG.info("esef_dates_checked symbol=%s dated=%d changed=%d unchecked=%d", symbol, len(dated), changed, left)
    if left:
        SYS_LOG.warning("esef_dates_unchecked symbol=%s rows=%d reason=no_matching_filing", symbol, left)
    return left


def unchecked_esef_rows(symbols: Optional[List[str]] = None) -> int:
    """ESEF rows not yet dated by their filing, for these stocks (all if None)."""
    with TRADE_HISTORY_LOCK:
        rows = _connection().execute(
            "SELECT n.symbol FROM company_numbers n LEFT JOIN esef_number_filings f "
            "USING (symbol, year_end, item) WHERE n.source = 'esef' AND f.filing_id IS NULL").fetchall()
    return sum(1 for (symbol,) in rows if symbols is None or symbol in symbols)


def load_esef(symbol: str, isin: str) -> int:
    """New ESEF filings for one stock. Returns how many numbers were saved."""
    lei = lei_for_isin(isin)
    if not lei:
        return 0
    filings = esef_filings(lei)
    try:
        _check_saved_dates(symbol, filings)
    except Exception as exc:  # rows stay unchecked and are tried again next week
        SYS_LOG.warning("esef_dates_check_failed symbol=%s detail=%s", symbol, exc)
    with TRADE_HISTORY_LOCK:
        read = {row[0] for row in _connection().execute(
            "SELECT filing_id FROM esef_filings_read WHERE symbol = ?", (symbol,))}
    cutoff = date.today().replace(month=1, day=1) - timedelta(days=365 * YEARS_WANTED)
    saved = 0
    for filing in _ingestion_order(filings):
        attrs = filing["attributes"]
        if filing["id"] in read or not attrs.get("json_url") or not attrs.get("period_end"):
            continue
        if date.fromisoformat(attrs["period_end"]) < cutoff:
            continue
        doc = _get_json("https://filings.xbrl.org" + attrs["json_url"])
        values, year_end = esef_values(doc, attrs["period_end"])
        # Only `date_added`: `processed` is when filings.xbrl.org last ran its
        # checks (Equinor 2021: added 2022-03-29, processed 2023-01-18), and
        # `published` is not what P1-8 dates by.
        known_from = esef_known_from(year_end, attrs.get("date_added"))
        saved += _save(symbol, year_end, values, "esef", known_from, (filing["id"], attrs.get("date_added")))
        with TRADE_HISTORY_LOCK:
            conn = _connection()
            with conn:
                conn.execute("INSERT OR IGNORE INTO esef_filings_read VALUES (?, ?)", (filing["id"], symbol))
    return saved


def load_yahoo(symbol: str) -> int:
    """Fill items ESEF did not have, from Yahoo's yearly statements."""
    ticker = yf.Ticker(symbol)
    currency = (ticker.info or {}).get("financialCurrency")
    tables = {name: getattr(ticker, name) for name in ("income_stmt", "balance_sheet", "cashflow")}
    by_year: Dict[str, Dict[str, Tuple[float, Optional[str]]]] = {}
    for item, (table_name, row_name) in YAHOO_ROWS.items():
        table = tables.get(table_name)
        if table is None or row_name not in getattr(table, "index", []):
            continue
        for column, value in table.loc[row_name].items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number != number:  # NaN
                continue
            year_end = column.strftime("%Y-%m-%d")
            by_year.setdefault(year_end, {})[item] = (number, None if item == "shares_outstanding" else currency)
    saved = 0
    for year_end, values in by_year.items():
        known_from = (date.fromisoformat(year_end) + YAHOO_KNOWN_AFTER).isoformat()
        saved += _save(symbol, year_end, values, "yahoo", known_from)
    return saved


def load_company_numbers(rows: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Every stock in the list: ESEF first, then Yahoo for the gaps."""
    rows = rows if rows is not None else stock_list.load()
    summary: Dict[str, Any] = {"saved": 0, "failed": []}
    for row in rows:
        esef_ok = yahoo_ok = True
        if row["region"] != "US":
            try:
                summary["saved"] += load_esef(row["ticker"], row["isin"])
            except NotInEsef:
                pass  # common for German, UK and new listings; Yahoo covers it
            except Exception as exc:
                SYS_LOG.warning("esef_numbers_failed symbol=%s detail=%s", row["ticker"], exc)
                esef_ok = False
        # Yahoo runs whatever ESEF did: it fills the items ESEF lacks.
        try:
            summary["saved"] += load_yahoo(row["ticker"])
        except Exception as exc:
            SYS_LOG.warning("yahoo_numbers_failed symbol=%s detail=%s", row["ticker"], exc)
            yahoo_ok = False
        if not esef_ok and not yahoo_ok:
            summary["failed"].append(row["ticker"])
    # Saved before version 4 and not matched to a filing yet (P1-8): their
    # `known_from` may still be the old, too early deadline date.
    summary["esef_dates_unchecked"] = unchecked_esef_rows([row["ticker"] for row in rows])
    return summary


def coverage(years: int = 3) -> Tuple[int, int]:
    """(stocks with all main items for each of the last `years` years, stocks listed)."""
    listed = stock_list.tickers()
    with TRADE_HISTORY_LOCK:
        rows = _connection().execute(
            "SELECT symbol, substr(year_end, 1, 4), COUNT(DISTINCT item) FROM company_numbers "
            "WHERE item IN ('revenue', 'net_profit', 'equity') GROUP BY 1, 2").fetchall()
    full: Dict[str, int] = {}
    for symbol, _year, items in rows:
        if items == 3:
            full[symbol] = full.get(symbol, 0) + 1
    return sum(1 for symbol in listed if full.get(symbol, 0) >= years), len(listed)


def job_load_company_numbers() -> Dict[str, Any]:
    """Weekly wrapper. Never raises."""
    try:
        summary = load_company_numbers()
    except Exception as exc:
        SYS_LOG.error("company_numbers_crashed detail=%s", exc)
        return {}
    APP_LOG.info("company_numbers saved=%d failed=%d esef_dates_unchecked=%d", summary["saved"],
                 len(summary["failed"]), summary["esef_dates_unchecked"])
    return summary
