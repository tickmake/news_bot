"""Do the four company scores pick better stocks? Tested on history (#79, Phase 3a).

Every month end, stocks are split into thirds by each score (0-100 within the
sector, from `company_scores`, built only from what was public that day). Each
stock is bought at the next session's open and sold at the close 20 sessions
later, the hold from #122. The test compares:

  top third        the stocks a score likes most
  all              every scored stock that month (a random pick on average)
  bottom third     the stocks it likes least

Fees are the same for every group, so they cancel in the comparisons.

Rules decided before the first run (2026-11-12). A score "works" only if all hold:
  1. top third beats "all" on average in the first part AND the check part,
  2. over all months, that difference has a t-value of at least 2
     (months are independent: one 20-session hold per month, no overlap
     in the comparison), and
  3. top third beats bottom third over all months.
Anything less is reported as "no shown edge", however good one part looks.

    scp raspberrypi5-02 copy of the DB  (see the README)
    python research/score_backtest.py /tmp/news_bot.db

Caveats, stated in every report: today's stock list only (companies that
failed or were bought are missing: Yahoo deletes them), so levels are
flattering; compare groups, not levels. Quality, value and performance start
in 2021 (annual reports in the EU format start with the 2020 accounts);
momentum starts in 2018. ~95 stocks, so a third is ~30 stocks.
"""
import csv
import math
import os
import sqlite3
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AREAS = ("quality", "value", "performance", "momentum", "combined")
HOLD = 20
FEE_ROUND_TRIP_PCT = 2 * 59.0 / 12_000 * 100


def load(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    scores = pd.read_sql_query(
        "SELECT symbol, month_end, quality, value, performance, momentum FROM company_scores", conn)
    prices = pd.read_sql_query("SELECT symbol, session_date, open, close FROM daily_prices", conn)
    conn.close()
    with open(os.path.join(ROOT, "stock_list.csv"), encoding="utf-8", newline="") as handle:
        regions = {row["ticker"]: row["region"] for row in csv.DictReader(handle)}
    return scores, prices, regions


def forward_returns(scores, prices):
    """Net % from the next open after each month end to the close HOLD sessions later."""
    out = {}
    for symbol, bars in prices.groupby("symbol"):
        bars = bars.sort_values("session_date").reset_index(drop=True)
        dates = bars["session_date"].tolist()
        for month_end in scores.loc[scores["symbol"] == symbol, "month_end"]:
            entry = pd.Index(dates).searchsorted(month_end, side="right")
            exit_ = entry + HOLD - 1
            if exit_ >= len(bars) or not bars.at[entry, "open"]:
                continue
            gross = (bars.at[exit_, "close"] / bars.at[entry, "open"] - 1) * 100
            out[(symbol, month_end)] = gross - FEE_ROUND_TRIP_PCT
    return out


def monthly(frame, area):
    """Per month: average net of top third, all, bottom third."""
    rows = []
    for month_end, group in frame.dropna(subset=[area, "net"]).groupby("month_end"):
        if len(group) < 9:   # thirds of at least 3 stocks
            continue
        ranked = group.sort_values(area)
        third = len(ranked) // 3
        rows.append({"month_end": month_end, "stocks": len(ranked),
                     "top": ranked["net"].iloc[-third:].mean(), "all": ranked["net"].mean(),
                     "bottom": ranked["net"].iloc[:third].mean()})
    return pd.DataFrame(rows)


def summary(months):
    if months.empty:
        return {"months": 0}
    diff = months["top"] - months["all"]
    t = diff.mean() / (diff.std(ddof=1) / math.sqrt(len(diff))) if len(diff) > 2 and diff.std() > 0 else float("nan")
    return {"months": len(months), "top net %": months["top"].mean(), "all net %": months["all"].mean(),
            "bottom net %": months["bottom"].mean(), "top - all": diff.mean(), "t": t,
            "top - bottom": (months["top"] - months["bottom"]).mean(),
            "top beats all, % of months": (diff > 0).mean() * 100}


def main(db_path):
    scores, prices, regions = load(db_path)
    scores["combined"] = scores[["quality", "value", "performance", "momentum"]].mean(axis=1, skipna=False)
    net = forward_returns(scores, prices)
    scores["net"] = [net.get((s, m)) for s, m in zip(scores["symbol"], scores["month_end"], strict=True)]
    scores["region"] = scores["symbol"].map(regions)
    print(f"{scores['symbol'].nunique()} stocks; fee {FEE_ROUND_TRIP_PCT:.2f}% round trip per hold\n")

    pd.set_option("display.width", 200)
    verdicts = {}
    for area in AREAS:
        months = monthly(scores, area)
        if months.empty:
            print(area.upper(), "- no months with enough stocks\n")
            continue
        split = months["month_end"].iloc[len(months) // 2]
        parts = {"all months": months, f"first part (to {split})": months[months["month_end"] < split],
                 f"check part (from {split})": months[months["month_end"] >= split]}
        table = pd.DataFrame({label: summary(part) for label, part in parts.items()}).T
        first, check, whole = (table.iloc[i] for i in (1, 2, 0))
        verdicts[area] = bool(first["top - all"] > 0 and check["top - all"] > 0
                              and whole["t"] >= 2 and whole["top - bottom"] > 0)
        print(f"{area.upper()}  ({months['month_end'].iloc[0]} to {months['month_end'].iloc[-1]})")
        print(table.round(2).to_string())
        by_region = {region: summary(monthly(scores[scores["region"] == region], area))
                     for region in ("Oslo", "EU", "US")}
        by_region = {r: s for r, s in by_region.items() if s["months"] > 2}
        if by_region:
            print("  by region (each ranked within its own stocks):")
            print(pd.DataFrame(by_region).T[["months", "top - all", "t", "top - bottom"]].round(2)
                  .to_string().replace("\n", "\n  "))
        print()
    print("WORKS BY THE RULES SET BEFOREHAND:", verdicts)


if __name__ == "__main__":
    main(sys.argv[1])
