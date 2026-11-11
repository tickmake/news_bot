"""The stock list file: every stock the bot studies (#74).

`stock_list.csv` at the repo root, one row per stock. ISINs come from the
exchanges' own lists and were checked against OpenFIGI; sector, country and
currency come from Yahoo. `ask_allowed` is "yes" when the ISIN's country is
in the EEA, the rule for an Aksjesparekonto (#82) -- a guide, not a promise.
"""

import csv
import os
from functools import lru_cache
from typing import Dict, List, Optional

COLUMNS = ["ticker", "isin", "name", "exchange", "currency", "country", "sector", "region", "ask_allowed"]
PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stock_list.csv")


def load(path: str = PATH) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as file_obj:
        return list(csv.DictReader(file_obj))


def tickers(path: str = PATH) -> List[str]:
    return [row["ticker"] for row in load(path)]


@lru_cache(maxsize=4)
def _by_ticker(path: str = PATH) -> Dict[str, Dict[str, str]]:
    return {row["ticker"].upper(): row for row in load(path)}


def ask_note(symbol: str, path: str = PATH) -> Optional[str]:
    """Why a stock can't (or might not) go in an Aksjesparekonto; None when it can (#107).

    Stocks in the list use `ask_allowed`. Others: no exchange suffix means a US
    listing, which is never allowed; anything else is unknown, so "check first".
    """
    symbol = symbol.strip().upper()
    row = _by_ticker(path).get(symbol)
    if row is not None:
        if row["ask_allowed"] == "yes":
            return None
        if row["isin"].startswith("US"):
            return "Not for ASK (US stock)"
        return f"Not for ASK (company registered outside the EU/EEA, ISIN {row['isin'][:2]})"
    if "." not in symbol:
        return "Not for ASK (US stock)"
    return "ASK: check first (not in the stock list)"
