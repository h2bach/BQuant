with daily as (
    select *
    from {{ ref('stg_daily_ohlcv') }}
),

numbered as (
    select
        *,
        row_number() over (partition by symbol order by trading_date) as rn,
        lag(close) over (partition by symbol order by trading_date) as prev_close,
        lag(high) over (partition by symbol order by trading_date) as prev_high,
        lag(low) over (partition by symbol order by trading_date) as prev_low
    from daily
),

base_inputs as (
    select
        *,
        close / nullif(lag(close, 10) over symbol_window, 0) - 1.0 as roc_10,
        close / nullif(lag(close, 20) over symbol_window, 0) - 1.0 as roc_20,
        close / nullif(lag(close, 60) over symbol_window, 0) - 1.0 as roc_60,
        ln(close / nullif(prev_close, 0)) as log_return_1d,
        greatest(
            high - low,
            abs(high - coalesce(prev_close, close)),
            abs(low - coalesce(prev_close, close))
        ) as true_range,
        case
            when high - coalesce(prev_high, high) > coalesce(prev_low, low) - low
             and high - coalesce(prev_high, high) > 0
                then high - coalesce(prev_high, high)
            else 0.0
        end as plus_dm,
        case
            when coalesce(prev_low, low) - low > high - coalesce(prev_high, high)
             and coalesce(prev_low, low) - low > 0
                then coalesce(prev_low, low) - low
            else 0.0
        end as minus_dm,
        greatest(close - coalesce(prev_close, close), 0.0) as gain_1d,
        greatest(coalesce(prev_close, close) - close, 0.0) as loss_1d,
        ((high + low + close) / 3.0) as typical_price,
        ((high + low + close) / 3.0) * volume as raw_money_flow,
        case
            when ((high + low + close) / 3.0)
               > lag((high + low + close) / 3.0) over symbol_window
                then ((high + low + close) / 3.0) * volume
            else 0.0
        end as positive_money_flow,
        case
            when ((high + low + close) / 3.0)
               < lag((high + low + close) / 3.0) over symbol_window
                then ((high + low + close) / 3.0) * volume
            else 0.0
        end as negative_money_flow,
        case
            when close > coalesce(prev_close, close) then volume
            when close < coalesce(prev_close, close) then -volume
            else 0.0
        end as obv_step
    from numbered
    window
        symbol_window as (partition by symbol order by trading_date)
),

base as (
    select
        *,
        avg(close) over rolling_20 as sma_20,
        avg(close) over rolling_50 as sma_50,
        avg(close) over rolling_100 as sma_100,
        avg(close) over rolling_200 as sma_200,
        stddev_samp(close) over rolling_20 as close_std_20,
        min(low) over rolling_14 as low_14,
        max(high) over rolling_14 as high_14,
        avg(volume) over rolling_20 as avg_volume_20,
        stddev_samp(volume) over rolling_20 as volume_std_20,
        max(close) over rolling_252 as high_close_252,
        stddev_samp(log_return_1d) over rolling_20 as realized_volatility_20d,
        stddev_samp(log_return_1d) over rolling_60 as realized_volatility_60d,
        stddev_samp(case when log_return_1d < 0 then log_return_1d end) over rolling_20 as downside_volatility_20d,
        avg(true_range) over rolling_14 as atr_14,
        avg(gain_1d) over rolling_14 as avg_gain_14,
        avg(loss_1d) over rolling_14 as avg_loss_14,
        sum(plus_dm) over rolling_14 as plus_dm_14,
        sum(minus_dm) over rolling_14 as minus_dm_14,
        sum(true_range) over rolling_14 as true_range_14,
        avg(typical_price) over rolling_20 as typical_sma_20,
        sum(positive_money_flow) over rolling_14 as positive_money_flow_14,
        sum(negative_money_flow) over rolling_14 as negative_money_flow_14,
        sum(obv_step) over symbol_window as obv
    from base_inputs
    window
        symbol_window as (partition by symbol order by trading_date),
        rolling_14 as (partition by symbol order by trading_date rows between 13 preceding and current row),
        rolling_20 as (partition by symbol order by trading_date rows between 19 preceding and current row),
        rolling_50 as (partition by symbol order by trading_date rows between 49 preceding and current row),
        rolling_60 as (partition by symbol order by trading_date rows between 59 preceding and current row),
        rolling_100 as (partition by symbol order by trading_date rows between 99 preceding and current row),
        rolling_200 as (partition by symbol order by trading_date rows between 199 preceding and current row),
        rolling_252 as (partition by symbol order by trading_date rows between 251 preceding and current row)
),

base_with_deviation as (
    select
        *,
        avg(abs(typical_price - typical_sma_20)) over (
            partition by symbol order by trading_date rows between 19 preceding and current row
        ) as typical_mean_deviation_20
    from base
),

