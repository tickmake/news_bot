-- Real trades saved with /fill. Prices are in the stock's own currency.
--
-- The Evidence SQLite connector errors on a query that returns no rows (it
-- infers column types from the first row), and this table stays empty until
-- the first /fill. So an empty table yields one placeholder row with
-- side = 'NONE', which every page filters out.
select id, trade_date, side, symbol, shares, price, currency, fx_to_nok, fee_nok, checks_skipped
from real_trades
union all
select 0, '', 'NONE', '', 0, 0.0, '', 0.0, 0.0, 0
where not exists (select 1 from real_trades)
