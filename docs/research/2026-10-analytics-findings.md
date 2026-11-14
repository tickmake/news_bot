# Does the screen, or any simple signal, beat holding the same stocks?

October 2026. Follows the findings in issue #12 (gates show no edge over an
ungated baseline). Scripts: [`research/`](../../research/README.md).

## Method

- **Entry and exit:** signal at a completed close, enter at the next session's open,
  exit at the close N sessions later (monthly rebalances for the S&P 500 test);
  10 bps per side.
- **Comparison:** the same-date average of the universe, so the number reported is
  the picks' *excess* over simply holding everything, in percentage points.
- **Development vs holdout:** the first 70% of dates to judge, the last 30% looked at
  once. t-values use non-overlapping samples for the standard error.

## 1. The 44-stock evaluation basket, ten years (2017-2026)

Development-period excess return over the same-date universe average (pp per
holding period):

| Signal | 5 sessions | 10 sessions | 20 sessions |
|---|---|---|---|
| The bot's current gates | -0.08 | **-0.17 (t -2.7)** | -0.20 |
| 12-month momentum (12-1) | -0.05 | -0.03 | +0.05 |
| 6-month momentum | -0.10 | -0.13 | -0.21 |
| 3-month momentum | -0.13 (t -2.3) | -0.21 | -0.34 |
| Near the 52-week high | -0.12 | -0.24 | -0.54 |
| Low volatility (60 days) | -0.06 | -0.16 | -0.31 |
| Weekly reversal | +0.05 | +0.05 | +0.07 |
| Monthly reversal | -0.03 | +0.01 | +0.21 (t 1.2) |
| Momentum + 52-week high + low-vol | -0.12 | -0.23 | -0.42 |

Nothing beats the universe average. The current gates are significantly *worse* at a
10-session hold: they pick stocks that have already run up, and those mean-revert.
12-month momentum had the only holdout result worth watching (+0.33 pp at 10
sessions, +0.64 at 20, t about 1.1 to 1.8), not significant.

## 2. Post-earnings drift

1,650 earnings events across 43 stocks, entered after the reaction (second session
after the report date) and measured against SPY. Beat above 5% minus miss below 5%:
+0.03 pp at 20 sessions (t 0.07), and the sign flips between the first and second half
of the sample (+1.04 then -1.09 pp). No drift to trade in these mega caps.

## 3. 12-month momentum on about 500 stocks, monthly rebalanced

Today's S&P 500 constituents, each eligible only from the date it joined the index
(this removes the worst survivorship bias, buying stocks that were only added after
they had risen; stocks that were later *removed* are still missing, so some bias
remains). Top decile by 12-1 momentum vs the equal-weighted universe, 10 bps per side
on turnover:

| Period | Months | Top decile vs universe | Top minus bottom |
|---|---|---|---|
| Development (2016-2023) | 84 | -0.20 pp/month (t -0.74) | -0.77 (t -1.14) |
| Holdout (2023-2026) | 35 | +1.37 pp/month (t +1.64) | +1.34 (t +1.12) |
| All | 119 | +0.26 pp/month (t +0.82) | -0.15 (t -0.25) |

By year the top decile was negative or flat every year from 2016 to 2021 and positive
from 2022 on, with 2026 (+3.08 pp/month) carrying much of the holdout. Monthly
reversal was not significant in either period.

## 4. The same test on small and mid caps

Current S&P 400 and S&P 600 members (992 stocks with data), same monthly 12-1
momentum and one-month reversal deciles, 10 bps per side. The Wikipedia lists carry
no join dates, so **no survivorship control is possible here** and the numbers are
flattering: the equal-weighted universe itself "returned" +1.7% a month.

| Period | Momentum top decile vs universe | One-month reversal top decile vs universe |
|---|---|---|
| Development | +0.33 pp/month (t +0.55) | +4.29 (t +1.21) |
| Holdout | +0.66 (t +0.90) | -0.19 (t -0.46) |
| All 119 months | +0.42 (t +0.91, better in 60% of months) | +2.97 (t +1.18) |

