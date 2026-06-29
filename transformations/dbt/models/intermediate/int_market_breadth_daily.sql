with symbol_moves as (
    select
        symbol,
        trading_date,
        close,
        volume,
        lag(close, 1) over (partition by symbol order by trading_date) as previous_close
    from {{ ref('stg_daily_ohlcv') }}
),

breadth as (
    select
        trading_date,
        count(*) as active_symbols,
        sum(case when close > previous_close then 1 else 0 end) as advancers,
        sum(case when close < previous_close then 1 else 0 end) as decliners,
        sum(case when close = previous_close then 1 else 0 end) as unchanged,
        sum(volume) as constituent_volume,
        avg(case when previous_close is not null then close / nullif(previous_close, 0) - 1.0 end) as avg_constituent_return_1d
    from symbol_moves
    group by trading_date
)

select
    *,
    advancers / nullif(active_symbols, 0)::double as advancer_ratio,
    decliners / nullif(active_symbols, 0)::double as decliner_ratio,
    (advancers - decliners) / nullif(active_symbols, 0)::double as breadth_score
from breadth
