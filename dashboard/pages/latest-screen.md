---
title: Latest screen
---

```sql latest
select * from trade_history.screen
where session_date = (select max(session_date) from trade_history.screen)
order by qualified desc, week_momentum_pct desc
```

```sql reasons
select reason, count(*) as stocks
from ${latest}
group by reason
order by stocks desc
```

Every stock the bot checked on <Value data={latest} column=session_date />.
"Skipped" stocks show the first filter they failed.

<BarChart data={reasons} x=reason y=stocks swapXY=true title="Picked or skipped, and why" />

<DataTable data={latest} rows=50>
  <Column id=symbol title="Stock" />
  <Column id=reason title="Result" />
  <Column id=last_close title="Price" fmt=num2 />
  <Column id=day_change_pct title="Today %" fmt=num1 contentType=delta />
  <Column id=week_momentum_pct title="Last 5 days %" fmt=num1 contentType=delta />
  <Column id=atr_pct title="Normal daily swing %" fmt=num1 />
  <Column id=drawdown_pct title="Below 20-day high %" fmt=num1 />
  <Column id=volume_ratio title="Volume vs normal (x)" fmt=num1 />
</DataTable>
