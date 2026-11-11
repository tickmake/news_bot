---
title: Fjord Signals
---

Every weekday at 07:00 (Oslo time) the bot checks 20-40 stocks: your watch
lists plus the day's big movers. A stock that passes all 5 filters becomes a
**pick** and goes to Slack and Telegram. When its hold is over (5 trading days
until 11 Nov 2026, 20 after), the bot works out what it actually did after
fees, every night at 03:30.

**The test:** if picks do not beat the stocks that were skipped, the filters
are not helping.

```sql days
-- One row per screen day and hold length: average result of picks vs skipped.
select signal_session::date as day,
       horizon_sessions,
       avg(case when qualified = 1 then net_return_pct end) as picks,
       avg(case when qualified = 0 then net_return_pct end) as skipped
from trade_history.signals
where has_result = 1
group by all
```

```sql scorecard
select horizon_sessions,
       count(*) as days_with_results,
       avg(picks) as picks_avg,
       avg(skipped) as skipped_avg,
       avg(picks) - avg(skipped) as difference,
       count(*) filter (where picks > skipped) as days_picks_better,
       count(*) filter (where picks is not null and skipped is not null) as days_compared,
       case when avg(picks) > avg(skipped) then 'Picks did better so far (too early to trust)'
            else 'Picks did NOT do better' end as verdict
from ${days}
group by horizon_sessions
order by horizon_sessions desc
```

```sql latest_hold
select * from ${scorecard} order by days_with_results desc limit 1
```

## Scorecard

<BigValue data={latest_hold} value=picks_avg title="Picks: average %" fmt=num2 />
<BigValue data={latest_hold} value=skipped_avg title="Skipped: average %" fmt=num2 />
<BigValue data={latest_hold} value=difference title="Difference %" fmt=num2 />
<BigValue data={latest_hold} value=days_picks_better title="Days picks did better" />
<BigValue data={latest_hold} value=days_compared title="Days compared" />

Big numbers are for the hold length with the most days of results. Returns are
per stock, after fees, at the end of the hold.

<DataTable data={scorecard}>
  <Column id=horizon_sessions title="Hold (trading days)" />
  <Column id=days_with_results title="Days with results" />
  <Column id=picks_avg title="Picks: average %" fmt=num2 />
  <Column id=skipped_avg title="Skipped: average %" fmt=num2 />
  <Column id=difference title="Difference %" fmt=num2 contentType=delta />
  <Column id=days_picks_better title="Days picks did better" />
  <Column id=days_compared title="Days compared" />
  <Column id=verdict title="In short" />
</DataTable>

```sql running
-- Running total of the daily difference: a line that keeps rising means the
-- filters keep adding value; one that drifts down means they cost money.
select day,
       horizon_sessions::int || '-day hold' as hold,
       sum(picks - skipped) over (partition by horizon_sessions order by day) as running_difference
from ${days}
where picks is not null and skipped is not null
order by day
```

<LineChart
  data={running}
  x=day
  y=running_difference
  series=hold
  title="Picks minus skipped, added up day by day (%)"
  markers=true
  yFmt=num1
/>

## Pages

- [Latest screen](/latest-screen): what the bot looked at and why each stock was picked or skipped
- [Day by day](/day-by-day): the same test for each day
- [Sent picks](/sent-picks): picks sent to Slack/Telegram, and their results
- [One stock](/one-stock): a stock's full history
- [Data collected](/data-collected): is the bot running every day?
- [My money](/my-money): trades saved with /fill, and what you hold

```sql refreshed
select refreshed_at from trade_history.refreshed
```

<small>"After fees" assumes 0.10% per buy and per sell. Real DNB fees are higher
on small trades (59 NOK minimum), see issue #70. The data is re-read from the
bot's database a minute or two after it changes; last read
<Value data={refreshed} column=refreshed_at /> UTC.</small>
