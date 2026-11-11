---
title: Day by day
---

Each row is one day's screen. A pick is bought at the next morning's open and
sold at the close at the end of the hold (5 trading days until 11 Nov 2026, 20
after), so the newest days are always waiting. Returns are after fees.

```sql daily
select signal_session::date as day,
       max(horizon_sessions) as hold,
       sum(qualified) as picks,
       avg(case when qualified = 1 then net_return_pct end) as picks_avg,
       sum(1 - qualified) as skipped,
       avg(case when qualified = 0 then net_return_pct end) as skipped_avg,
       case when sum(has_result) = 0 then 'waiting for result'
            when sum(has_result) < count(*) then 'partly in, still waiting'
            when sum(qualified) = 0 then 'no picks'
            when avg(case when qualified = 1 then net_return_pct end)
               > avg(case when qualified = 0 then net_return_pct end) then 'picks better'
            else 'picks worse' end as verdict
from trade_history.signals
group by all
order by day desc
```

```sql chart
unpivot (select day, picks_avg as "Picks", skipped_avg as "Skipped" from ${daily})
on "Picks", "Skipped" into name "group" value "average"
```

<BarChart
  data={chart}
  x=day
  y=average
  series=group
  type=grouped
  title="Average result after fees, picks vs skipped (%)"
  yAxisTitle="%"
/>

<DataTable data={daily} rows=40>
  <Column id=day title="Day picked" />
  <Column id=hold title="Hold (trading days)" />
  <Column id=picks title="Picks" />
  <Column id=picks_avg title="Picks: average %" fmt=num2 contentType=delta />
  <Column id=skipped title="Skipped" />
  <Column id=skipped_avg title="Skipped: average %" fmt=num2 contentType=delta />
  <Column id=verdict title="Verdict" />
</DataTable>
