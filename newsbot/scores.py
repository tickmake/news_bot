"""Four 0-100 company scores per stock per month end (Phase 2, #146-#151).

Built from `score_inputs` (#145), so a past month is scored only with what was
public at its end:

- Quality (#146): the report card's 9 health checks (#124), return on equity,
  and net debt in years of operating profit (skipped for banks and insurers,
  which borrow by design).
- Value (#147): earnings, book value and operating cash flow per 1 of share
  price, in the price's currency. Higher is cheaper. A loss gives no earnings
  number, never a negative one. Free cash flow needs capital spending, which
  the bot does not keep, so operating cash flow stands in for it.
- Performance (#148): sales and profit growth against the year before.
  Analysts' estimate changes are shown live in /analyze but not scored: their
  history only starts in 2026-10.
- Momentum (#149): the price change over the 12 months to a month ago; the
  latest month is left out because it tends to reverse.

Each number is ranked 0-100 against the same sector that month (#150); a
sector with fewer than 5 stocks is ranked against its whole region instead.
An area's score is the average of its ranked numbers. A missing number is
left out, never guessed, and an area with nothing left has no score.

Information only until Phase 3 (#79) shows the scores work.
"""

import json
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from newsbot import report_card, score_inputs, stock_list
from newsbot.logsetup import APP_LOG, SYS_LOG
from newsbot.state import TRADE_HISTORY_LOCK

AREAS = ("quality", "value", "performance", "momentum")
# Each area's numbers, and whether higher is better.
METRICS = {
    "quality": (("health", True), ("return_on_equity", True), ("debt_years", False)),
    "value": (("earnings_yield", True), ("book_yield", True), ("cash_flow_yield", True)),
    "performance": (("sales_growth", True), ("profit_growth", True)),
    "momentum": (("momentum_12_1", True),),
}
MIN_PEERS = 5      # below this a sector is too small to rank in; use the region
MIN_RANKED = 3     # below this nothing is ranked at all
NO_DEBT_CHECK = {"Financial Services"}

_TABLE = """
    CREATE TABLE IF NOT EXISTS company_scores (
        symbol TEXT NOT NULL,
        month_end TEXT NOT NULL,
        quality INTEGER,
        value INTEGER,
        performance INTEGER,
        momentum INTEGER,
        peers TEXT,
        details_json TEXT NOT NULL,
        PRIMARY KEY (symbol, month_end)
    )
"""


def _connection():
    conn = score_inputs._connection()
    conn.execute(_TABLE)
    return conn


def _ratio(top: Optional[float], bottom: Optional[float]) -> Optional[float]:
    if top is None or not bottom:
        return None
    return top / bottom


def _growth(now: Optional[float], before: Optional[float]) -> Optional[float]:
    """Change in %, only from a positive starting point (growth from a loss means nothing)."""
    if now is None or before is None or before <= 0:
        return None
    return (now / before - 1.0) * 100.0


def metrics(row: Dict[str, Any], sector: str) -> Dict[str, Optional[float]]:
    """The raw numbers behind the four scores, for one score_inputs row."""
    now, before = row["numbers"], row["previous"]
    checks = [c.passed for c in report_card.health_checks(now, before)] if before else []
    known = [passed for passed in checks if passed is not None]
    profit, equity = now.get("net_profit"), now.get("equity")

    market_value = None
    if row["close"] and now.get("shares_outstanding") and row["fx"]:
        # In the numbers' currency, so it compares directly with profit and equity.
        market_value = row["close"] * now["shares_outstanding"] / row["fx"]

    return {
        # At least 5 of the 9 checks must be answerable, or it says too little.
        "health": len([p for p in known if p]) / len(known) * 9 if len(known) >= 5 else None,
        "return_on_equity": _ratio(profit, equity) * 100 if equity and equity > 0 and profit is not None else None,
        "debt_years": None if sector in NO_DEBT_CHECK else report_card.debt_years(now),
        "earnings_yield": _ratio(profit, market_value) * 100 if profit and profit > 0 and market_value else None,
        "book_yield": _ratio(equity, market_value) * 100 if equity and equity > 0 and market_value else None,
        "cash_flow_yield": (_ratio(now.get("operating_cash_flow"), market_value) * 100
                            if now.get("operating_cash_flow") is not None and market_value else None),
        "sales_growth": _growth(now.get("revenue"), before.get("revenue")),
        "profit_growth": _growth(profit, before.get("net_profit")),
        "momentum_12_1": ((row["close_1m"] / row["close_12m"] - 1.0) * 100.0
                          if row["close_1m"] and row["close_12m"] else None),
    }


def percentiles(values: Dict[str, float], higher_is_better: bool = True) -> Dict[str, float]:
    """0 (worst) to 100 (best) within the group; ties share the average place."""
    if len(values) < MIN_RANKED:
        return {}
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=not higher_is_better)
    places: Dict[str, float] = {}
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        for k in range(i, j + 1):
            places[ordered[k][0]] = (i + j) / 2
        i = j + 1
    last = len(ordered) - 1
    return {symbol: place / last * 100.0 for symbol, place in places.items()}


