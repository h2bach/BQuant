with daily as (
    select *
    from {{ ref('stg_daily_ohlcv') }}
)

select
    symbol,
    trading_date,
    volume,
    close * volume as traded_value_proxy,
    avg(volume) over rolling_20 as avg_volume_20d,
    avg(volume) over rolling_60 as avg_volume_60d,
    volume / nullif(avg(volume) over rolling_20, 0) as volume_to_avg_20d,
    percent_rank() over (partition by trading_date order by volume) as volume_cross_section_percentile,
    percent_rank() over (partition by trading_date order by close * volume) as traded_value_cross_section_percentile
from daily
window
    rolling_20 as (partition by symbol order by trading_date rows between 19 preceding and current row),
    rolling_60 as (partition by symbol order by trading_date rows between 59 preceding and current row)
