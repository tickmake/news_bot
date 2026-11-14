import math
import pickle
import sys
import warnings

warnings.filterwarnings("ignore")
# After the filter on purpose: it also silences what these print while importing.
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

data, meta = pickle.load(open(sys.argv[1], "rb"))  # noqa: S301 - written by fetch_basket.py
syms = [s for s in data if s != "SPY"]
spy = data["SPY"]
spy.index = pd.to_datetime(spy.index.strftime("%Y-%m-%d"))
events = []
for s in syms:
    try:
        ed = yf.Ticker(s).get_earnings_dates(limit=60)
    except Exception:  # noqa: S112 - a symbol Yahoo has no earnings dates for is left out; the symbol count is printed
        continue
    if ed is None or ed.empty:
        continue
    h = data[s].copy()
    h.index = pd.to_datetime(h.index.strftime("%Y-%m-%d"))
    for ts, row in ed.iterrows():
        sur = row.get("Surprise(%)")
        if sur is None or pd.isna(sur):
            continue
        d = pd.Timestamp(ts.strftime("%Y-%m-%d"))
        if d < h.index[0] or d > h.index[-1]:
            continue
        # first trading day on/after the report date: report may be before open (reaction that day) or after close (next day).
        # To avoid look-ahead on timing, we trade from the close of the SECOND session after the date,
        # i.e. after the reaction has been fully priced either way.
        pos = h.index.searchsorted(d)
        for hold in (5, 20, 40):
            e, x = pos + 1, pos + 1 + hold
            if x >= len(h):
                continue
            sp = spy.index.searchsorted(h.index[e])
            if sp + hold >= len(spy):
                continue
            r = h["Close"].iloc[x] / h["Close"].iloc[e] - 1
            m = spy["Close"].iloc[sp + hold] / spy["Close"].iloc[sp] - 1
            events.append({"sym": s, "date": d, "hold": hold, "surprise": float(sur),
                           "excess": (r - m) * 100 - 0.2 * 1})
ev = pd.DataFrame(events)
print("events:", ev[ev.hold == 20].shape[0], "symbols:", ev.sym.nunique(), "span:", ev.date.min().date(), ev.date.max().date())
for hold in (5, 20, 40):
    e = ev[ev.hold == hold].copy()
    e["bucket"] = pd.cut(e.surprise, [-1e9, -5, 0, 5, 1e9], labels=["miss>5%", "miss<5%", "beat<5%", "beat>5%"])
    g = e.groupby("bucket", observed=True)["excess"].agg(["count", "mean", lambda x: x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))])
    g.columns = ["n", "mean_excess_pp", "t"]
    print(f"\nhold {hold} sessions, excess vs SPY, entered after the reaction (net 20 bps round trip):")
    print(g.round(2).to_string())
    top = e[e.surprise > 5]["excess"]
    bot = e[e.surprise < -5]["excess"]
    if len(top) > 5 and len(bot) > 5:
        diff = top.mean() - bot.mean()
        se = math.sqrt(top.var(ddof=1)/len(top) + bot.var(ddof=1)/len(bot))
        print(f"beat>5% minus miss>5%: {diff:+.2f} pp, t {diff/se:+.2f}")
    half = e.date.median()
    for name, part in (("first half", e[e.date <= half]), ("second half", e[e.date > half])):
        t, b = part[part.surprise > 5]["excess"], part[part.surprise < -5]["excess"]
        if len(t) > 5 and len(b) > 5:
            print(f"  {name}: {t.mean()-b.mean():+.2f} pp (n {len(t)}/{len(b)})")
