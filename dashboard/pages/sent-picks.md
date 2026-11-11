---
title: Sent picks
---

Each pick the bot sent to Slack and Telegram. Results fill in every night at
03:30; "waiting" means its hold is not over yet.

```sql sent
select sent_on::date as sent_on,
       symbol,
       horizon_sessions,
       case when has_result = 1 then 'done'
            else 'waiting (' || horizon_sessions::int || '-day hold not over)' end as status,
       net_return_pct,
       excess_return_pct,
       benchmark_symbol
from trade_history.picks
order by sent_on desc, symbol
```

```sql summary
select count(*) as sent,
       count(net_return_pct) as with_result,
       avg(net_return_pct) as avg_result,
       avg(excess_return_pct) as avg_vs_index,
       count(*) filter (where net_return_pct > 0) / nullif(count(net_return_pct), 0) as share_up,
       count(*) filter (where excess_return_pct > 0) / nullif(count(excess_return_pct), 0) as share_beat_index
from ${sent}
```

<BigValue data={summary} value=sent title="Picks sent" />
<BigValue data={summary} value=with_result title="With result" />
<BigValue data={summary} value=avg_result title="Average after fees %" fmt=num2 />
<BigValue data={summary} value=avg_vs_index title="Average vs index %" fmt=num2 />
<BigValue data={summary} value=share_up title="Went up" fmt=pct0 />
<BigValue data={summary} value=share_beat_index title="Beat the index" fmt=pct0 />

```sql histogram
-- Results in 1-point buckets, so a few big winners or losers stand out.
select floor(net_return_pct) as bucket, count(*) as picks
from ${sent}
where net_return_pct is not null
group by all
order by bucket
```

<BarChart
  data={histogram}
  x=bucket
  y=picks
  title="How the finished picks did (after fees, 1% buckets)"
  xAxisTitle="Result %, rounded down"
/>

<DataTable data={sent} rows=50 search=true>
  <Column id=sent_on title="Sent on" />
  <Column id=symbol title="Stock" />
  <Column id=status title="Status" />
  <Column id=net_return_pct title="Result after fees %" fmt=num2 contentType=delta />
  <Column id=excess_return_pct title="Better than index by %" fmt=num2 contentType=delta />
  <Column id=benchmark_symbol title="Index compared with" />
</DataTable>
