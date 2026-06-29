with base_rows as (
    select
        *,
        'base' as file_role,
        1 as source_priority
    from {{ source('warehouse', 'intraday_ohlcv_15m_base') }}
),

delta_rows as (
    select
        *,
        'delta' as file_role,
        2 as source_priority
    from {{ source('warehouse', 'intraday_ohlcv_15m_delta') }}
),

stacked as (
    select * from base_rows
    union all
    select * from delta_rows
),

deduplicated as (
    select
        *,
        row_number() over (
            partition by upper(cast(symbol as varchar)), cast(bar_time as timestamp)
            order by source_priority desc, cast(updated_at as timestamp) desc
        ) as row_rank
    from stacked
)

select
    upper(cast(symbol as varchar)) as symbol,
    cast(bar_time as timestamp) as bar_time,
    cast(session_date as date) as session_date,
    cast(open as double) as open,
    cast(high as double) as high,
    cast(low as double) as low,
    cast(close as double) as close,
    cast(volume as double) as volume,
    lower(cast(interval as varchar)) as interval,
    cast(source as varchar) as source,
    cast(snapshot_date as date) as snapshot_date,
    cast(created_at as timestamp) as created_at,
    cast(updated_at as timestamp) as updated_at,
    file_role,
    upper(cast(symbol as varchar)) || '|' || cast(cast(bar_time as timestamp) as varchar) as symbol_bar_key
from deduplicated
where row_rank = 1
