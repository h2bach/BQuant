select
    upper(cast(universe_name as varchar)) as universe_name,
    upper(cast(symbol as varchar)) as symbol,
    cast(effective_date as date) as effective_date,
    cast(end_date as date) as end_date,
    cast(source as varchar) as source,
    cast(created_at as timestamp) as created_at,
    end_date is null or cast(end_date as date) >= current_date as is_active,
    upper(cast(universe_name as varchar)) || '|' || upper(cast(symbol as varchar)) || '|' || cast(cast(effective_date as date) as varchar) as universe_symbol_key
from {{ source('warehouse', 'universe_members') }}
