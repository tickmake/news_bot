import pickle, sys, math
import numpy as np, pandas as pd
data = pickle.load(open(sys.argv[1], "rb")); meta = pd.read_csv(sys.argv[2])
meta["sym"] = meta["Symbol"].str.replace(".", "-", regex=False)
added = pd.to_datetime(meta.set_index("sym")["Date added"], errors="coerce")
sector = meta.set_index("sym")["GICS Sector"]
C = pd.DataFrame({s: h["Close"] for s, h in data.items()}); O = pd.DataFrame({s: h["Open"] for s, h in data.items()})
for df in (C, O): df.index = pd.to_datetime(df.index.strftime("%Y-%m-%d"))
C = C[~C.index.duplicated()].sort_index(); O = O[~O.index.duplicated()].sort_index()
mom = C.shift(21) / C.shift(252) - 1            # 12-1 month momentum, known at the close
rev = -(C / C.shift(21) - 1)
# month-end rebalance dates
me = C.groupby([C.index.year, C.index.month]).tail(1).index
COST = 10 / 1e4                                   # per side, applied to the traded fraction
rows = []
prev_sel = {}
for k in range(len(me) - 1):
    d, nxt = me[k], me[k + 1]
    if d < C.index[260]: continue
    i = C.index.get_loc(d); e = C.index[i + 1]; x_pos = C.index.get_loc(nxt) + 1
    if x_pos >= len(C.index): continue
    x = C.index[x_pos]                             # next rebalance, exit at the next day's open
    elig = [s for s in C.columns if pd.notna(mom.at[d, s]) and pd.notna(O.at[e, s]) and pd.notna(O.at[x, s]) and (pd.isna(added.get(s)) or added.get(s) <= d)]
    # survivorship control: a stock is eligible only from the date it joined the index
    if len(elig) < 100: continue
    r = pd.Series({s: O.at[x, s] / O.at[e, s] - 1 for s in elig})
    out = {"date": d, "n": len(elig), "base": r.mean()}
    for name, sig in (("mom_12_1", mom), ("reversal_1m", rev)):
        v = sig.loc[d, elig]
        top = v.nlargest(len(v) // 10).index; bot = v.nsmallest(len(v) // 10).index
        turn = 1.0 if name not in prev_sel else 1 - len(set(top) & prev_sel[name]) / len(top)
        prev_sel[name] = set(top)
        out[name + "_top"] = r[top].mean() - 2 * COST * turn
        out[name + "_bot"] = r[bot].mean()
    rows.append(out)
df = pd.DataFrame(rows).set_index("date")
split = df.index[int(len(df) * 0.7)]
def summ(part, label):
    for name in ("mom_12_1", "reversal_1m"):
        d_top = (part[name + "_top"] - part["base"]) * 100
        ls = (part[name + "_top"] - part[name + "_bot"]) * 100
        t = d_top.mean() / (d_top.std(ddof=1) / math.sqrt(len(d_top)))
        tl = ls.mean() / (ls.std(ddof=1) / math.sqrt(len(ls)))
        print(f"{label:12s} {name:12s} months {len(part):3d} | top decile vs universe {d_top.mean():+.2f} pp/mo (t {t:+.2f}, better {np.mean(d_top>0)*100:.0f}%) | top-bottom {ls.mean():+.2f} pp/mo (t {tl:+.2f}) | universe {part['base'].mean()*100:+.2f}%/mo")
print("universe size per month: min", df.n.min(), "max", df.n.max(), "| split", split.date())
summ(df[df.index <= split], "development"); summ(df[df.index > split], "holdout"); summ(df, "all")
# per-year stability for the momentum top decile
yr = ((df["mom_12_1_top"] - df["base"]) * 100).groupby(df.index.year).mean().round(2)
print("by year (mom top decile vs universe, pp/month):", yr.to_dict())
