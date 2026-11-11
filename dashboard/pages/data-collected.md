---
title: Data collected
---

A gap in the dates means the bot did not run that day; weekends and holidays
are normal gaps. "With result" fills in when the hold is over.

```sql per_day
select signal_session::date as day,
       count(*) as checked,
       sum(qualified) as picked,
       sum(has_result) as with_result
from trade_history.signals
group by all
order by day desc
```

<BarChart data={per_day} x=day y={["picked", "checked"]} type=grouped title="Stocks checked and picked per day" />

<DataTable data={per_day} rows=40>
  <Column id=day title="Day" />
  <Column id=checked title="Stocks checked" />
  <Column id=picked title="Picked" />
  <Column id=with_result title="With result" />
</DataTable>
