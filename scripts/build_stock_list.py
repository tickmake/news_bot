"""Rebuild stock_list.csv with the rules from #74 (#113). Run every quarter.

    python scripts/build_stock_list.py            # show what would change
    python scripts/build_stock_list.py --write    # and write stock_list.csv

Rules:
- Oslo: the 43 most-traded Oslo Børs (main market) stocks with a market value
  of at least 2 bn NOK.
- Euronext EU: the 34 most-traded stocks on Paris, Amsterdam, Milan and
  Brussels.
- "Most-traded" is the median daily turnover (price x volume) over 3 months.
  The median, not the average, so a few busy days cannot carry a small stock
  onto the list (as happened with Circio Holding).
- The German and Spanish blue chips and the 10 US stocks are a fixed choice,
  kept as they are.

Where the data comes from: Euronext's own share lists (name, ISIN, ticker),
Yahoo (3-month prices, market value, sector, currency, country) and OpenFIGI
(every new ISIN must map to a listing with the same ticker). Stocks already on
the list keep their saved details, so a rerun only shows real changes.

A stock that leaves the list keeps its saved prices and company numbers;
nothing is deleted.
"""

import argparse
import csv
import io
import os
import statistics
import sys
import time

import requests
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_PATH = os.path.join(ROOT, "stock_list.csv")
COLUMNS = ["ticker", "isin", "name", "exchange", "currency", "country", "sector", "region",
           "ask_allowed", "previous_ticker"]

OSLO_COUNT = 43
EURONEXT_COUNT = 34
OSLO_MIN_MARKET_VALUE_NOK = 2e9
# Shortlist by Euronext's last-day turnover before asking Yahoo for 3 months:
# Paris alone lists hundreds of shares.
SHORTLIST = 120

# Euronext market code -> (Yahoo suffix, exchange name in stock_list.csv, region,
# OpenFIGI exchange code).
MARKETS = {
    "XOSL": (".OL", "Oslo", "Oslo", "NO"),
    "XPAR": (".PA", "Paris", "EU", "FP"),
    "XAMS": (".AS", "Amsterdam", "EU", "NA"),
    "MTAA": (".MI", "Milan", "EU", "IM"),   # Euronext Milan's code is MTAA, not XMIL
    "XBRU": (".BR", "Brussels", "EU", "BB"),
}
EURONEXT_EXCHANGES = {name for _, name, _, _ in MARKETS.values()}

# ISIN country codes in the EEA: the ASK rule from #82. A guide, not a promise.
EEA = set("AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IS IT LI LT LU LV MT NL NO PL PT RO SE SI SK".split())


def euronext_list(mic):
    """Euronext's downloadable list of shares on one market, with last-day turnover."""
    response = requests.post(
        f"https://live.euronext.com/en/pd_es/data/stocks/download?mics={mic}",
        data={"args[fe_type]": "csv", "args[fe_layout]": "ver", "args[fe_decimal_separator]": ".",
              "args[fe_date_format]": "d/m/Y", "iDisplayLength": "10000", "iDisplayStart": "0"},
        timeout=60)
    response.raise_for_status()
    lines = response.content.decode("utf-8-sig").splitlines()
    rows = []
    for row in csv.DictReader(io.StringIO("\n".join([lines[0], *lines[4:]])), delimiter=";"):
        if not row.get("ISIN") or not row.get("Symbol"):
            continue
        try:
            turnover = float(row.get("Turnover") or 0)
        except ValueError:
            turnover = 0.0
        rows.append({"isin": row["ISIN"], "symbol": row["Symbol"], "name": row["Name"],
                     "market": row["Market"], "turnover": turnover})
    return rows


def median_turnover(tickers, short_ok=()):
    """Median daily price x volume over 3 months, per Yahoo ticker.

    A line with no trade in the last week has stopped trading (Cadeler's old
    CADLR.OL line after the move to CDLR.OL) and is left out. A stock already
    on the list may have a short history, as a new line does after a ticker
    change (#112).
    """
    bars = yf.download(tickers, period="3mo", interval="1d", auto_adjust=False,
                       group_by="ticker", progress=False, threads=True)
    out = {}
    for ticker in tickers:
        try:
            frame = bars[ticker][["Close", "Volume"]].dropna()
        except KeyError:
            continue
        if frame.empty or (bars.index[-1] - frame.index[-1]).days > 7:
            continue
        values = (frame["Close"] * frame["Volume"]).tolist()
        if len(values) >= 40 or (values and ticker in short_ok):
            out[ticker] = statistics.median(values)
    return out


