"""Rerun section 10 with ESEF numbers dated by their filing (P1-8).

Works on copies of the bot's database, never the live one. Three steps:

    cp news_bot.db /tmp/before.db && cp news_bot.db /tmp/after.db
    python research/esef_dates_rerun.py redate  /tmp/after.db              # the bot's own fix, needs filings.xbrl.org
    python research/esef_dates_rerun.py rebuild /tmp/before.db /tmp/fx.pkl # score_inputs + company_scores
    python research/esef_dates_rerun.py rebuild /tmp/after.db  /tmp/fx.pkl # same exchange rates for both
    python research/esef_dates_rerun.py compare /tmp/before.db /tmp/after.db
    python research/score_backtest.py /tmp/before.db
    python research/score_backtest.py /tmp/after.db

`redate` runs `company_numbers._check_saved_dates` for every listed stock, the
same code the weekly load runs, and loads no new filings, so the only change
between the copies is `known_from`. `rebuild` caches Yahoo's exchange rates in
the pickle on its first run and reuses them, and builds as of one fixed day,
so the two copies differ only by the dates.
"""
import os
import pickle
import sqlite3
import sys
from datetime import date

AS_OF = date(2026, 11, 14)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _use(db_path):
    """Point the bot's code at a database copy. Call before importing newsbot."""
    sys.path.insert(0, ROOT)
    os.environ["TRADE_HISTORY_DB_FILE"] = os.path.abspath(db_path)
    os.environ.setdefault("STATE_FILE", os.path.abspath(db_path) + ".state.json")
    os.environ.setdefault("LOG_LEVEL", "WARNING")


def redate(db_path):
    _use(db_path)
    from newsbot import company_numbers as cn
    from newsbot import stock_list
    from newsbot.state import TRADE_HISTORY_LOCK
    listed = stock_list.load()
    with TRADE_HISTORY_LOCK:
        held = {row[0] for row in cn._connection().execute(
            "SELECT DISTINCT symbol FROM company_numbers WHERE source = 'esef'")}
    for row in listed:
        if row["ticker"] not in held:
            continue
        lei = cn.lei_for_isin(row["isin"])
        left = cn._check_saved_dates(row["ticker"], cn.esef_filings(lei)) if lei else None
        print(row["ticker"], "unchecked:", left, flush=True)
    print("unchecked, listed stocks:", cn.unchecked_esef_rows([r["ticker"] for r in listed]))
    print("unchecked, all:", cn.unchecked_esef_rows(), "(stocks no longer listed are never checked)")


def rebuild(db_path, fx_cache):
    _use(db_path)
    from newsbot import score_inputs, scores
    rates = {}
    if os.path.exists(fx_cache):
        with open(fx_cache, "rb") as handle:
            rates = pickle.load(handle)  # noqa: S301 - this script's own cache

    def cached(pair):
        if pair not in rates:
            rates[pair] = score_inputs._fx_closes(pair)
        return rates[pair]

    written = score_inputs.build(today=AS_OF, fx_closes=cached)
    with open(fx_cache, "wb") as handle:
        pickle.dump(rates, handle)
    print("score_inputs", written, "company_scores", scores.build(today=AS_OF))


def _q(path, sql):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def compare(before, after):
    """What the corrected dates change, from the numbers to the scores."""
    known = ("SELECT n.source, COUNT(*) FROM company_numbers n "
             "JOIN (SELECT DISTINCT month_end FROM score_inputs) m ON n.known_from <= m.month_end GROUP BY 1")
    print("company-number rows available, summed over month ends (source, rows):")
    print("  before", _q(before, known))
    print("  after ", _q(after, known))
    old = {(s, y, i): k for s, y, i, k in _q(before, "SELECT symbol, year_end, item, known_from FROM company_numbers")}
    new = {(s, y, i): k for s, y, i, k in _q(after, "SELECT symbol, year_end, item, known_from FROM company_numbers")}
    moved = [(date.fromisoformat(new[k]) - date.fromisoformat(old[k])).days for k in old if new.get(k) != old[k]]
    print(f"rows with a new known_from: {len(moved)}; later: {sum(d > 0 for d in moved)}, "
          f"earlier: {sum(d < 0 for d in moved)}; days later: median {sorted(moved)[len(moved) // 2] if moved else 0}, "
          f"max {max(moved, default=0)}")
    rows = "SELECT symbol, month_end, year_end, numbers_json, previous_year_end, previous_json FROM score_inputs"
    a = {r[:2]: r[2:] for r in _q(before, rows)}
    b = {r[:2]: r[2:] for r in _q(after, rows)}
    print(f"score_inputs rows: before {len(a)}, after {len(b)}; with numbers: before "
          f"{sum(1 for v in a.values() if v[1] != '{}')}, after {sum(1 for v in b.values() if v[1] != '{}')}")
    print(f"score_inputs rows whose numbers changed: {sum(1 for k in a if k in b and a[k] != b[k])}; "
          f"newest year changed: {sum(1 for k in a if k in b and a[k][0] != b[k][0])}")
    areas = ("quality", "value", "performance", "momentum")
    count = "SELECT COUNT(quality), COUNT(value), COUNT(performance), COUNT(momentum) FROM company_scores"
    print("scores present (quality, value, performance, momentum): before", _q(before, count)[0],
          "after", _q(after, count)[0])
    each = "SELECT symbol, month_end, quality, value, performance, momentum FROM company_scores"
    sa = {r[:2]: r[2:] for r in _q(before, each)}
    sb = {r[:2]: r[2:] for r in _q(after, each)}
    for i, area in enumerate(areas):
        changed = sum(1 for k in sa if k in sb and sa[k][i] != sb[k][i])
        print(f"  {area}: stock-months with a different score: {changed}")


if __name__ == "__main__":
    command, *args = sys.argv[1:]
    {"redate": redate, "rebuild": rebuild, "compare": compare}[command](*args)
