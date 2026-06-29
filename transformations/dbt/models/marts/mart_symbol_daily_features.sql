with symbol_features as (
    select *
    from {{ ref('int_symbol_daily_returns') }}
),

liquidity as (
    select *
    from {{ ref('int_symbol_liquidity_daily') }}
),

vn30_returns as (
    select
        trading_date,
        return_1d as vn30_return_1d,
        return_20d as vn30_return_20d,
        return_60d as vn30_return_60d
    from {{ ref('int_market_index_returns') }}
    where symbol = 'VN30'
),

active_universe as (
    select distinct symbol
    from {{ ref('stg_universe_members') }}
    where universe_name = 'VN30'
      and is_active
)

select
    symbol_features.symbol,
    symbol_features.trading_date,
    symbol_features.open,
    symbol_features.high,
    symbol_features.low,
    symbol_features.close,
    symbol_features.adjusted_close,
    symbol_features.volume,
    symbol_features.source,
    symbol_features.return_1d,
    symbol_features.return_5d,
    symbol_features.return_20d,
    symbol_features.return_60d,
    symbol_features.log_return_1d,
    symbol_features.sma_20,
    symbol_features.sma_50,
    symbol_features.sma_200,
    symbol_features.volatility_20d,
    symbol_features.volatility_60d,
    symbol_features.drawdown_from_peak,
    liquidity.traded_value_proxy,
    liquidity.avg_volume_20d,
    liquidity.avg_volume_60d,
    liquidity.volume_to_avg_20d,
    liquidity.volume_cross_section_percentile,
    liquidity.traded_value_cross_section_percentile,
    vn30_returns.vn30_return_1d,
    vn30_returns.vn30_return_20d,
    vn30_returns.vn30_return_60d,
    symbol_features.return_20d - vn30_returns.vn30_return_20d as relative_strength_20d,
    symbol_features.return_60d - vn30_returns.vn30_return_60d as relative_strength_60d,
    case
        when symbol_features.close > symbol_features.sma_50
         and symbol_features.sma_50 > symbol_features.sma_200
            then 'uptrend'
        when symbol_features.close < symbol_features.sma_50
         and symbol_features.sma_50 < symbol_features.sma_200
            then 'downtrend'
        else 'sideways'
    end as trend_state,
    case
        when liquidity.traded_value_cross_section_percentile >= 0.75 then 'high_liquidity'
        when liquidity.traded_value_cross_section_percentile <= 0.25 then 'low_liquidity'
        else 'normal_liquidity'
    end as liquidity_state,
    symbol_features.symbol || '|' || cast(symbol_features.trading_date as varchar) as symbol_date_key
from symbol_features
inner join active_universe
    on symbol_features.symbol = active_universe.symbol
left join liquidity
    on symbol_features.symbol = liquidity.symbol
   and symbol_features.trading_date = liquidity.trading_date
left join vn30_returns
    on symbol_features.trading_date = vn30_returns.trading_date