def score_month(raw: Dict[str, Dict[str, Optional[float]]],
                groups: Dict[str, Tuple[str, str]]) -> Dict[str, Dict[str, Any]]:
    """Scores for every stock in one month. `groups` maps symbol -> (sector, region)."""
    out: Dict[str, Dict[str, Any]] = {s: {"ranks": {}, "peers": {}} for s in raw}
    for area, numbers in METRICS.items():
        for name, higher in numbers:
            have = {s: m[name] for s, m in raw.items() if m.get(name) is not None}
            by_sector: Dict[str, Dict[str, float]] = {}
            for symbol, value in have.items():
                by_sector.setdefault(groups[symbol][0], {})[symbol] = value
            for symbol in have:
                sector, region = groups[symbol]
                peers = by_sector[sector]
                label = sector
                if len(peers) < MIN_PEERS:
                    peers = {s: v for s, v in have.items() if groups[s][1] == region}
                    label = f"{region} (sector too small)"
                rank = percentiles(peers, higher).get(symbol)
                if rank is not None:
                    out[symbol]["ranks"][name] = rank
                    out[symbol]["peers"][area] = f"{label}, {len(peers)} stocks"
    for result in out.values():
        for area, numbers in METRICS.items():
            ranks = [result["ranks"][n] for n, _ in numbers if n in result["ranks"]]
            result[area] = round(sum(ranks) / len(ranks)) if ranks else None
    return out


def build(today: Optional[date] = None) -> int:
    """Score every month end in score_inputs. Returns how many rows were written."""
    listed = {row["ticker"]: row for row in stock_list.load()}
    with TRADE_HISTORY_LOCK:
        rows = _connection().execute(
            "SELECT symbol, month_end, close, close_1m, close_12m, numbers_json, previous_json, fx "
            "FROM score_inputs").fetchall()
    months: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for symbol, month_end, close, close_1m, close_12m, numbers, previous, fx in rows:
        if symbol not in listed:
            continue
        months.setdefault(month_end, {})[symbol] = {
            "close": close, "close_1m": close_1m, "close_12m": close_12m,
            "numbers": json.loads(numbers), "previous": json.loads(previous), "fx": fx}

    written = []
    for month_end, inputs in months.items():
        groups = {s: (listed[s]["sector"], listed[s]["region"]) for s in inputs}
        raw = {s: metrics(row, groups[s][0]) for s, row in inputs.items()}
        for symbol, result in score_month(raw, groups).items():
            details = {"metrics": raw[symbol], "ranks": result["ranks"]}
            written.append((symbol, month_end, *(result[a] for a in AREAS),
                            json.dumps(result["peers"]), json.dumps(details)))
    with TRADE_HISTORY_LOCK:
        conn = _connection()
        with conn:
            conn.executemany("INSERT OR REPLACE INTO company_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?)", written)
    return len(written)


def job_build_scores() -> int:
    """Weekly: score inputs first, then the scores. Never raises."""
    try:
        score_inputs.build()
        written = build()
    except Exception as exc:
        SYS_LOG.error("company_scores_failed detail=%s", exc)
        return 0
    APP_LOG.info("company_scores written=%d", written)
    return written


def latest(symbol: str) -> Optional[Dict[str, Any]]:
    """The newest month's scores for one stock, or None."""
    with TRADE_HISTORY_LOCK:
        row = _connection().execute(
            "SELECT month_end, quality, value, performance, momentum, peers, details_json "
            "FROM company_scores WHERE symbol = ? ORDER BY month_end DESC LIMIT 1", (symbol,)).fetchone()
    if not row:
        return None
    return {"month_end": row[0], **dict(zip(AREAS, row[1:5], strict=True)),
            "peers": json.loads(row[5] or "{}"), **json.loads(row[6])}


MEANINGS = {
    "quality": "profitable, low debt and getting stronger, compared with similar companies.",
    "value": "the share price is low compared with the company's profit, book value and cash flow.",
    "performance": "sales and profit grew more than at similar companies.",
    "momentum": "the share price rose more over the past year (leaving out the last month).",
}
LABELS = {"quality": "Quality", "value": "Value", "performance": "Company performance",
          "momentum": "Momentum"}


def estimate_change(symbol: str, today: Optional[date] = None, days: int = 30) -> Optional[float]:
    """Change in % of analysts' forward earnings per share over `days`, from the nightly snapshots (#71)."""
    today = today or date.today()
    start = date.fromordinal(today.toordinal() - days).isoformat()
    try:
        with TRADE_HISTORY_LOCK:
            rows = _connection().execute(
                "SELECT data_json FROM stock_snapshots WHERE symbol = ? AND snapshot_date >= ? "
                "AND snapshot_date <= ? ORDER BY snapshot_date", (symbol, start, today.isoformat())).fetchall()
    except Exception:  # no snapshots taken yet: the table does not exist
        return None
    eps = [json.loads(r[0]).get("info", {}).get("forwardEps") for r in rows]
    eps = [e for e in eps if isinstance(e, (int, float))]
    if len(eps) < 2 or eps[0] <= 0:
        return None
    return (eps[-1] / eps[0] - 1.0) * 100.0


def build_scores_section(symbol: str) -> List[str]:
    """Telegram HTML lines for /analyze. Information only: no buy or sell words."""
    lines = ["<b>Scores</b>"]
    result = latest(symbol)
    if not result:
        lines.append("Not available: no scores for this stock yet (they are worked out on Sundays "
                     "for the stocks on the list).")
        return lines
    lines.append(f"<i>0-100 against similar stocks, at the end of {result['month_end']}. "
                 "100 means the best of the group. They describe the company. Tested on 2021-2026, "
                 "none of them picked shares that did better over the next month.</i>")
    for area in AREAS:
        value = result[area]
        text = f"{value}/100" if value is not None else "not available"
        peers = result["peers"].get(area)
        lines.append(f"{LABELS[area]}: <b>{text}</b>" + (f" ({peers})" if peers and value is not None else ""))
        lines.append(f"<i>High means {MEANINGS[area]}</i>")
    change = estimate_change(symbol)
    if change is not None:
        lines.append(f"Analysts' earnings estimate, last 30 days: {change:+.1f}%")
        lines.append("<i>How much analysts changed the profit they expect for next year. "
                     "Shown, not scored: the bot has kept these only since 2026-10.</i>")
    return lines
