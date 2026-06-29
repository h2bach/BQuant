with active_universe as (
    select distinct symbol
    from {{ ref('stg_universe_members') }}
    where universe_name = 'VN30'
      and is_active
),

daily_quality as (
    select
        symbol,
        count(*) as daily_row_count,
        min(trading_date) as first_trading_date,
        max(trading_date) as latest_trading_date,
        sum(case when high < greatest(open, low, close) or low > least(open, high, close) then 1 else 0 end) as invalid_ohlc_rows,
        sum(case when volume < 0 then 1 else 0 end) as negative_volume_rows,
        count(*) - count(distinct symbol_date_key) as duplicate_symbol_date_rows
    from {{ ref('stg_daily_ohlcv') }}
    group by symbol
),

freshness as (
    select *
    from {{ ref('int_data_freshness_status') }}
    where dataset_name = 'daily_ohlcv_10y'
)

select
    active_universe.symbol,
    coalesce(daily_quality.daily_row_count, 0) as daily_row_count,
    daily_quality.first_trading_date,
    daily_quality.latest_trading_date,
    coalesce(daily_quality.invalid_ohlc_rows, 0) as invalid_ohlc_rows,
    coalesce(daily_quality.negative_volume_rows, 0) as negative_volume_rows,
    coalesce(daily_quality.duplicate_symbol_date_rows, 0) as duplicate_symbol_date_rows,
    freshness.manifest_row_count,
    freshness.manifest_row_count_diff,
    freshness.stale_days,
    case
        when coalesce(daily_quality.daily_row_count, 0) = 0 then 'missing'
        when coalesce(daily_quality.invalid_ohlc_rows, 0) > 0 then 'invalid_ohlc'
        when coalesce(daily_quality.negative_volume_rows, 0) > 0 then 'invalid_volume'
        when coalesce(daily_quality.duplicate_symbol_date_rows, 0) > 0 then 'duplicate_rows'
        when coalesce(freshness.stale_days, 999) > 7 then 'stale'
        when coalesce(freshness.manifest_row_count_diff, 0) > 0 then 'manifest_mismatch'
        else 'pass'
    end as data_quality_status
from active_universe
left join daily_quality
    on active_universe.symbol = daily_quality.symbol
left join freshness
    on active_universe.symbol = freshness.symbol