Momentum was positive in ten of eleven years, steadier than in large caps, but never
significant, and a 10 bps cost is optimistic for small caps. The reversal result is
large and noisy in development and vanishes in the holdout, which is what survivors-only
data produces (losers that happened to survive bounce). Neither can be validated
without point-in-time constituents and realistic costs.

## 5. Point-in-time constituents: how big is the survivorship hole?

October 2026 follow-up to the open item in issue #12. Sections 3 and 4 above are
caveated by survivorship, so the question is whether that caveat can be removed.
It splits in two, and only one half is fixable for free.

**Membership is reconstructable.** The article's "selected changes to the list"
table -- what the usual recipe walks backwards from today's members -- is no longer
in the page. Instead `research/pit_members.py` asks the MediaWiki API for the
article's own revision at each quarter end and parses the constituents table out
of that revision, which is true membership for the date including names later
removed: 47 quarters, 2015-03 to 2026-09, 502-505 symbols each, 782 distinct
symbols in all, of which **279 have left the index**.

**Prices for dead listings are not obtainable.** Yahoo drops daily history when a
listing dies, so only **105 of those 279 departed names (38%)** still return usable
bars; the other 174 -- ATVI, SIVB, FRC, TWTR, XLNX, CERN, DISCA, ANTM and so on --
return nothing at all. Knowing SIVB was in the index in 2022 does not let you price
it. Stooq, the usual free fallback, now gates its CSV endpoint behind a JavaScript
proof-of-work challenge for every symbol, including live ones.

So `research/pit_coverage.py` measures the hole rather than correcting it
(`docs/research/data/sp500-pit-coverage.csv`):

| Year | True members | Unpriceable today | Share of the index |
|---|---|---|---|
| 2015 | 503 | 129 | **25.6%** |
| 2016 | 505 | 113 | 22.4% |
| 2017 | 505 | 102 | 20.1% |
| 2018 | 505 | 88 | 17.3% |
| 2019 | 505 | 74 | 14.6% |
| 2020 | 505 | 60 | 11.9% |
| 2021 | 505 | 51 | 10.0% |
| 2022 | 504 | 37 | 7.3% |
| 2023 | 503 | 26 | 5.2% |
| 2024 | 503 | 19 | 3.7% |
| 2025 | 503 | 13 | 2.5% |
| 2026 | 503 | 3 | **0.7%** |

**This reframes section 3.** The large-cap momentum result there was "flat or negative
to 2021, positive since 2022" and was read as regime-dependence. But the data hole
falls almost perfectly along the same line: 10% or more of the index is invisible in
every year through 2021, and 7.3% or less from 2022 on. The two explanations are
confounded, and a decade-long test run on today's members is measuring a universe that
is a quarter wrong at its start and nearly right at its end. Nothing in section 3
distinguishes a momentum regime from a coverage artefact, so **the 2022-onward result
should not be treated as a finding at all** until it is rerun on data that prices
delisted names.

**What that would take:** a dataset with delisted history -- CRSP, Sharadar via Nasdaq
Data Link, Norgate, EODHD or Polygon. The membership half is now solved and committed,
so such a dataset plugs into `momentum_sp500.py` by swapping the eligibility filter for
the quarter's symbol set. Until then the `/forwardtest` archive, which records what the
live screen actually did with no survivorship question at all, is the only clean
out-of-sample read.

## 6. Sweeping the gate thresholds: do any settings work?

Issue #47 part 1. Sections 1-5 tested the gates only at their shipped values.
This sweeps them. `research/gate_sweep.py` walks the full grid of the five
gates on the 44-stock basket over ten years, same yardstick as section 1:
next-open entry, exit at the close N sessions later, 10 bps per side, excess
over the same-date average of every priced symbol.

