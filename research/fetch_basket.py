import csv, pickle, sys
import yfinance as yf
syms=[r["symbol"] for r in csv.DictReader(open("/Users/sunil/lab/news_bot/evaluation_universe.csv"))]
meta={r["symbol"]:r for r in csv.DictReader(open("/Users/sunil/lab/news_bot/evaluation_universe.csv"))}
out={}
for s in syms+["SPY"]:
    try:
        h=yf.Ticker(s).history(period="10y",interval="1d",auto_adjust=True)
        if len(h)>500: out[s]=h
        else: print("short",s,len(h))
    except Exception as e: print("fail",s,e)
pickle.dump((out,meta),open(sys.argv[1],"wb"))
print(len(out),"symbols;", min(len(h) for h in out.values()), "min bars;", max(h.index[-1] for h in out.values()))
