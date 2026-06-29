with source_rows as (
    select *
    from {{ source('warehouse', 'market_index_daily_base') }}
)

select
    upper(cast(symbol as varchar)) as symbol,
    cast(trading_date as date) as trading_date,
    cast(open as double) as open,
    cast(high as double) as high,
    cast(low as double) as low,
    cast(close as double) as close,
    cast(adjusted_close as double) as adjusted_close,
    cast(volume as double) as volume,
    cast(source as varchar) as source,
    cast(created_at as timestamp) as created_at,
    cast(updated_at as timestamp) as updated_at,
    upper(cast(symbol as varchar)) || '|' || cast(cast(trading_date as date) as varchar) as symbol_date_key
from source_rows
where upper(cast(symbol as varchar)) in ('VNINDEX', 'VN30')
