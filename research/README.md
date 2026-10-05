# Analytics research

Scripts behind the October 2026 review of whether the trade screen, or any
simple price or earnings signal, beats holding the same stocks. They are
throwaway research code: they read a pickle you build first and print tables.
They are not run by CI or by the bot. Results and caveats are in
[`docs/research/2026-10-analytics-findings.md`](../docs/research/2026-10-analytics-findings.md).

```bash
# 1. The 44-stock evaluation basket, ten years of daily bars
python research/fetch_basket.py /tmp/basket.pkl
python research/factor_study.py /tmp/basket.pkl /tmp/factor_results.pkl   # gates + eight factors, 5/10/20-session holds
python research/earnings_drift.py /tmp/basket.pkl                          # post-earnings drift vs SPY

# 2. S&P 500 constituents (Wikipedia list saved as sp500.csv with Symbol, Security, GICS Sector, Date added)
python research/fetch_sp500.py sp500.csv /tmp/sp500.pkl
python research/momentum_sp500.py /tmp/sp500.pkl sp500.csv                 # monthly 12-1 momentum and reversal deciles
# The same two scripts work for the S&P 400 + 600 lists (a CSV with a blank 'Date added' column)

# 3. Point-in-time membership, and how much of it can still be priced
python research/pit_members.py /tmp/pit_members.pkl                        # 47 quarter-end constituent lists from Wikipedia revisions (~20 min, rate-limited)
python research/pit_coverage.py /tmp/pit_members.pkl sp500_wiki.html docs/research/data/sp500-pit-coverage.csv
```

`pit_coverage.py` needs the current-members page saved alongside the snapshots
(`curl -A '<something identifying>' 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies' -o sp500_wiki.html`);
`pd.read_html` on the live URL gets a 403.

Needs `yfinance`, `pandas`, `numpy` (the bot's requirements); `lxml` is only needed
to read the Wikipedia table when you build `sp500.csv`.