ema_weighted as (
    select
        current.symbol,
        current.trading_date,
        sum(case when current.rn - previous.rn <= 48 then previous.close * power(1.0 - 2.0 / 13.0, current.rn - previous.rn) end)
            / nullif(sum(case when current.rn - previous.rn <= 48 then power(1.0 - 2.0 / 13.0, current.rn - previous.rn) end), 0) as ema_12,
        sum(case when current.rn - previous.rn <= 80 then previous.close * power(1.0 - 2.0 / 21.0, current.rn - previous.rn) end)
            / nullif(sum(case when current.rn - previous.rn <= 80 then power(1.0 - 2.0 / 21.0, current.rn - previous.rn) end), 0) as ema_20,
        sum(case when current.rn - previous.rn <= 104 then previous.close * power(1.0 - 2.0 / 27.0, current.rn - previous.rn) end)
            / nullif(sum(case when current.rn - previous.rn <= 104 then power(1.0 - 2.0 / 27.0, current.rn - previous.rn) end), 0) as ema_26,
        sum(case when current.rn - previous.rn <= 200 then previous.close * power(1.0 - 2.0 / 51.0, current.rn - previous.rn) end)
            / nullif(sum(case when current.rn - previous.rn <= 200 then power(1.0 - 2.0 / 51.0, current.rn - previous.rn) end), 0) as ema_50
    from numbered as current
    inner join numbered as previous
        on current.symbol = previous.symbol
       and previous.rn between current.rn - 200 and current.rn
    group by current.symbol, current.trading_date
),

with_ema as (
    select
        base_with_deviation.*,
        ema_weighted.ema_12,
        ema_weighted.ema_20,
        ema_weighted.ema_26,
        ema_weighted.ema_50,
        ema_weighted.ema_12 - ema_weighted.ema_26 as macd
    from base_with_deviation
    left join ema_weighted
        on base_with_deviation.symbol = ema_weighted.symbol
       and base_with_deviation.trading_date = ema_weighted.trading_date
),

indicator_base as (
    select
        *,
        avg(macd) over (partition by symbol order by trading_date rows between 8 preceding and current row) as macd_signal,
        100.0 - 100.0 / (1.0 + avg_gain_14 / nullif(avg_loss_14, 0)) as rsi_14,
        100.0 * (close - low_14) / nullif(high_14 - low_14, 0) as stochastic_k,
        100.0 * plus_dm_14 / nullif(true_range_14, 0) as plus_di_14,
        100.0 * minus_dm_14 / nullif(true_range_14, 0) as minus_di_14,
        (typical_price - typical_sma_20) / nullif(0.015 * typical_mean_deviation_20, 0) as cci_20,
        100.0 - 100.0 / (1.0 + positive_money_flow_14 / nullif(negative_money_flow_14, 0)) as mfi_14,
        (4.0 * close_std_20) / nullif(sma_20, 0) as bollinger_band_width,
        close / nullif(high_close_252, 0) - 1.0 as drawdown_from_252d_high,
        volume / nullif(avg_volume_20, 0) as volume_to_avg_20d_ta,
        (volume - avg_volume_20) / nullif(volume_std_20, 0) as volume_zscore_20,
        (sma_20 / nullif(lag(sma_20, 20) over (partition by symbol order by trading_date), 0) - 1.0) as sma_20_slope_20d,
        (sma_50 / nullif(lag(sma_50, 20) over (partition by symbol order by trading_date), 0) - 1.0) as sma_50_slope_20d
    from with_ema
),

indicator_final as (
    select
        *,
        macd - macd_signal as macd_histogram,
        avg(stochastic_k) over (partition by symbol order by trading_date rows between 2 preceding and current row) as stochastic_d,
        avg(
            100.0 * abs(plus_di_14 - minus_di_14) / nullif(plus_di_14 + minus_di_14, 0)
        ) over (partition by symbol order by trading_date rows between 13 preceding and current row) as adx_14
    from indicator_base
),

with_index as (
    select
        indicator_final.*,
        vn30.return_20d as vn30_return_20d,
        vn30.return_60d as vn30_return_60d,
        vnindex.return_20d as vnindex_return_20d,
        vnindex.return_60d as vnindex_return_60d,
        vn30.return_1d as vn30_return_1d
    from indicator_final
    left join {{ ref('int_market_index_returns') }} as vn30
        on indicator_final.trading_date = vn30.trading_date
       and vn30.symbol = 'VN30'
    left join {{ ref('int_market_index_returns') }} as vnindex
        on indicator_final.trading_date = vnindex.trading_date
       and vnindex.symbol = 'VNINDEX'
),

with_relative as (
    select
        *,
        roc_20 - vn30_return_20d as relative_strength_vs_vn30_20d,
        roc_60 - vn30_return_60d as relative_strength_vs_vn30_60d,
        roc_20 - vnindex_return_20d as relative_strength_vs_vnindex_20d,
        roc_60 - vnindex_return_60d as relative_strength_vs_vnindex_60d,
        covar_samp(log_return_1d, vn30_return_1d) over rolling_60
            / nullif(var_samp(vn30_return_1d) over rolling_60, 0) as beta_vs_vn30_60d,
        corr(log_return_1d, vn30_return_1d) over rolling_60 as correlation_vs_vn30_60d
    from with_index
    window rolling_60 as (partition by symbol order by trading_date rows between 59 preceding and current row)
),

