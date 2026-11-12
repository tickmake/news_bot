"""Plain-English report card for one stock, for /analyze (#124).

Built only from numbers the bot has saved: yearly company numbers
(company_numbers) and the nightly analyst snapshot (stock_snapshots). Every
number is taken as it was known on `as_of`, using `known_from` and the snapshot
date, so the same code can be run on past dates when the scores are tested.

Information only: it says what the numbers are and what they usually mean,
never what to buy or sell. A missing number is "not available", never a guess.
"""

import json
import statistics
from dataclasses import dataclass
from datetime import date
from html import escape
from typing import Dict, List, Optional, Tuple

from newsbot import company_numbers
from newsbot.state import TRADE_HISTORY_LOCK

Year = Dict[str, float]


@dataclass
class Check:
    name: str
    passed: Optional[bool]  # None: a number it needs is missing
    meaning: str


def _years(symbol: str, as_of: date) -> Tuple[List[str], Dict[str, Year], Optional[str]]:
    """Year ends (newest first), their numbers known by `as_of`, and the currency."""
    with TRADE_HISTORY_LOCK:
        rows = company_numbers._connection().execute(
            "SELECT year_end, item, value, currency FROM company_numbers "
            "WHERE symbol = ? AND known_from <= ? ORDER BY year_end DESC",
            (symbol, as_of.isoformat())).fetchall()
    by_year: Dict[str, Year] = {}
    currency = None
    for year_end, item, value, item_currency in rows:
        by_year.setdefault(year_end, {})[item] = value
        currency = currency or item_currency
    return sorted(by_year, reverse=True), by_year, currency


def _snapshots(as_of: date) -> Dict[str, dict]:
    """Each stock's latest snapshot `info` on or before `as_of`."""
    try:
        with TRADE_HISTORY_LOCK:
            rows = company_numbers._connection().execute(
                "SELECT symbol, data_json FROM stock_snapshots s WHERE snapshot_date = ("
                " SELECT MAX(snapshot_date) FROM stock_snapshots WHERE symbol = s.symbol"
                " AND snapshot_date <= ?)", (as_of.isoformat(),)).fetchall()
    except Exception:  # no snapshots taken yet: the table does not exist
        return {}
    out = {}
    for symbol, data in rows:
        try:
            out[symbol] = json.loads(data).get("info") or {}
        except ValueError:
            continue
    return out


def _ratio(top: Optional[float], bottom: Optional[float]) -> Optional[float]:
    if top is None or not bottom:
        return None
    return top / bottom


def _rising(now: Optional[float], before: Optional[float]) -> Optional[bool]:
    if now is None or before is None:
        return None
    return now > before


def health_checks(now: Year, before: Year) -> List[Check]:
    """The nine yes/no checks of the Piotroski F-score, for one year against the one before.

    Simplified where the bot keeps fewer numbers than the original: ratios use
    the same year's assets, and "debt falling" is all debt against assets.
    """
    def g(year: Year, item: str) -> Optional[float]:
        return year.get(item)

    roa = _ratio(g(now, "net_profit"), g(now, "total_assets"))
    roa_before = _ratio(g(before, "net_profit"), g(before, "total_assets"))
    profit, cash_flow = g(now, "net_profit"), g(now, "operating_cash_flow")
    shares, shares_before = g(now, "shares_outstanding"), g(before, "shares_outstanding")
    debt_share = _ratio(g(now, "total_debt"), g(now, "total_assets"))
    debt_share_before = _ratio(g(before, "total_debt"), g(before, "total_assets"))
    return [
        Check("Made a profit", None if profit is None else profit > 0,
              "the company earned more than it spent over the year."),
        Check("Cash came in from the business", None if cash_flow is None else cash_flow > 0,
              "day-to-day business brought in real cash, not just profit on paper."),
        Check("Return on assets rising", _rising(roa, roa_before),
              "it earned more for everything it owns than the year before."),
        Check("Cash flow bigger than profit",
              None if profit is None or cash_flow is None else cash_flow > profit,
              "profit is backed by cash. When it is not, the profit may be accounting, "
              "not money in the bank."),
        Check("Debt falling", None if _rising(debt_share, debt_share_before) is None
              else debt_share < debt_share_before,
              "debt is a smaller part of what it owns than a year ago."),
        Check("Short-term bills easier to pay", _rising(
            _ratio(g(now, "current_assets"), g(now, "current_liabilities")),
            _ratio(g(before, "current_assets"), g(before, "current_liabilities"))),
              "what it can turn into cash within a year grew compared with bills due "
              "within a year."),
        Check("No new shares", None if shares is None or shares_before is None
              else shares <= shares_before,
              "it did not issue new shares. New shares split the company into more "
              "pieces, so each existing share owns less."),
        Check("Gross margin rising", _rising(
            _ratio(g(now, "gross_profit"), g(now, "revenue")),
            _ratio(g(before, "gross_profit"), g(before, "revenue"))),
              "it keeps more of each sale after the direct cost of making it."),
        Check("Sales per asset rising", _rising(
            _ratio(g(now, "revenue"), g(now, "total_assets")),
            _ratio(g(before, "revenue"), g(before, "total_assets"))),
              "it sold more for what it owns, so it uses what it owns better."),
    ]


