-- Every screened stock, every day, picked or not (signal_archive), with the
-- outcome JSON unpacked so pages can average it. Outcomes are NULL until the
-- hold is over.
select symbol,
       signal_session,
       horizon_sessions,
       qualified,
       case when qualified then 'Picked'
            else case failed_gate
              when 'trend' then 'Below its 20-day average'
              when 'momentum_5d' then 'Not up 1% over 5 days'
              when 'momentum_1d' then 'Down today'
              when 'volatility' then 'Swings too much'
              when 'drawdown' then 'Too far below its high'
              when 'stale' then 'Price data is old'
              else failed_gate end
       end as reason,
       last_close,
       day_change_pct,
       week_momentum_pct,
       atr_pct,
       drawdown_pct,
       benchmark_symbol,
       outcome_json is not null as has_result,
       json_extract(outcome_json, '$.net_return_pct') as net_return_pct,
       json_extract(outcome_json, '$.benchmark_return_pct') as benchmark_return_pct,
       json_extract(outcome_json, '$.excess_return_pct') as excess_return_pct
from signal_archive