| Gate | Shipped | Values swept |
|---|---|---|
| price above its 20-day EMA | on | on, off |
| 5-day momentum | >= 1.0% | -2, -1, 0, 0.5, 1, 2, 3, off |
| 1-day change | > 0.0% | -2, -1, 0, 0.5, 1, off |
| ATR | <= 4.5% | 2.5, 3.5, 4.5, 6, 8, off |
| drawdown from the 20-day high | <= 8% | 2, 4, 8, 12, 20, off |

That is 3,456 combinations per hold length. The result does not need a table:

| Hold | Positive in development | Positive in **both** development and holdout |
|---|---|---|
| 5 sessions | 10 of 3,456 | **0 of 3,456** |
| 10 sessions | 8 of 3,456 | **0 of 3,456** |
| 20 sessions | 0 of 3,456 | **0 of 3,456** |

Not one setting of the gates beats holding the same stocks out of sample. The
best development combination at each hold length (+0.02, +0.01, -0.05 pp) turns
negative on the holdout (-0.19, -0.11, -0.30). The shipped settings cost
-0.08 pp at 5 sessions, -0.16 at 10 and -0.19 at 20, and the picks beat the
universe average on only 45-46% of dates.

Dropping each gate in turn is also informative: every drop *improves* the
result, and the closer the gates get to admitting everything, the smaller the
damage. At 10 sessions the shipped set is -0.16 pp; without the momentum gate
-0.11; without the ATR gate -0.21 (the one gate whose removal hurts, by
admitting the most volatile names). Loosening is not an edge either -- it is
regression toward the universe, which is the thing that cannot be beaten here.

**What follows from this.** No retuning: there is nothing to tune toward. The
gates stay as an attention filter -- a way to look at eight names instead of
thirty -- and `/recommendations` keeps saying so. What changed in the product
is the part that was overclaiming: the numbered 1-5 ranking. An order between
two qualifying names cannot be justified by anything measured here, so picks
are now grouped by what *is* directly observed (volume confirmation, and
whether the name is already extended) and listed alphabetically inside each
group. The `signal_archive` still records the underlying rank, so
`/forwardtest` can keep asking whether rank predicted anything.

## 7. How often does the stop actually hold?

Issue #47 part 3. `risk.portfolio_lines` warned that "a gap can lose more"
than the 2xATR stop without saying how much more. `research/gap_risk.py`
measures it: place that stop on every session in the basket where a symbol
passed the shipped gates, then walk the hold forward session by session the
way a stop order would be hit, filling at the open when the open is already
through the stop.

| | |
|---|---|
| qualifying entries | 26,645 |
| stopped out | 12,364 (46.4%) |
| gapped through the stop | 1,718 (13.9% of stops, 6.4% of entries) |
| extra loss when it gapped, mean | 0.93 pp on a ~4% stop (1.23x intended) |
| median / 90th percentile / worst | 0.51 / 2.29 / 10.82 pp |
| across all stops, intended vs realised | 4.03% vs 4.16% |

So the stop holds most of the time, and the tail is real but not ruinous: about
one stop in seven gaps through, costing roughly a quarter more than planned on
average, with a worst observed case near three times the intended loss. The
footer now says that instead of gesturing at it. Two related fixes shipped with
it: the entry/exit sketch now measures reward:risk against the **same** stop the
sizing line places (it used the 20-session low while the line below it placed a
volatility stop elsewhere, so one block showed two different stops), and
`risk.correlated_clusters` adds up the risk of transitively correlated picks,
since sizing otherwise treats every pick as an independent bet while
`correlated_pairs` separately proves they are not.

## 8. Hold length and sell rule after DNB fees (issue #122, November 2026)

The question changed once real trading was set for January 2027 with DNB: a 59 NOK
minimum fee each way on a ~12,000 NOK position is **0.49% per side, about 1% for a
buy and a sell**, ten times the 10 bps used above. `research/hold_exit_study.py` asks
which hold length and sell rule loses least once that is paid. No signal is involved:
every stock of `stock_list.csv` (95 of 97 had history) is bought at the next open every
5th session for ten years (2016-12 to 2026-10, 44,807 entries per row below) and sold
by one of four rules: **time** (sell at the end of the hold), **stop** (also sell at
entry - 2xATR, the stop the bot places), **wide** (the same at 3xATR) and **trail**
(2xATR below the highest close since entry). A session that opens below the stop sells
at that open.

