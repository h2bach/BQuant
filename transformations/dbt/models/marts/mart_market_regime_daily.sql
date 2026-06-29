with index_features as (
    select *
    from {{ ref('int_market_index_returns') }}
),

pivoted as (
    select
        trading_date,
        max(case when symbol = 'VNINDEX' then close end) as vnindex_close,
        max(case when symbol = 'VNINDEX' then return_1d end) as vnindex_return_1d,
        max(case when symbol = 'VNINDEX' then return_20d end) as vnindex_return_20d,
        max(case when symbol = 'VNINDEX' then sma_20 end) as vnindex_sma_20,
        max(case when symbol = 'VNINDEX' then sma_50 end) as vnindex_sma_50,
        max(case when symbol = 'VNINDEX' then sma_200 end) as vnindex_sma_200,
        max(case when symbol = 'VNINDEX' then volatility_20d end) as vnindex_volatility_20d,
        max(case when symbol = 'VN30' then close end) as vn30_close,
        max(case when symbol = 'VN30' then return_1d end) as vn30_return_1d,
        max(case when symbol = 'VN30' then return_20d end) as vn30_return_20d,
        max(case when symbol = 'VN30' then sma_20 end) as vn30_sma_20,
        max(case when symbol = 'VN30' then sma_50 end) as vn30_sma_50,
        max(case when symbol = 'VN30' then sma_200 end) as vn30_sma_200,
        max(case when symbol = 'VN30' then volatility_20d end) as vn30_volatility_20d
    from index_features
    group by trading_date
),

with_breadth as (
    select
        pivoted.*,
        breadth.active_symbols,
        breadth.advancers,
        breadth.decliners,
        breadth.advancer_ratio,
        breadth.breadth_score,
        breadth.constituent_volume
    from pivoted
    left join {{ ref('int_market_breadth_daily') }} as breadth
        on pivoted.trading_date = breadth.trading_date
),

scored as (
    select
        *,
        avg(vnindex_volatility_20d) over (
            order by trading_date rows between 251 preceding and current row
        ) as vnindex_volatility_20d_avg_252,
        case
            when vnindex_close > vnindex_sma_50
             and vnindex_sma_50 > vnindex_sma_200
             and coalesce(breadth_score, 0) >= 0.10
                then 'bullish'
            when vnindex_close < vnindex_sma_50
             and vnindex_sma_50 < vnindex_sma_200
             and coalesce(breadth_score, 0) <= -0.10
                then 'bearish'
            else 'neutral'
        end as market_regime,
        case
            when vnindex_volatility_20d > 1.35 * nullif(avg(vnindex_volatility_20d) over (
                order by trading_date rows between 251 preceding and current row
            ), 0)
                then 'high_volatility'
            when vnindex_volatility_20d < 0.75 * nullif(avg(vnindex_volatility_20d) over (
                order by trading_date rows between 251 preceding and current row
            ), 0)
                then 'low_volatility'
            else 'normal_volatility'
        end as volatility_regime
    from with_breadth
)

select
    *,
    (
        case when market_regime = 'bullish' then 1.0 when market_regime = 'bearish' then -1.0 else 0.0 end
        + coalesce(breadth_score, 0)
        + case when vn30_return_20d > vnindex_return_20d then 0.25 else -0.25 end
    ) as regime_score
from scored
