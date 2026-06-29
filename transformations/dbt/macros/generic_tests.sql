{% test ohlc_bounds(model, open_col='open', high_col='high', low_col='low', close_col='close') %}
select *
from {{ model }}
where {{ high_col }} < greatest({{ open_col }}, {{ low_col }}, {{ close_col }})
   or {{ low_col }} > least({{ open_col }}, {{ high_col }}, {{ close_col }})
{% endtest %}

{% test non_negative(model, column_name) %}
select *
from {{ model }}
where {{ column_name }} < 0
{% endtest %}

{% test not_in_values(model, column_name, values) %}
select *
from {{ model }}
where {{ column_name }} in (
  {% for value in values %}
    '{{ value }}'{% if not loop.last %},{% endif %}
  {% endfor %}
)
{% endtest %}