def figi_ok(isin, ticker, exch_code):
    """OpenFIGI maps the ISIN to a listing with this ticker on this exchange."""
    for attempt in range(3):
        response = requests.post("https://api.openfigi.com/v3/mapping",
                                 json=[{"idType": "ID_ISIN", "idValue": isin}], timeout=30)
        if response.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        response.raise_for_status()
        data = response.json()[0].get("data") or []
        base = ticker.rsplit(".", 1)[0].upper()
        return any(item.get("exchCode") == exch_code
                   and (item.get("ticker") or "").upper().replace(" ", "") in (base, base.replace("-", ""))
                   for item in data)
    return False


def new_row(ticker, isin, exchange, region):
    info = yf.Ticker(ticker).info
    return {
        "ticker": ticker, "isin": isin, "name": info.get("longName") or info.get("shortName") or "",
        "exchange": exchange, "currency": info.get("currency") or "", "country": info.get("country") or "",
        "sector": info.get("sector") or "", "region": region,
        "ask_allowed": "yes" if isin[:2] in EEA else "no", "previous_ticker": "",
    }


def pick(mics, count, saved, min_market_value=None):
    """The `count` most-traded stocks on these markets, as stock_list rows."""
    candidates = []
    for mic in mics:
        suffix, exchange, region, exch_code = MARKETS[mic]
        for row in euronext_list(mic):
            candidates.append({**row, "ticker": row["symbol"].replace(" ", "-") + suffix,
                               "exchange": exchange, "region": region, "exch_code": exch_code})
        time.sleep(1)
    candidates.sort(key=lambda c: c["turnover"], reverse=True)
    candidates = candidates[:SHORTLIST]
    turnover = median_turnover([c["ticker"] for c in candidates],
                               {c["ticker"] for c in candidates if c["isin"] in saved})
    ranked = sorted((c for c in candidates if c["ticker"] in turnover),
                    key=lambda c: turnover[c["ticker"]], reverse=True)

    # A company that moved to a new share line keeps its history under the old
    # ticker; previous_ticker links the two (see stock_list.previous_tickers).
    old_ticker = {c["name"]: saved[c["isin"]]["ticker"] for c in candidates if c["isin"] in saved}
    rows, skipped, seen = [], [], set()
    for c in ranked:
        if len(rows) == count:
            break
        # One line per company: a share listed on two markets (ArcelorMittal in
        # Paris and Amsterdam), or a company moving to a new share line
        # (Cadeler), counts once, as its busiest line.
        if c["isin"] in seen or c["name"] in seen:
            continue
        seen.update((c["isin"], c["name"]))
        if min_market_value is not None:
            value = yf.Ticker(c["ticker"]).fast_info.get("marketCap")
            if not value:
                skipped.append(f"{c['ticker']} ({c['name']}): Yahoo has no market value")
                continue
            if value < min_market_value:
                continue
        if c["isin"] in saved:
            rows.append(saved[c["isin"]])
            continue
        if not figi_ok(c["isin"], c["ticker"], c["exch_code"]):
            skipped.append(f"{c['ticker']} ({c['name']}): OpenFIGI does not match ISIN {c['isin']}")
            continue
        row = new_row(c["ticker"], c["isin"], c["exchange"], c["region"])
        row["previous_ticker"] = old_ticker.get(c["name"], "")
        if not row["sector"] or not row["currency"]:
            skipped.append(f"{c['ticker']} ({c['name']}): Yahoo has no sector or currency")
            continue
        rows.append(row)
    return rows, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true", help="write stock_list.csv")
    args = parser.parse_args()

    with open(LIST_PATH, encoding="utf-8", newline="") as handle:
        current = list(csv.DictReader(handle))
    saved = {row["isin"]: row for row in current}

    oslo, skipped_oslo = pick(["XOSL"], OSLO_COUNT, saved, OSLO_MIN_MARKET_VALUE_NOK)
    euronext, skipped_eu = pick(["XPAR", "XAMS", "MTAA", "XBRU"], EURONEXT_COUNT, saved)
    fixed = [row for row in current if row["exchange"] not in EURONEXT_EXCHANGES]
    rows = oslo + euronext + fixed

    before = {row["ticker"] for row in current}
    after = {row["ticker"] for row in rows}
    print(f"{len(rows)} stocks: {len(oslo)} Oslo, {len(euronext)} Euronext EU, {len(fixed)} fixed")
    print("Added:  ", ", ".join(sorted(after - before)) or "none")
    print("Removed:", ", ".join(sorted(before - after)) or "none")
    for line in skipped_oslo + skipped_eu:
        print("Skipped:", line)
    if len(oslo) < OSLO_COUNT or len(euronext) < EURONEXT_COUNT:
        sys.exit("Fewer stocks than wanted; stock_list.csv left unchanged.")
    if args.write:
        with open(LIST_PATH, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in COLUMNS} for row in rows)
        print("Wrote", LIST_PATH)


if __name__ == "__main__":
    main()
