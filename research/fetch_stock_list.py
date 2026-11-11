"""Ten years of daily bars for every stock in stock_list.csv (#122).

    python research/fetch_stock_list.py /tmp/stock_list_10y.pkl

Prices are split- and dividend-adjusted (auto_adjust), so a return is what a
holder got. Saves (bars by ticker, stock_list rows by ticker).
"""
import csv
import os
import pickle
import sys

import yfinance as yf

LIST_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stock_list.csv")


def main(out_path):
    with open(LIST_PATH, encoding="utf-8", newline="") as handle:
        meta = {row["ticker"]: row for row in csv.DictReader(handle)}
    bars = {}
    for symbol in meta:
        try:
            history = yf.Ticker(symbol).history(period="10y", interval="1d", auto_adjust=True)
        except Exception as exc:
            print("fail", symbol, exc)
            continue
        if len(history) > 250:
            bars[symbol] = history
        else:
            print("short", symbol, len(history))
    with open(out_path, "wb") as handle:
        pickle.dump((bars, meta), handle)
    print(len(bars), "of", len(meta), "symbols;", min(len(h) for h in bars.values()), "min bars")


if __name__ == "__main__":
    main(sys.argv[1])
