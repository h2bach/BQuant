with index_rows as (
    select *
    from {{ ref('stg_market_index_daily') }}
),

base_features as (
    select
        symbol,
        trading_date,
        open,
        high,
        low,
        close,
        volume,
        source,
        close / nullif(lag(close, 1) over index_window, 0) - 1.0 as return_1d,
        close / nullif(lag(close, 5) over index_window, 0) - 1.0 as return_5d,
        close / nullif(lag(close, 20) over index_window, 0) - 1.0 as return_20d,
        close / nullif(lag(close, 60) over index_window, 0) - 1.0 as return_60d,
        ln(close / nullif(lag(close, 1) over index_window, 0)) as log_return_1d,
        avg(close) over rolling_20 as sma_20,
        avg(close) over rolling_50 as sma_50,
        avg(close) over rolling_200 as sma_200,
        (high - low) / nullif(close, 0) as intraday_range_pct
    from index_rows
    window
        index_window as (partition by symbol order by trading_date),
        rolling_20 as (partition by symbol order by trading_date rows between 19 preceding and current row),
        rolling_50 as (partition by symbol order by trading_date rows between 49 preceding and current row),
        rolling_200 as (partition by symbol order by trading_date rows between 199 preceding and current row)
)

select
    *,
    stddev_samp(log_return_1d) over (
        partition by symbol order by trading_date rows between 19 preceding and current row
    ) as volatility_20d,
    stddev_samp(log_return_1d) over (
        partition by symbol order by trading_date rows between 59 preceding and current row
    ) as volatility_60d
from base_features