scored as (
    select
        *,
        least(1.0, greatest(0.0,
            (
                case when close > sma_20 then 1.0 else 0.0 end
              + case when close > sma_50 then 1.0 else 0.0 end
              + case when close > sma_200 then 1.0 else 0.0 end
              + case when sma_20 > sma_50 then 1.0 else 0.0 end
              + case when sma_50 > sma_200 then 1.0 else 0.0 end
              + case when ema_20 > ema_50 then 1.0 else 0.0 end
              + case when coalesce(adx_14, 0) >= 20 and coalesce(plus_di_14, 0) > coalesce(minus_di_14, 0) then 1.0 else 0.0 end
            ) / 7.0
        )) as ta_trend_score,
        least(1.0, greatest(0.0,
            (
                coalesce((rsi_14 - 30.0) / 40.0, 0.5)
              + case when macd_histogram > 0 then 1.0 else 0.0 end
              + coalesce((roc_20 + 0.12) / 0.24, 0.5)
              + coalesce(stochastic_k / 100.0, 0.5)
              + coalesce((mfi_14 - 20.0) / 60.0, 0.5)
            ) / 5.0
        )) as ta_momentum_score,
        least(1.0, greatest(0.0,
            (
                coalesce(1.0 - realized_volatility_20d * 8.0, 0.5)
              + coalesce(1.0 - atr_14 / nullif(close, 0) * 8.0, 0.5)
              + coalesce(1.0 - bollinger_band_width * 2.0, 0.5)
              + coalesce(1.0 + drawdown_from_252d_high, 0.5)
            ) / 4.0
        )) as ta_volatility_score,
        least(1.0, greatest(0.0,
            (
                percent_rank() over (partition by trading_date order by close * volume)
              + coalesce(least(volume_to_avg_20d_ta, 2.0) / 2.0, 0.5)
              + coalesce(least(greatest((volume_zscore_20 + 2.0) / 4.0, 0.0), 1.0), 0.5)
            ) / 3.0
        )) as ta_liquidity_score,
        least(1.0, greatest(0.0,
            (
                coalesce((relative_strength_vs_vn30_20d + 0.10) / 0.20, 0.5)
              + coalesce((relative_strength_vs_vn30_60d + 0.15) / 0.30, 0.5)
              + coalesce((relative_strength_vs_vnindex_20d + 0.10) / 0.20, 0.5)
              + coalesce((relative_strength_vs_vnindex_60d + 0.15) / 0.30, 0.5)
            ) / 4.0
        )) as ta_relative_strength_score
    from with_relative
),

composite as (
    select
        *,
        least(1.0, greatest(0.0,
            0.28 * ta_trend_score
          + 0.24 * ta_momentum_score
          + 0.16 * ta_volatility_score
          + 0.14 * ta_liquidity_score
          + 0.18 * ta_relative_strength_score
        )) as ta_composite_score
    from scored
)

select
    symbol,
    trading_date,
    sma_20,
    sma_50,
    sma_100,
    sma_200,
    ema_12,
    ema_20,
    ema_26,
    ema_50,
    close / nullif(sma_20, 0) - 1.0 as price_to_sma_20,
    close / nullif(sma_50, 0) - 1.0 as price_to_sma_50,
    close / nullif(sma_200, 0) - 1.0 as price_to_sma_200,
    close / nullif(ema_20, 0) - 1.0 as price_to_ema_20,
    sma_20_slope_20d,
    sma_50_slope_20d,
    adx_14,
    plus_di_14,
    minus_di_14,
    rsi_14,
    macd,
    macd_signal,
    macd_histogram,
    roc_10,
    roc_20,
    stochastic_k,
    stochastic_d,
    cci_20,
    atr_14,
    atr_14 / nullif(close, 0) as atr_pct_14,
    bollinger_band_width,
    realized_volatility_20d,
    realized_volatility_60d,
    downside_volatility_20d,
    drawdown_from_252d_high,
    volume_zscore_20,
    volume_to_avg_20d_ta,
    obv,
    mfi_14,
    relative_strength_vs_vn30_20d,
    relative_strength_vs_vn30_60d,
    relative_strength_vs_vnindex_20d,
    relative_strength_vs_vnindex_60d,
    beta_vs_vn30_60d,
    correlation_vs_vn30_60d,
    ta_trend_score,
    ta_momentum_score,
    ta_volatility_score,
    ta_liquidity_score,
    ta_relative_strength_score,
    ta_composite_score,
    case
        when ta_composite_score >= 0.62 then 'bullish'
        when ta_composite_score <= 0.38 then 'bearish'
        else 'neutral'
    end as ta_action_bias,
    case
        when coalesce(realized_volatility_20d, 0) >= 0.045
          or coalesce(drawdown_from_252d_high, 0) <= -0.20
          or coalesce(atr_14 / nullif(close, 0), 0) >= 0.055
            then 'high'
        when coalesce(realized_volatility_20d, 0) >= 0.028
          or coalesce(drawdown_from_252d_high, 0) <= -0.10
            then 'medium'
        else 'low'
    end as ta_risk_flag,
    symbol || '|' || cast(trading_date as varchar) as symbol_date_key
from composite