def debt_years(year: Year) -> Optional[float]:
    """Net debt (debt minus cash) in years of operating profit. Negative: more cash than debt."""
    debt, cash, profit = year.get("total_debt"), year.get("cash"), year.get("operating_profit")
    if debt is None or cash is None or not profit or profit < 0:
        return None
    return (debt - cash) / profit


def _debt_words(years: Optional[float], year: Year) -> str:
    if years is None:
        if year.get("operating_profit") is not None and year["operating_profit"] < 0:
            return "Not available: the company made an operating loss, so profit cannot pay debt down."
        return "Not available."
    if years <= 0:
        return "It holds more cash than debt."
    return f"Paying off net debt would take about {years:.1f} years of operating profit."


def _sector_middle(snapshots: Dict[str, dict], sector: str, key: str) -> Tuple[Optional[float], int]:
    values = [info[key] for info in snapshots.values()
              if info.get("sector") == sector and isinstance(info.get(key), (int, float)) and info[key] > 0]
    return (statistics.median(values), len(values)) if len(values) >= 3 else (None, len(values))


def build_report_card(symbol: str, as_of: Optional[date] = None) -> List[str]:
    """Telegram HTML lines for the report card section of /analyze."""
    as_of = as_of or date.today()
    lines = ["<b>Report card</b>"]
    year_ends, by_year, currency = _years(symbol, as_of)
    if len(year_ends) < 2:
        lines.append("Not available: the bot has fewer than two years of company numbers "
                     "for this stock.")
    else:
        now, before = by_year[year_ends[0]], by_year[year_ends[1]]
        checks = health_checks(now, before)
        known = [c for c in checks if c.passed is not None]
        score = sum(1 for c in known if c.passed)
        lines.append(f"<b>Health check: {score} of {len(known)}</b>"
                     + (f" ({9 - len(known)} of 9 not available)" if len(known) < 9 else "")
                     + f", year to {year_ends[0]} against the year before.")
        lines.append("<i>The Piotroski F-score: nine yes/no checks on the yearly accounts. "
                     "A high score means the business got stronger over the year, a low "
                     "one that it got weaker.</i>")
        for check in checks:
            mark = {True: "✅", False: "❌", None: "➖"}[check.passed]
            suffix = " (not available)" if check.passed is None else ""
            lines.append(f"{mark} {escape(check.name)}{suffix}: <i>Yes means {escape(check.meaning)}</i>")
        lines.append("")
        lines.append(f"<b>Debt:</b> {escape(_debt_words(debt_years(now), now))}")
        lines.append("<i>Net debt is debt minus cash. Below 3 years of profit is usually "
                     "seen as comfortable; banks are different and borrow by design.</i>")
        if currency:
            lines.append(f"<i>Company numbers in {escape(currency)}.</i>")

    snapshots = _snapshots(as_of)
    info = snapshots.get(symbol, {})
    sector = info.get("sector")
    lines.append("")
    if not sector:
        lines.append("<b>Price vs sector:</b> not available (no analyst snapshot for this stock).")
    else:
        lines.append(f"<b>Price vs sector</b> ({escape(sector)})")
        for key, label, meaning in (
            ("trailingPE", "P/E", "Price paid for each 1 of yearly profit. Above the sector "
                                  "means the market pays more for the same profit, often "
                                  "because it expects growth."),
            ("priceToBook", "Price/book", "Price paid for each 1 the company is worth on its "
                                          "books. Above the sector means the market values "
                                          "it further above its books than its peers."),
        ):
            mine = info.get(key)
            middle, peers = _sector_middle(snapshots, sector, key)
            mine_text = f"{mine:.1f}" if isinstance(mine, (int, float)) and mine > 0 else "not available"
            middle_text = (f"{middle:.1f} (middle of {peers} stocks the bot follows)" if middle
                           else f"not available (fewer than 3 stocks in the sector, {peers} found)")
            lines.append(f"{label}: {mine_text}; sector {middle_text}.")
            lines.append(f"<i>{meaning}</i>")
    return lines
