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

## Conclusions

1. Daily price gates, classic factors and earnings surprises show **no reliable edge**
   over holding the same stocks in this data. Treat `/recommendations` as a screen.
2. 12-month momentum looked **regime-dependent in large caps** (nothing before 2022,
   strong since) and **steadier but unproven in small and mid caps**. Section 5 undercuts
   the first half: the share of the index that cannot be priced today falls from 26% in
   2015 to 0.7% in 2026, tracking the same 2022 boundary, so regime and data coverage are
   confounded. Treat neither as a finding. A hypothesis to follow forward (the
   `/forwardtest` archive records what the live screen does) and to retest on data that
   prices delisted names, not a rule to ship.
3. What does not depend on a signal working is **how much to risk, whether the picks are
   one bet, and what events fall inside the hold**. That is what the risk and
   relative-strength lines in `/recommendations` and `/analyze` provide.

## Caveats

- Sections 1-4 use today's index members only; delisted and acquired stocks are absent,
  and removals from the index are not modelled. The small/mid-cap test has no join dates
  at all. Section 5 quantifies what this costs: up to a quarter of the 2015 index.
- Equal-weighted universes; no sector neutralisation; one regime-heavy decade.
- Holds overlap, so the t-values are rough. Costs are a flat 10 bps per side.
- Daily bars only; nothing here tests intraday, options, fundamentals beyond earnings
  surprise, or news.
