-- Picks sent to Slack and Telegram, with their results once the hold is over.
select symbol,
       signal_session,
       substr(published_at, 1, 10) as sent_on,
       horizon_sessions,
       benchmark_symbol,
       outcome_json is not null as has_result,
       json_extract(outcome_json, '$.net_return_pct') as net_return_pct,
       json_extract(outcome_json, '$.benchmark_return_pct') as benchmark_return_pct,
       json_extract(outcome_json, '$.excess_return_pct') as excess_return_pct
from published_picks
