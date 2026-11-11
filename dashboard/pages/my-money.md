---
title: My money
---

```sql trades
-- side = 'NONE' is the placeholder row an empty real_trades table yields.
select * from trade_history.real_trades where side <> 'NONE'
```

```sql trade_list
select trade_date::date as date,
       case side when 'BUY' then 'Bought' else 'Sold' end as action,
       symbol,
       shares,
       printf('%.2f %s', price, currency) as price,
       round(shares * price * fx_to_nok) as worth_nok,
       round(fee_nok) as fee_nok,
       case when currency = 'NOK' then '' else printf('1 %s = %.2f NOK', currency, fx_to_nok) end as fx,
       case when checks_skipped = 1 then 'yes (saved with !)' else '' end as checks_skipped
from ${trades}
order by id desc
```

```sql holdings
select symbol,
       sum(case side when 'BUY' then shares else -shares end) as shares_held,
       printf('%.2f %s',
              sum(case side when 'BUY' then shares * price end)
              / sum(case side when 'BUY' then shares end), max(currency)) as avg_buy_price,
       round(sum(case side when 'BUY' then shares * price * fx_to_nok + fee_nok else 0 end)) as paid_nok,
       round(sum(case side when 'SELL' then shares * price * fx_to_nok - fee_nok else 0 end)) as got_back_nok,
       round(sum(fee_nok)) as fees_nok,
       case when sum(case side when 'BUY' then shares else -shares end) = 0
            then printf('closed: %+.0f NOK',
                        sum(case side when 'SELL' then shares * price * fx_to_nok - fee_nok
                                      else -(shares * price * fx_to_nok + fee_nok) end))
            else 'still holding' end as result
from ${trades}
group by symbol
order by max(trade_date) desc
```

## What I hold

One line per stock. "Paid" includes buy fees; "Got back" is after sell fees.
When all shares are sold, the last column shows the profit or loss in NOK.
Today's value of shares you still hold is not shown here.

<DataTable data={holdings} emptySet=pass emptyMessage="No trades saved yet. Send /fill in Telegram after a real trade.">
  <Column id=symbol title="Stock" />
  <Column id=shares_held title="Shares held" />
  <Column id=avg_buy_price title="Average buy price" />
  <Column id=paid_nok title="Paid NOK" fmt=num0 />
  <Column id=got_back_nok title="Got back NOK" fmt=num0 />
  <Column id=fees_nok title="Fees NOK" fmt=num0 />
  <Column id=result title="Result" />
</DataTable>

## My trades

Every real trade saved with /fill, newest first. "Worth in NOK" uses that
day's exchange rate. To fix a wrong trade, send /unfill and then /fill again.

<DataTable data={trade_list} emptySet=pass emptyMessage="No trades saved yet.">
  <Column id=date title="Date" />
  <Column id=action title="Action" />
  <Column id=symbol title="Stock" />
  <Column id=shares title="Shares" />
  <Column id=price title="Price" />
  <Column id=worth_nok title="Worth in NOK" fmt=num0 />
  <Column id=fee_nok title="Fee NOK" fmt=num0 />
  <Column id=fx title="Exchange rate" />
  <Column id=checks_skipped title="Checks skipped" />
</DataTable>
