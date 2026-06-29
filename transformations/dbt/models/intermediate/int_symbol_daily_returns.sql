with daily as (
    select *
    from {{ ref('stg_daily_ohlcv') }}
),

base_features as (
    select
        symbol,
        trading_date,
        open,
        high,
        low,
        close,
        adjusted_close,
        volume,
        source,
        close / nullif(lag(close, 1) over symbol_window, 0) - 1.0 as return_1d,
        close / nullif(lag(close, 5) over symbol_window, 0) - 1.0 as return_5d,
        close / nullif(lag(close, 20) over symbol_window, 0) - 1.0 as return_20d,
        close / nullif(lag(close, 60) over symbol_window, 0) - 1.0 as return_60d,
        ln(close / nullif(lag(close, 1) over symbol_window, 0)) as log_return_1d,
        avg(close) over rolling_20 as sma_20,
        avg(close) over rolling_50 as sma_50,
        avg(close) over rolling_200 as sma_200,
        close / nullif(max(close) over peak_window, 0) - 1.0 as drawdown_from_peak
    from daily
    window
        symbol_window as (partition by symbol order by trading_date),
        rolling_20 as (partition by symbol order by trading_date rows between 19 preceding and current row),
        rolling_50 as (partition by symbol order by trading_date rows between 49 preceding and current row),
        rolling_60 as (partition by symbol order by trading_date rows between 59 preceding and current row),
        rolling_200 as (partition by symbol order by trading_date rows between 199 preceding and current row),
        peak_window as (partition by symbol order by trading_date rows between unbounded preceding and current row)
),

features as (
    select
        *,
        stddev_samp(log_return_1d) over (
            partition by symbol order by trading_date rows between 19 preceding and current row
        ) as volatility_20d,
        stddev_samp(log_return_1d) over (
            partition by symbol order by trading_date rows between 59 preceding and current row
        ) as volatility_60d
    from base_features
)

select *
from features