All years, net of fees (%, per trade):

| Hold | Rule | Net mean | Net median | Worst 5% | Net > 0 | Stopped | Net per 20 sessions |
|---|---|---|---|---|---|---|---|
| 5 | time | -0.68 | -0.64 | -8.2 | 42% | - | **-2.70** |
| 10 | time | -0.28 | -0.32 | -10.8 | 47% | - | -0.56 |
| 20 | time | **+0.51** | +0.34 | -14.0 | 52% | - | **+0.51** |
| 20 | stop (2xATR) | +0.09 | -2.38 | -9.6 | 43% | 46% | +0.09 |
| 20 | wide (3xATR) | +0.31 | -0.25 | -11.9 | 49% | 29% | +0.31 |
| 20 | trail | -0.10 | -2.09 | -8.6 | 37% | 73% | -0.10 |

"Net per 20 sessions" fills the same four weeks with back-to-back trades of that length,
so it compares hold lengths fairly.

What holds in the first part (before Nov 2021), the check part (after), Oslo alone, and
year by year:

1. **Short holds lose to fees.** A 5-session trade earns +0.3% before fees and pays
   ~1%. Filling four weeks with 5-session trades costs **-2.7%**; one 20-session trade
   makes **+0.5%**. The 20-session hold beat the 5-session hold in every year from
   2017 to 2026, in both halves (-2.84 vs +0.42 and -2.58 vs +0.60) and in Oslo alone
   (-2.67 vs +0.61). This settles the owner's hold length: **2 to 4 weeks**.
2. **The 2xATR stop costs more than it saves on a 4-week hold.** It is hit in 46% of
   20-session trades, so a normal wobble sells the stock and the fee is paid for
   nothing; the average falls from +0.51% to +0.09%. It does cut the worst 5% of
   trades from -14.0% to -9.6%. The trailing stop is worse on average than any other
   rule. 15% of stops gap through, as in section 7.
3. **A 3xATR stop is the middle ground:** hit in 29% of trades, keeps +0.31% on
   average, worst 5% at -11.9%. Without a stop the average was better in 9 of 11 years.
   But this test can't see the companies that collapsed and left the list, which is
   exactly where a stop helps most, so "no stop" is flattered here. The recommendation
   is a **3xATR stop for 2-4 week holds**, treated as insurance that costs about
   0.2 pp per trade, not as a way to earn more.

Two consequences for the bot, not changed in this PR because they change the advice
and sizing the owner sees:

- With a 3xATR stop, the fixed 0.5%-risk sizing gives positions of about 8% of a
  50,000 NOK account (~4,000 NOK) for a typical 2% daily-swing stock, and at that size
  the 59 NOK minimum is nearly 3% round trip. Sizing and fees pull in opposite directions;
  `/check` shows both so the owner can see it, and the settings
  (`RISK_STOP_ATR_MULTIPLE`, `RISK_PER_TRADE_PCT`) are an owner decision.
- `/forwardtest` and `/pickperformance` judge picks over 5 sessions
  (`TRADE_OUTCOME_LOOKBACK_DAYS=5`), which this section shows is the hold that loses
  most to fees. They should judge the hold the owner actually uses (about 20).

Caveats: today's list only (failed companies are missing, which flatters every rule
and "no stop" most); the levels include ten years of a rising market, so compare rules
with each other, not with zero; entries 5 sessions apart overlap on 10- and 20-session
holds, so there are fewer independent results than rows; currency exchange fees on
non-NOK stocks are not included in the table above. A later sensitivity (P2-5) adds them
at 0, 10, 25 and 50 bps per conversion, as illustrative levels and not a broker's prices:
the order of the rules and holds stays the same, and the levels fall for EUR and USD stocks
(the 20-session "time" mean goes from +0.51 to +0.39, +0.21 and -0.09). See
`docs/audit/remediation-roadmap.md` (P2-5) and `data/hold_exit_study_fx.txt`.

