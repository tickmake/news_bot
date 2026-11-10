"""The stock list file: every stock the bot studies (#74).

`stock_list.csv` at the repo root, one row per stock. ISINs come from the
exchanges' own lists and were checked against OpenFIGI; sector, country and
currency come from Yahoo. `ask_allowed` is "yes" when the ISIN's country is
in the EEA, the rule for an Aksjesparekonto (#82) -- a guide, not a promise.
"""

import csv
import os
from typing import Dict, List

COLUMNS = ["ticker", "isin", "name", "exchange", "currency", "country", "sector", "region", "ask_allowed"]
PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stock_list.csv")


def load(path: str = PATH) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as file_obj:
        return list(csv.DictReader(file_obj))


def tickers(path: str = PATH) -> List[str]:
    return [row["ticker"] for row in load(path)]
