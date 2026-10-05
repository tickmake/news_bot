"""Point-in-time S&P 500 membership, reconstructed from Wikipedia revisions.

The article no longer carries the "selected changes to the list" table that the
usual reconstruction recipe walks backwards, so this takes the article's own
revision at each quarter end and parses the constituents table out of it. That
yields true membership for the date, including names later removed.

    python research/pit_members.py pit_members.pkl [START END FREQ]

The API rate-limits hard; 47 quarters takes about 20 minutes with the backoff.
"""
import io
import json
import pickle
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

UA = "news_bot-research/1.0 (personal research)"
API = "https://en.wikipedia.org/w/api.php"
TITLE = "List of S&P 500 companies"


def _get(params):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})  # noqa: S310
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:  # noqa: S310
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = int(exc.headers.get("Retry-After") or 0) or 20 * (attempt + 1)
                print(f"  429, sleeping {wait}s", flush=True)
                time.sleep(wait)
                continue
            if attempt == 5:
                raise
            time.sleep(3 * (attempt + 1))
        except Exception:
            if attempt == 5:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def revision_at(timestamp):
    """The id of the last revision at or before `timestamp`."""
    data = _get({"action": "query", "prop": "revisions", "titles": TITLE, "rvlimit": 1,
                 "rvstart": timestamp, "rvdir": "older", "rvprop": "ids|timestamp",
                 "format": "json"})
    page = next(iter(data["query"]["pages"].values()))
    revisions = page.get("revisions")
    return (revisions[0]["revid"], revisions[0]["timestamp"]) if revisions else (None, None)


def symbols_of(revid):
    """Ticker symbols in the constituents table of one revision, Yahoo-style."""
    html = _get({"action": "parse", "oldid": revid, "prop": "text",
                 "format": "json", "formatversion": 2})["parse"]["text"]
    for table in pd.read_html(io.StringIO(html)):
        columns = [str(c) for c in table.columns]
        column = next((c for c in columns
                       if c == "Ticker" or re.fullmatch(r"(Ticker )?[Ss]ymbol", c)), None)
        if column and len(table) > 300:
            series = (table[column].astype(str).str.strip().str.upper()
                      .str.replace(r"\.", "-", regex=True)
                      .str.replace(r"\s.*", "", regex=True))
            return sorted({s for s in series if re.fullmatch(r"[A-Z][A-Z0-9-]{0,6}", s)})
    return None


def main(out_path, start="2015-01-31", end="2026-09-30", freq="QE"):
    captured = {}
    for date in pd.date_range(start, end, freq=freq):
        try:
            revid, rev_ts = revision_at(date.strftime("%Y-%m-%dT23:59:59Z"))
            symbols = symbols_of(revid) if revid else None
            if symbols:
                captured[date.strftime("%Y-%m-%d")] = {
                    "revid": revid, "rev_ts": rev_ts, "symbols": symbols}
            print(date.date(), revid, len(symbols) if symbols else "PARSE-FAIL", flush=True)
        except Exception as exc:
            print(date.date(), "ERR", type(exc).__name__, exc, flush=True)
        time.sleep(2.0)
    with open(out_path, "wb") as handle:
        pickle.dump(captured, handle)
    print("periods captured:", len(captured))


if __name__ == "__main__":
    main(*sys.argv[1:])
