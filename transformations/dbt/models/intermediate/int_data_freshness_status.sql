with source_latest as (
    select
        'daily_ohlcv_10y' as dataset_name,
        symbol,
        max(cast(trading_date as timestamp)) as latest_data_ts,
        count(*) as table_row_count
    from {{ ref('stg_daily_ohlcv') }}
    group by symbol

    union all

    select
        'market_index_daily_10y' as dataset_name,
        symbol,
        max(cast(trading_date as timestamp)) as latest_data_ts,
        count(*) as table_row_count
    from {{ ref('stg_market_index_daily') }}
    group by symbol

    union all

    select
        'intraday_ohlcv_15m_60d' as dataset_name,
        symbol,
        max(bar_time) as latest_data_ts,
        count(*) as table_row_count
    from {{ ref('stg_intraday_15m') }}
    group by symbol
),

manifest_latest as (
    select
        dataset_name,
        symbol,
        max(coverage_end) as manifest_coverage_end,
        sum(row_count) as manifest_row_count,
        max(last_refresh_at) as last_refresh_at,
        max(update_status) as update_status
    from {{ ref('stg_data_file_manifest') }}
    group by dataset_name, symbol
)

select
    source_latest.dataset_name,
    source_latest.symbol,
    source_latest.latest_data_ts,
    source_latest.table_row_count,
    manifest_latest.manifest_coverage_end,
    manifest_latest.manifest_row_count,
    manifest_latest.last_refresh_at,
    coalesce(manifest_latest.update_status, 'missing_manifest') as update_status,
    date_diff('day', cast(source_latest.latest_data_ts as date), current_date) as stale_days,
    abs(coalesce(manifest_latest.manifest_row_count, 0) - source_latest.table_row_count) as manifest_row_count_diff
from source_latest
left join manifest_latest
    on source_latest.dataset_name = manifest_latest.dataset_name
   and source_latest.symbol = manifest_latest.symbol