## 9. Portfolio rules for 4 stocks held at once (issue #125, November 2026)

`research/portfolio_rules_study.py` asks whether three simple rules cut the bad months
of a 4-stock portfolio. No signal is involved, as in section 8. Every 20 sessions from 2017
to 2026-09 (125 periods), 4 random stocks from `stock_list.csv` (96 with history) are held
for 20 sessions, net of the 59 NOK fee each way. That's 400 random draws per period. Each
rule picks from the same random order, skipping what it forbids, so the comparison is
paired.

| Rule | Net mean | Worst 5% of months | Months below -5% | vs random: mean | vs random: worst 5% |
|---|---|---|---|---|---|
| random 4 | +0.54 | -9.27 | 15.8% | - | - |
| one per sector | +0.51 | -9.04 | 15.5% | -0.03 | **+0.24** |
| no report in the first 7 days | +0.53 | -9.33 | 15.9% | -0.01 | -0.05 |
| no pair correlated > 0.7 | +0.54 | -9.24 | 15.7% | -0.00 | +0.03 |
| all three | +0.48 | -9.07 | 15.5% | -0.05 | +0.21 |

To pass, a rule had to lift the worst 5% by at least 0.1 points at a cost of at most
0.1 points of mean, in both the first part (before 2021-11) and the check part.

1. **One stock per sector passes, just.** The worst 5% of months is +0.28 points better
   in the first part and +0.20 in the check part, at a cost of +0.03 and -0.09 points of
   mean. It changes the portfolio in 57% of draws. It stays a warning in `/check`.
2. **Avoiding earnings weeks does not help.** The tail is slightly worse in both parts
   (-0.05, -0.02). A report moves a stock both ways, so skipping it removes good surprises
   as often as bad ones. `/check` now shows the date as information, not a warning.
3. **The correlation rule almost never matters.** Fewer than 1% of stock pairs correlated
   above 0.7 over 60 sessions, so it changed only 10% of portfolios, and the effect is
   noise (+0.01 / +0.05). `/check` still shows a high correlation, without a warning.
4. **Sizes are small.** Even the sector rule moves the worst month by about a quarter of
   a point out of -9%. With 4 stocks, a bad month is mostly the market, and no
   diversification rule removes that.

Caveats: today's stock list (survivors only), random entries rather than the bot's picks,
and no currency fees.

## 10. Do the four company scores pick better stocks? (Phase 3a, #79, November 2026)

