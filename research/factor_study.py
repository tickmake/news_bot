import pickle, sys, math
import numpy as np, pandas as pd
data, meta = pickle.load(open(sys.argv[1], "rb"))
syms = [s for s in data if s != "SPY"]
def norm(h):
    h = h.copy(); h.index = pd.to_datetime(h.index.strftime("%Y-%m-%d")); return h[~h.index.duplicated()]
H = {s: norm(data[s]) for s in syms}
dates = sorted(set().union(*[set(h.index) for h in H.values()]))
idx = pd.DatetimeIndex(dates)
C = pd.DataFrame({s: H[s]["Close"] for s in syms}).reindex(idx)
O = pd.DataFrame({s: H[s]["Open"] for s in syms}).reindex(idx)
Hh = pd.DataFrame({s: H[s]["High"] for s in syms}).reindex(idx)
Lo = pd.DataFrame({s: H[s]["Low"] for s in syms}).reindex(idx)
COST = 2 * 10 / 1e4

# signals computed on each symbol's own bar history (ffill only for rolling windows, NaN where no bar that day)
Cf = C.ffill(limit=3)
ret1 = Cf.pct_change(fill_method=None)
sig = {}
sig["mom_12_1"] = Cf.shift(21) / Cf.shift(252) - 1
sig["mom_6_1"] = Cf.shift(21) / Cf.shift(126) - 1
sig["mom_3m"] = Cf / Cf.shift(63) - 1
sig["near_52w_high"] = Cf / Cf.rolling(252, min_periods=200).max()
sig["low_vol_60d"] = -ret1.rolling(60, min_periods=50).std()
sig["rev_1w"] = -(Cf / Cf.shift(5) - 1)
sig["rev_1m"] = -(Cf / Cf.shift(21) - 1)
sig["above_200dma"] = Cf / Cf.rolling(200, min_periods=180).mean() - 1
# the bot's current gates as a boolean "selection"
ema20 = Cf.ewm(span=20, adjust=False).mean()
tr = pd.concat([(Hh - Lo).stack(), (Hh - Cf.shift(1)).abs().stack(), (Lo - Cf.shift(1)).abs().stack()], axis=1).max(axis=1).unstack()
atr = tr.rolling(14).mean() / Cf * 100
dd = (Cf.rolling(20).max() - Cf) / Cf.rolling(20).max() * 100
gate = (Cf > ema20) & ((Cf / Cf.shift(5) - 1) * 100 >= 1.0) & (ret1 * 100 > 0) & (atr <= 4.5) & (dd <= 8.0)
# combo: average cross-sectional z of momentum, 52w-high proximity and low vol
def z(df): return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)
sig["combo_mom_high_lowvol"] = (z(sig["mom_12_1"]) + z(sig["near_52w_high"]) + z(sig["low_vol_60d"])) / 3

def fwd(h):
    # enter next bar open, exit close h bars after entry (per-symbol bars), net of costs
    out = pd.DataFrame(index=idx, columns=syms, dtype=float)
    for s in syms:
        bars = H[s]; pos = {d: i for i, d in enumerate(bars.index)}
        o, c = bars["Open"].values, bars["Close"].values
        vals = []
        for d in idx:
            i = pos.get(d)
            if i is None or i + h >= len(bars): vals.append(np.nan); continue
            e = i + 1; x = e + h - 1
            vals.append(c[x] / o[e] - 1 - COST)
        out[s] = vals
    return out

split = int(len(idx) * 0.7)
periods = {"development": (idx[260], idx[split]), "holdout": (idx[split + 1], idx[-1])}
def evaluate(sel_fn, F, h, lo, hi):
    rows = []
    for d in idx[(idx >= lo) & (idx <= hi)]:
        f = F.loc[d].dropna()
        if len(f) < 20: continue
        chosen = sel_fn(d, f.index)
        if chosen is None or len(chosen) < 3: continue
        rows.append((d, f.loc[chosen].mean() - f.mean(), f.loc[chosen].mean(), f.mean()))
    if not rows: return None
    df = pd.DataFrame(rows, columns=["d", "delta", "sel", "base"]).set_index("d")
    nonov = df.iloc[::h]["delta"]  # non-overlapping sample for the standard error
    se = nonov.std(ddof=1) / math.sqrt(len(nonov)) if len(nonov) > 2 else float("nan")
    return dict(n=len(df), delta=df["delta"].mean() * 100, t=(nonov.mean() / se) if se and se == se and se > 0 else float("nan"), base=df["base"].mean() * 100, sel=df["sel"].mean() * 100, better=(df["delta"] > 0).mean() * 100)

def top_quintile(name):
    s = sig[name]
    def fn(d, cols):
        v = s.loc[d, cols].dropna()
        if len(v) < 20: return None
        return v.nlargest(max(3, len(v) // 5)).index
    return fn
def gate_sel(d, cols):
    g = gate.loc[d, cols]; return g[g].index

results = []
for h in (5, 10, 20):
    F = fwd(h)
    strategies = {"current_gates": gate_sel, **{n: top_quintile(n) for n in sig}}
    for name, fn in strategies.items():
        for pname, (lo, hi) in periods.items():
            r = evaluate(fn, F, h, lo, hi)
            if r: results.append(dict(strategy=name, hold=h, period=pname, **r))
res = pd.DataFrame(results)
pd.set_option("display.width", 200); pd.set_option("display.max_rows", 500)
for h in (5, 10, 20):
    dev = res[(res.hold == h) & (res.period == "development")].set_index("strategy")
    ho = res[(res.hold == h) & (res.period == "holdout")].set_index("strategy")
    t = dev[["n", "sel", "base", "delta", "t", "better"]].round(2).join(ho[["delta", "t"]].round(2), rsuffix="_holdout")
    print(f"\n=== hold {h} sessions (net of 10 bps/side; delta = picks minus same-date universe average, pp) ===")
    print(t.sort_values("delta", ascending=False).to_string())
print("\nperiods:", {k: (v[0].date(), v[1].date()) for k, v in periods.items()})
res.to_pickle(sys.argv[2])
