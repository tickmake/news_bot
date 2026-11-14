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

# 4. Are the gate thresholds worth anything, and does the stop hold? (issue #47)
# Both read the same basket pickle as step 1.
python research/gate_sweep.py /tmp/basket.pkl /tmp/gate_sweep.pkl          # 3,456 threshold combinations x 3 hold lengths
python research/gap_risk.py /tmp/basket.pkl 2.0 entry-open --full-history-ema  # how often a 2xATR stop gaps through (section 7)
python research/gap_risk.py /tmp/basket.pkl 3.0 signal-close               # the stop the bot places (section 11; output in docs/research/data/)

# 5. Hold length and sell rule after DNB fees (issue #122)
python research/fetch_stock_list.py /tmp/stock_list_10y.pkl                # stock_list.csv, ten years
python research/hold_exit_study.py /tmp/stock_list_10y.pkl                 # 5/10/20 sessions x time/stop/wide/trail
# Both gap_risk.py and hold_exit_study.py end with an FX cost sensitivity (P2-5): 0/10/25/50 bps per
# conversion for non-NOK stocks, from research/fx_costs.py. Illustrative levels, not a broker's prices.

# 7. The four company scores on history (Phase 3a, #79). Needs a copy of the bot's database
#    with 10 years of prices (PR #156): see "Backups off the Pi" in the main README.
python research/score_backtest.py /tmp/news_bot.db                          # top third vs all vs bottom third
#    Section 10b: the same with ESEF numbers dated by their filing (P1-8). Copies only; see the script.
python research/esef_dates_rerun.py redate  /tmp/after.db                   # the bot's own re-dating, needs filings.xbrl.org
python research/esef_dates_rerun.py rebuild /tmp/before.db /tmp/fx.pkl      # score_inputs + company_scores, cached FX
python research/esef_dates_rerun.py rebuild /tmp/after.db  /tmp/fx.pkl
python research/esef_dates_rerun.py compare /tmp/before.db /tmp/after.db

# 6. Portfolio rules for 4 stocks held at once (issue #125)
python research/portfolio_rules_study.py /tmp/stock_list_10y.pkl /tmp/earnings_dates.pkl   # sector / earnings / correlation
```

`pit_coverage.py` needs the current-members page saved alongside the snapshots
(`curl -A '<something identifying>' 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies' -o sp500_wiki.html`);
`pd.read_html` on the live URL gets a 403.

Needs `yfinance`, `pandas`, `numpy` (the bot's requirements); `lxml` is only needed
to read the Wikipedia table when you build `sp500.csv`.
