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
   in seven, and correlated picks now have their combined loss stated rather than implied.

## Caveats

- Sections 1-4 use today's index members only; delisted and acquired stocks are absent,
  and removals from the index are not modelled. The small/mid-cap test has no join dates
  at all. Section 5 quantifies what this costs: up to a quarter of the 2015 index.
- Equal-weighted universes; no sector neutralisation; one regime-heavy decade.
- Holds overlap, so the t-values are rough. Costs are a flat 10 bps per side.
- Daily bars only; nothing here tests intraday, options, fundamentals beyond earnings
  surprise, or news.