`research/score_backtest.py` reads the bot's own database after loading 10 years of
prices and annual reports back to the 2020 accounts (PR #156). Every month end, the stocks
are split into thirds by each score (0-100 within the sector, only from what was public
that day). Each stock is bought at the next open and sold 20 sessions later, after
DNB fees. The pass rules were written into the script before the first run:
1. The top third beats all stocks in both the first and the check part.
2. That difference has t >= 2 over all months.
3. The top third beats the bottom third.

| Score | Months | Top third vs all (per month) | t | Top vs bottom | Top beats all, % of months | Passes |
|---|---|---|---|---|---|---|
| Quality | 66 (2021-04 to 2026-09) | -0.31 | -1.3 | -0.43 | 35% | no, **backwards** |
| Value | 42 (2023-04 on) | +0.32 (first part -0.03, check +0.67) | 1.6 | +0.82 | 62% | no |
| Performance | 54 | -0.29 | -1.2 | -0.51 | 41% | no, backwards |
| Momentum | 107 (2017-11 on) | +0.18 (+0.08, +0.27) | 1.0 | +0.32 | 51% | no |
| All four averaged | 42 | -0.41 | -1.7 | -0.47 | 40% | no |

1. **No score passes.** On today's ~97 stocks, no score picked a top third that beat
   the average stock reliably.
2. **Quality and performance point the wrong way.** Stocks that were profitable, low on
   debt and growing did *worse* than average over the next month, most clearly in Oslo
   (quality top vs all -0.66, t -2.0). Over one month, the market had already priced
   good companies, and the cheap, weak ones bounced. This matches the usual finding that
   quality pays, if at all, over years, not weeks.
3. **Value is the only near-miss.** It was flat in the first half and strong in the
   second (+0.67 a month, t 2.7), mostly in the EU (t 2.2). Half of one cycle is not
   proof: it is the 2025-26 rotation into cheap stocks as much as a rule. It has only 42
   months, because share counts come from Yahoo, which keeps 4 years.
4. **Momentum is weakly positive everywhere** (Oslo, EU, US, both halves), as in the S&P
   500 study in section 4, but the size (+0.2 a month) is far too small to pay for itself
   on its own.

Caveats: survivors only (Yahoo has no prices for Kahoot, Adevinta, XXL, REC Silicon,
Golden Ocean, Schibsted and every other stock that left); ~30 stocks per third; one
regime. The bias would flatter a quality score, since failed companies would have scored
low, so quality's negative result is not caused by it.

**What it means:** the scores stay in `/analyze` as descriptions of a company, not as
signals. A high quality score says the business is sound, not that the share will beat
others next month. The 6-month forward test (#79) is not worth running on the scores as
they are. If anything is followed up, it is value in the EU, re-tested in 2027 with
another year of data.

### 10b. Rerun with ESEF numbers dated by their filing (P1-8, 2026-11-14)

The table above used ESEF numbers whose `known_from` was capped at year end + 120
days. A report that filings.xbrl.org added later therefore counted as known up to
several months, or in one case years, before it was on file. P1-8 dates each number by
its filing's `date_added`, and uses year end + 120 days only when that date is missing
or unusable. The table above is kept as it was. Its output, regenerated from the same
database, is in `docs/research/data/score_backtest_before_p1-8.txt`.

**Method:**
- Two copies were made of the Pi's 2026-11-14 database backup.
  1. **Before:** left as it was.
  2. **After:** re-dated with `research/esef_dates_rerun.py redate`. This runs the bot's
     own `company_numbers._check_saved_dates` against the live filings.xbrl.org metadata
     and loads nothing new.
- Both copies were rebuilt with `score_inputs` and `company_scores` as of 2026-11-14,
  using one cached set of exchange rates.
- `research/score_backtest.py` then ran on each copy.
- The "before" rerun matches the original output line for line. So the only difference
  between the two runs is the corrected `known_from`, not prices, exchange rates,
  code or stock list.
- Outputs:
  - `docs/research/data/score_backtest_after_p1-8.txt`
  - `docs/research/data/esef_dates_p1-8_compare.txt`

**What the correction changed in the data:**
- **Rows re-dated:** 1,150 of 2,920 ESEF rows, every one of them later, none earlier.
  - The median move is 9 days.
  - 717 moved by less than a month, and 6 by more than five years. Santander's 2020
    report was added to filings.xbrl.org on 2026-10-20, so its numbers now count from
    that day.
- **No row was guessed.**
  - Every row of a listed stock was matched to its filing. 2,832 rows now record theirs.
  - The 88 rows of the four stocks dropped in #152 (ACA.PA, G.MI, PST.MI, WKL.AS) are
    left unchecked. Nothing scores them.
- **Unchanged:** Yahoo rows, values, currencies and fetch times.
- **Availability:** ESEF rows known at month ends, summed over all month ends, fell from
  96,634 to 92,934 (−3.8%).
- **`score_inputs`:**
  - Stock-months with any numbers fell from 5,118 to 5,013.
  - 426 stock-months changed their numbers, and 104 of those changed which year is the
    newest.
  - One stock-month (CMBTO.OL, 2021-04-30) had nothing known any more and lost its row.
- **Scores present:**
  - Quality 5,056 → 4,678, value 4,109 → 3,786, performance 3,878 → 3,611, momentum
    unchanged at 9,772.
  - Quality changed in 1,305 stock-months, value in 1,006, performance in 941.
  - Coverage falls partly because, between a year's deadline and its late `date_added`,
    a stock can have that year's Yahoo items but not yet its ESEF items. The newest year
    is then incomplete, and some metrics drop out. That is point-in-time correct, just
    thinner.

| Score | Months | Top third vs all (first part, check part) | t | Top vs bottom | Top beats all, % of months | Passes |
|---|---|---|---|---|---|---|
| Quality, before | 66 | -0.31 (-0.36, -0.26) | -1.33 | -0.43 | 35% | no |
| Quality, after | 65 (from 2021-05) | -0.26 (-0.28, -0.24) | -1.11 | -0.11 | 35% | no |
| Value, before | 42 | +0.32 (-0.03, +0.67) | 1.64 | +0.82 | 62% | no |
| Value, after | 42 | +0.38 (-0.06, +0.82) | 1.77 | +0.81 | 62% | no |
| Performance, before | 54 | -0.29 (-0.14, -0.43) | -1.17 | -0.51 | 41% | no |
| Performance, after | 54 | -0.30 (-0.19, -0.41) | -1.23 | -0.61 | 43% | no |
| Momentum, before and after | 107 | +0.18 (+0.08, +0.27) | 0.97 | +0.32 | 51% | no |
| All four, before | 42 | -0.41 (-0.45, -0.36) | -1.74 | -0.47 | 40% | no |
| All four, after | 42 | -0.40 (-0.55, -0.26) | -1.61 | -0.40 | 43% | no |

**What changes in the conclusions:**
- **Still no score passes.** Every verdict is the same, and so is the conclusion above.
- **Quality is less clearly backwards.**
  - Top vs bottom is -0.11, against -0.43 before.
  - The Oslo result is now slightly stronger the wrong way: top vs all -0.75, t -2.16.
  - Part of the earlier "backwards" result rested on numbers used before they were filed.
- **Value is still the only near-miss, and still fails rule 1.** Its first part stays
  negative. The check part is a little stronger (+0.82, t 2.74), and the EU result is
  unchanged (+0.78, t 2.22).
- **Momentum uses no company numbers,** so it is identical.

The two runs are comparable only as a pair from the same database. They are not
comparable with any future run on new data. The original section-10 numbers above were
computed with back-dated ESEF numbers, and they stay only as the record of that run.

## 11. Gap risk for the stop the bot actually places (issue #161, November 2026)

Section 7 measured a 2xATR stop placed below the **buy price**. The bot places its stop
below the **close the signal was formed on** (`risk.size_position`), and the Pi runs
3xATR. So the footer quoted numbers for a stop that was never the live one.

`research/gap_risk.py` now takes the multiple and the anchor, and screens sessions the way
the live screen does: the five gates (volume is not one), 30 closes needed, and EMA20
seeded at the start of the 3 months of bars the screen fetches (section 7 seeded it ten
years back; that changes one entry in 26,000). A test checks the study's gates against
`indicators._failed_gates` session by session.

**Units.** When a stop gaps, the sale is at the open, below the stop price. The study now
records that shortfall in **percent of the stop price**. Section 7 recorded it in points of
the buy price, which depends on what was paid; with a stop measured from the signal close,
adding those points to the stop distance mixes two reference prices. The shortfall below
the stop price depends on neither, so the bot can apply it before anyone has bought:

    gap sale  = stop × (1 − shortfall)
    gap loss  = 1 − gap sale / reference      (reference = the signal close)

Same basket (44 stocks, ten years to 2026-11-13), 20-session hold. Outputs in
`docs/research/data/gap_risk_*x_signal-close.txt`.

| Stop | Entries | Stopped | Gapped (of stops) | Sale below the stop when gapped, % of stop price: mean / 90th / worst | Same in points of the buy price |
|---|---|---|---|---|---|
| 2xATR below the buy price, section 7 method | 26,448 | 46.7% | 14.0% | 0.98 / 2.37 / 11.62 | 0.93 / 2.26 / 10.82 |
| 2xATR below the signal close | 26,407 | 46.0% | 14.3% | 0.99 / 2.36 / 12.04 | 0.94 / 2.24 / 11.22 |
| **3xATR below the signal close (live)** | 26,438 | 29.0% | 13.7% | **1.09 / 2.75 / 12.75** | 1.02 / 2.55 / 11.94 |
| 3xATR below the signal close, volume 1.2x or more | 6,980 | 28.7% | 12.8% | 1.27 / 3.21 / 12.75 | 1.18 / 2.96 / 11.94 |

What this says:

- The section-7 method reproduces section 7 in its own units (0.93 / 2.26 / 10.82 against
  0.93 / 2.29 / 10.82 then; entries differ because 30 closes are now required).
- A 3xATR stop is hit less often (29% against 46%), but when it is hit it gaps about as
  often: **about 1 stop in 7**. The sale then comes 1.1% below the stop price on average,
  less than 2.75% below in nine of ten, 12.75% at worst.
- Anchoring to the signal close makes the real distance from the buy price 0.07 points
  wider on average, and wider than stated in 57% of entries. 11 entries opened at or below
  the stop and are counted as no trade.
- The "confirmed by volume" rows are a subset, not the live screen; their shortfall is a
  little larger.

The bot keeps one row per measured stop in `risk.GAP_STUDIES` (the two signal-close rows
without the volume filter); a test checks them against the saved outputs. A multiple with no
row prints "not measured". The "if it gaps through" figure uses the 90th-percentile
shortfall and is labelled an estimate, not a maximum. All loss figures are modelled as if
bought at the signal close: the actual loss on a position depends on its buy price, which
the bot does not have for a signal.

## Conclusions

1. Daily price gates, classic factors and earnings surprises show **no reliable edge**
   over holding the same stocks in this data. Treat `/recommendations` as a screen.
   Section 6 closes the obvious follow-up question: it is not a matter of the wrong
   thresholds. None of the 3,456 settings of the five gates is positive out of sample
   at any hold length, so there is nothing to tune toward, and the ranking that implied
   otherwise has been replaced by tiers.
2. 12-month momentum looked **regime-dependent in large caps** (nothing before 2022,
   strong since) and **steadier but unproven in small and mid caps**. Section 5 undercuts
   the first half: the share of the index that cannot be priced today falls from 26% in
   2015 to 0.7% in 2026, tracking the same 2022 boundary, so regime and data coverage are
   confounded. Treat neither as a finding. A hypothesis to follow forward (the
   `/forwardtest` archive records what the live screen does) and to retest on data that
   prices delisted names, not a rule to ship.
3. What does not depend on a signal working is **how much to risk, whether the picks are
   one bet, and what events fall inside the hold**. That is what the risk and
   relative-strength lines in `/recommendations` and `/analyze` provide. Section 7 puts
   measured numbers behind two of its disclaimers: the stop gaps through about one time
   in seven (section 11 repeats this for the 3xATR stop the bot places), and correlated picks now have their combined loss stated rather than implied.
4. With real DNB fees, **hold length matters more than any signal tested here**: a
   5-session trade loses about 0.7% to fees on average, a 20-session trade makes about
   +0.5% (section 8). A 2xATR stop is hit in almost half of 4-week holds; 3xATR is the
   better trade-off.

## Caveats

- Sections 1-4 use today's index members only; delisted and acquired stocks are absent,
  and removals from the index are not modelled. The small/mid-cap test has no join dates
  at all. Section 5 quantifies what this costs: up to a quarter of the 2015 index.
- Equal-weighted universes; no sector neutralisation; one regime-heavy decade.
- Holds overlap, so the t-values are rough. Costs are a flat 10 bps per side.
- Daily bars only; nothing here tests intraday, options, fundamentals beyond earnings
  surprise, or news.
