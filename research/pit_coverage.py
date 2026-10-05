"""How much of the true point-in-time S&P 500 can still be priced today?

Membership comes from `pit_members.py`. Yahoo drops daily history when a listing
dies, so a name that left the index is usually unobtainable -- which is exactly
the survivorship hole in the momentum tests. This measures the hole per period
instead of correcting it (correcting it needs a dataset with delisted prices).

    python research/pit_members.py pit_members.pkl
    python research/pit_coverage.py pit_members.pkl sp500_wiki.html coverage.csv
"""
import pickle
import sys
import time
import warnings

import pandas as pd
import yfinance as yf

MIN_BARS = 300          # enough history to carry a 12-1 momentum signal
CHUNK = 40


def _current_symbols(path):
    table = pd.read_html(path)[0]
    return set(table["Symbol"].astype(str).str.strip().str.upper()
               .str.replace(r"\.", "-", regex=True))


def bar_counts(symbols, start="2015-01-01", end="2026-10-01"):
    """Daily bars available per symbol. Bulk first, then retry the zeros alone.

    A failed chunk download and a genuinely dead listing look identical, so the
    second pass is what separates them -- without it, transient connection
    errors are miscounted as delistings (ALK, EMN, ZION and eight others all
    came back on the retry).
    """
    counts = {}
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i:i + CHUNK]
        try:
            frame = yf.download(chunk, start=start, end=end, interval="1d",
                                auto_adjust=True, group_by="ticker", threads=True,
                                progress=False)
            for symbol in chunk:
                try:
                    counts[symbol] = int(frame[symbol]["Close"].dropna().shape[0])
                except Exception:
                    counts[symbol] = 0
        except Exception:
            counts.update(dict.fromkeys(chunk, 0))
        print(f"  probed {min(i + CHUNK, len(symbols))}/{len(symbols)}", flush=True)
        time.sleep(1)

    zeros = sorted(s for s, n in counts.items() if n <= MIN_BARS)
    print(f"  re-probing {len(zeros)} zero/short names individually")
    for symbol in zeros:
        for attempt in range(3):
            try:
                counts[symbol] = len(yf.Ticker(symbol).history(
                    start=start, end=end, auto_adjust=True))
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        time.sleep(0.4)
    return counts


def main(members_path, wiki_path, out_csv=None):
    warnings.filterwarnings("ignore")
    with open(members_path, "rb") as handle:
        pit = pickle.load(handle)  # noqa: S301 - a pickle this repo's own script wrote
    periods = sorted(pit)
    current = _current_symbols(wiki_path)
    ever = set().union(*(set(pit[p]["symbols"]) for p in periods))
    departed = sorted(ever - current)
    print(f"periods          : {len(periods)} ({periods[0]} .. {periods[-1]})")
    print(f"symbols ever seen: {len(ever)}")
    print(f"left the index   : {len(departed)}")

    counts = bar_counts(departed)
    priceable = {s for s, n in counts.items() if n > MIN_BARS}
    print(f"\ndeparted names still priceable: {len(priceable)}/{len(departed)} "
          f"({100 * len(priceable) / max(len(departed), 1):.0f}%)")

    rows = []
    for period in periods:
        members = set(pit[period]["symbols"])
        missing = {s for s in members if s not in current and s not in priceable}
        rows.append({"period": period, "true_members": len(members),
                     "missing": len(missing), "pct_missing": 100 * len(missing) / len(members)})
    coverage = pd.DataFrame(rows)
    coverage["year"] = pd.to_datetime(coverage["period"]).dt.year
    by_year = coverage.groupby("year").agg(members=("true_members", "mean"),
                                           missing=("missing", "mean"),
                                           pct_missing=("pct_missing", "mean")).round(1)
    print("\nshare of the true index with no obtainable price history, by year:")
    print(by_year.to_string())
    if out_csv:
        coverage.to_csv(out_csv, index=False)


if __name__ == "__main__":
    main(*sys.argv[1:])
