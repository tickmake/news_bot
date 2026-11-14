import pickle
import sys

import pandas as pd
import yfinance as yf

t = pd.read_csv(sys.argv[1])
syms = [s.replace(".", "-") for s in t["Symbol"]]
out = {}
for i in range(0, len(syms), 50):
    chunk = syms[i:i+50]
    df = yf.download(chunk, period="11y", interval="1d", auto_adjust=True, group_by="ticker", threads=True, progress=False)
    for s in chunk:
        try:
            h = df[s][["Open", "Close"]].dropna()
            if len(h) > 300:
                out[s] = h
        except Exception:  # noqa: S110 - a symbol missing from the download is left out; the running count is printed
            pass
    print(i, len(out), flush=True)
pickle.dump(out, open(sys.argv[2], "wb"))
print("done", len(out))
