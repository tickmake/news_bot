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

## Conclusions

1. Daily price gates, classic factors and earnings surprises show **no reliable edge**
   over holding the same stocks in this data. Treat `/recommendations` as a screen.
2. 12-month momentum is **regime-dependent in large caps** (nothing before 2022,
   strong since) and **steadier but unproven in small and mid caps** (survivor-biased
   data). A hypothesis to follow forward (the `/forwardtest` archive records what the
   live screen does) and to test on point-in-time data, not a rule to ship.
3. What does not depend on a signal working is **how much to risk, whether the picks are
   one bet, and what events fall inside the hold**. That is what the risk and
   relative-strength lines in `/recommendations` and `/analyze` provide.

## Caveats

- Today's index members only; delisted and acquired stocks are absent, and removals
  from the index are not modelled. The small/mid-cap test has no join dates at all.
- Equal-weighted universes; no sector neutralisation; one regime-heavy decade.
- Holds overlap, so the t-values are rough. Costs are a flat 10 bps per side.
- Daily bars only; nothing here tests intraday, options, fundamentals beyond earnings
  surprise, or news.
