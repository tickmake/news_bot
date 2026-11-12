"""Do simple portfolio rules cut the bad months of a 4-stock portfolio? (#125)

No signal is involved: every 20 sessions, 4 random stocks from stock_list.csv
are bought at the next open and sold at the close 20 sessions later (the hold
chosen in #122), net of the 59 NOK DNB minimum fee each way on ~12,000 NOK.
Each rule then picks from the same random order, skipping stocks it forbids:

  sector     at most one stock per sector
  earnings   no stock with an earnings report in the first 7 days of the hold
  corr       no two stocks whose daily returns over the 60 sessions before
             entry correlate above 0.7
  all        the three together

Paired with the random 4 drawn from the same order, so the difference is the
rule, not luck. A rule "helps" when it lifts the worst 5% of portfolio months
by at least 0.1 percentage points and costs at most 0.1 points of average
return, in both the first part (before 2021-11) and the check part. Smaller
changes are noise at 125 periods.

    python research/fetch_stock_list.py /tmp/stock_list_10y.pkl
    python research/portfolio_rules_study.py /tmp/stock_list_10y.pkl /tmp/earnings_dates.pkl

The second file is earnings dates from Yahoo, fetched once and kept. Caveats:
today's list (companies that failed are missing; compare rules, not levels),
and currency fees are left out.
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd
import yfinance as yf

FEE_SIDE_PCT = 59.0 / 12_000 * 100
HOLD = 20
LOOKBACK = 60
PICKS = 4
TRIALS = 400
CORR_LIMIT = 0.7
EARNINGS_DAYS = 7
SPLIT = pd.Timestamp("2021-11-01")
RULES = ("random", "sector", "earnings", "corr", "all")


def earnings_dates(symbols, path):
    if os.path.exists(path):
        with open(path, "rb") as handle:
            return pickle.load(handle)  # noqa: S301 - written below
    out = {}
    for symbol in symbols:
        try:
            frame = yf.Ticker(symbol).get_earnings_dates(limit=60)
            out[symbol] = sorted({stamp.date() for stamp in frame.index}) if frame is not None else []
        except Exception as exc:
            print("no earnings dates", symbol, exc)
            out[symbol] = []
    with open(path, "wb") as handle:
        pickle.dump(out, handle)
    return out


def _bars(history):
    bars = history.copy()
    bars.index = pd.to_datetime(bars.index.strftime("%Y-%m-%d"))
    return bars[~bars.index.duplicated()].dropna(subset=["Open", "Close"])


def periods(bars, meta, reports):
    """Per period: each eligible stock's net return, sector, report flag and recent returns."""
    calendar = pd.bdate_range("2017-01-02", "2026-09-30")
    closes = pd.DataFrame({s: b["Close"] for s, b in bars.items()}).reindex(calendar).ffill(limit=5)
    daily = closes.pct_change(fill_method=None)
    out = []
    for start in calendar[LOOKBACK::HOLD]:
        stocks = {}
        for symbol, b in bars.items():
            after = b.index.searchsorted(start)
            last = after + HOLD - 1
            if after == 0 or last >= len(b) or after < LOOKBACK:
                continue
            entry_day = b.index[after]
            if (entry_day - start).days > 5:
                continue
            gross = (b["Close"].iloc[last] / b["Open"].iloc[after] - 1) * 100
            soon = any(0 <= (day - entry_day.date()).days < EARNINGS_DAYS for day in reports.get(symbol, []))
            stocks[symbol] = {"net": gross - 2 * FEE_SIDE_PCT, "sector": meta[symbol]["sector"],
                              "report": soon}
        if len(stocks) < 20:
            continue
        window = daily.loc[:start].iloc[-LOOKBACK - 1:-1][list(stocks)]
        corr = window.corr(min_periods=40)
        out.append((start, stocks, corr))
    return out


def pick(order, stocks, corr, rule):
    chosen = []
    for symbol in order:
        info = stocks[symbol]
        if rule in ("sector", "all") and any(stocks[c]["sector"] == info["sector"] for c in chosen):
            continue
        if rule in ("earnings", "all") and info["report"]:
            continue
        if rule in ("corr", "all") and any(
                not np.isnan(corr.at[symbol, c]) and corr.at[symbol, c] > CORR_LIMIT for c in chosen):
            continue
        chosen.append(symbol)
        if len(chosen) == PICKS:
            break
    return chosen


def simulate(all_periods, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for start, stocks, corr in all_periods:
        symbols = sorted(stocks)
        for trial in range(TRIALS):
            order = list(rng.permutation(symbols))
            for rule in RULES:
                chosen = pick(order, stocks, corr, "random" if rule == "random" else rule)
                if len(chosen) == PICKS:
                    rows.append((start, trial, rule, np.mean([stocks[s]["net"] for s in chosen])))
    return pd.DataFrame(rows, columns=["start", "trial", "rule", "net"])


def table(frame):
    wide = frame.pivot_table(index=["start", "trial"], columns="rule", values="net")
    out = {}
    for rule in RULES:
        series = wide[rule].dropna()
        out[rule] = {
            "portfolio months": len(series),
            "net mean %": series.mean(),
            "worst 5% %": series.quantile(0.05),
            "months below -5%": (series < -5).mean() * 100,
            "vs random, mean": (wide[rule] - wide["random"]).mean(),
        }
    result = pd.DataFrame(out).T
    result["vs random, worst 5%"] = result["worst 5% %"] - result.at["random", "worst 5% %"]
    return result.round(2)


def main(bars_path, earnings_path):
    with open(bars_path, "rb") as handle:
        raw, meta = pickle.load(handle)  # noqa: S301 - written by fetch_stock_list.py
    bars = {s: _bars(h) for s, h in raw.items()}
    reports = earnings_dates(sorted(bars), earnings_path)
    print(f"{len(bars)} stocks, {sum(1 for r in reports.values() if r)} with earnings dates")
    all_periods = periods(bars, meta, reports)
    frame = simulate(all_periods)
    print(f"{len(all_periods)} periods of {HOLD} sessions x {TRIALS} random draws; "
          f"fee {FEE_SIDE_PCT:.2f}% each way\n")
    pd.set_option("display.width", 200)
    verdicts = {}
    for label, part in (("ALL YEARS", frame), ("FIRST PART (before 2021-11)", frame[frame["start"] < SPLIT]),
                        ("CHECK PART (from 2021-11)", frame[frame["start"] >= SPLIT])):
        result = table(part)
        print(label)
        print(result.to_string())
        print()
        if label != "ALL YEARS":
            for rule in RULES[1:]:
                ok = (result.at[rule, "vs random, worst 5%"] >= 0.1
                      and result.at[rule, "vs random, mean"] >= -0.1)
                verdicts[rule] = verdicts.get(rule, True) and ok
    print("PASSES IN BOTH PARTS:", {rule: bool(ok) for rule, ok in verdicts.items()})
    # How often each rule changes the pick at all.
    wide = frame.pivot_table(index=["start", "trial"], columns="rule", values="net")
    for rule in RULES[1:]:
        print(f"{rule}: a different portfolio than random in "
              f"{((wide[rule] - wide['random']).abs() > 1e-9).mean() * 100:.0f}% of draws")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
