---
title: One stock
---

```sql stocks
select symbol, count(*) as days
from trade_history.signals
group by symbol
order by symbol
```

<Dropdown name=stock data={stocks} value=symbol title="Stock" defaultValue="EQNR.OL" />

```sql history
select signal_session::date as day,
       reason,
       last_close,
       day_change_pct,
       week_momentum_pct,
       horizon_sessions,
       net_return_pct
from trade_history.signals
where symbol = '${inputs.stock.value}'
order by day desc
```

<LineChart data={history} x=day y=last_close title="Price on each screen day" yAxisTitle="Price" markers=true />

<DataTable data={history} rows=50>
  <Column id=day title="Day" />
  <Column id=reason title="Result" />
  <Column id=last_close title="Price" fmt=num2 />
  <Column id=day_change_pct title="Today %" fmt=num1 contentType=delta />
  <Column id=week_momentum_pct title="Last 5 days %" fmt=num1 contentType=delta />
  <Column id=horizon_sessions title="Hold (trading days)" />
  <Column id=net_return_pct title="At the end of the hold, after fees %" fmt=num2 contentType=delta />
</DataTable>
