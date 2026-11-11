-- Daily screen numbers. The bot keeps only the last 14 days of this table.
select symbol,
       session_date,
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
       volume_ratio
from trade_metrics_history
