with symbol_features as (
    select *
    from {{ ref('mart_symbol_daily_features') }}
),

market_regime as (
    select *
    from {{ ref('mart_market_regime_daily') }}
),

quality as (
    select *
    from {{ ref('mart_symbol_data_quality') }}
),

ta_signals as (
    select *
    from {{ ref('mart_symbol_ta_signals_daily') }}
)

select
    symbol_features.symbol,
    symbol_features.trading_date,
    symbol_features.close,
    symbol_features.volume,
    symbol_features.return_1d,
    symbol_features.return_5d,
    symbol_features.return_20d,
    symbol_features.return_60d,
    symbol_features.volatility_20d,
    symbol_features.volatility_60d,
    symbol_features.drawdown_from_peak,
    symbol_features.relative_strength_20d,
    symbol_features.relative_strength_60d,
    symbol_features.traded_value_proxy,
    symbol_features.volume_to_avg_20d,
    symbol_features.traded_value_cross_section_percentile,
    symbol_features.trend_state,
    symbol_features.liquidity_state,
    ta_signals.ta_trend_score,
    ta_signals.ta_momentum_score,
    ta_signals.ta_volatility_score,
    ta_signals.ta_liquidity_score,
    ta_signals.ta_relative_strength_score,
    ta_signals.ta_composite_score,
    ta_signals.ta_action_bias,
    ta_signals.ta_risk_flag,
    ta_signals.rsi_14,
    ta_signals.macd,
    ta_signals.macd_signal,
    ta_signals.macd_histogram,
    ta_signals.adx_14,
    ta_signals.plus_di_14,
    ta_signals.minus_di_14,
    ta_signals.atr_pct_14,
    ta_signals.bollinger_band_width,
    ta_signals.volume_zscore_20,
    ta_signals.mfi_14,
    ta_signals.relative_strength_vs_vn30_20d,
    ta_signals.relative_strength_vs_vnindex_20d,
    ta_signals.beta_vs_vn30_60d,
    ta_signals.correlation_vs_vn30_60d,
    market_regime.market_regime,
    market_regime.volatility_regime,
    market_regime.regime_score,
    market_regime.breadth_score,
    market_regime.advancer_ratio,
    quality.data_quality_status,
    quality.stale_days,
    quality.manifest_row_count_diff,
    case
        when quality.data_quality_status = 'pass'
         and market_regime.market_regime = 'bullish'
         and symbol_features.trend_state = 'uptrend'
         and symbol_features.relative_strength_20d > 0
            then 'candidate_long'
        when quality.data_quality_status <> 'pass'
            then 'blocked_data_quality'
        when market_regime.market_regime = 'bearish'
            then 'risk_off_watch'
        else 'watch'
    end as agent_candidate_state,
    symbol_features.symbol_date_key
from symbol_features
left join market_regime
    on symbol_features.trading_date = market_regime.trading_date
left join quality
    on symbol_features.symbol = quality.symbol
left join ta_signals
    on symbol_features.symbol = ta_signals.symbol
   and symbol_features.trading_date = ta_signals.trading_date
