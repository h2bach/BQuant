select
    symbol,
    session_date as trading_date,
    min(bar_time) as first_bar_time,
    max(bar_time) as last_bar_time,
    count(*) as bar_count,
    arg_min(open, bar_time) as session_open,
    max(high) as session_high,
    min(low) as session_low,
    arg_max(close, bar_time) as session_close,
    sum(volume) as session_volume,
    (max(high) - min(low)) / nullif(arg_max(close, bar_time), 0) as session_range_pct,
    arg_max(close, bar_time) / nullif(arg_min(open, bar_time), 0) - 1.0 as session_return
from {{ ref('stg_intraday_15m') }}
group by symbol, session_date
