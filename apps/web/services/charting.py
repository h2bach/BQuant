"""Chart data loaders and TradingView Lightweight Charts render specs."""

from __future__ import annotations

import html as html_lib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

import pandas as pd

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection
from warehouse.refresh_state import get_refresh_version


OVERVIEW_PERIOD_OPTIONS = ["6M", "1Y", "3Y", "5Y", "10Y", "ALL"]
MARKET_CANDLE_OPTIONS = ["VN30", "VNIndex"]
SYMBOL_DAILY_RANGE_OPTIONS = ["3M", "6M", "1Y", "3Y", "5Y", "10Y", "ALL"]
SYMBOL_INTRADAY_RANGE_OPTIONS = ["5D", "20D", "60D"]

VNINDEX_LABEL = "VNIndex"
VN30_LABEL = "VN30"

COLOR_BG = "#0f172a"
COLOR_PANEL = "#111827"
COLOR_GRID = "rgba(148, 163, 184, 0.16)"
COLOR_TEXT = "#e2e8f0"
COLOR_MUTED = "#94a3b8"
COLOR_ACCENT = "#22d3ee"
COLOR_BENCH = "rgba(226, 232, 240, 0.48)"
COLOR_EMA_FAST = "#f59e0b"
COLOR_EMA_SLOW = "#a78bfa"
COLOR_EMA_LONG = "#38bdf8"
COLOR_UP = "#22c55e"
COLOR_DOWN = "#ef4444"
COLOR_RSI = "#c084fc"
COLOR_MACD = "#60a5fa"
COLOR_SIGNAL = "#f97316"
COLOR_VWAP = "#eab308"
COLOR_SMA = "#cbd5e1"
COLOR_BOLLINGER = "rgba(34, 211, 238, 0.72)"
COLOR_DONCHIAN = "rgba(244, 114, 182, 0.72)"
COLOR_KELTNER = "rgba(52, 211, 153, 0.72)"
COLOR_SUPERTREND = "#fb7185"
COLOR_STOCH = "#2dd4bf"
COLOR_MFI = "#facc15"
COLOR_ADX = "#818cf8"
COLOR_CCI = "#f472b6"
COLOR_VOLUME_SIGNAL = "#94a3b8"
DEFAULT_DAILY_VIEW_BARS = 60
PRICE_COLUMNS = ("open", "high", "low", "close")
LIGHTWEIGHT_CHARTS_CDN = "https://unpkg.com/lightweight-charts@5/dist/lightweight-charts.standalone.production.js"

LOGGER = BQuantLogger("web_charting", component="web", subcomponent="charting", default_channel="web")


@dataclass(frozen=True)
class LightweightChartSpec:
    """Render contract for one TradingView Lightweight Charts block.

    Attributes:
        element_id: Unique DOM id used by NiceGUI HTML and JavaScript hooks.
        html: Static HTML container inserted into the NiceGUI page.
        script: JavaScript snippet executed after the container is mounted.
    """

    element_id: str
    html: str
    script: str


def _period_to_offset(period_key: str) -> pd.DateOffset | None:
    """Map a UI range preset to a pandas trailing offset.

    Args:
        period_key: Range key emitted by dashboard or Symbol Explorer controls.
            Supported values include `3M`, `1Y`, `10Y`, intraday day windows,
            and `ALL`.

    Returns:
        DateOffset used to trim a trailing window, or `None` when the caller
        should keep the full available history.
    """
    mapping = {
        "3M": pd.DateOffset(months=3),
        "6M": pd.DateOffset(months=6),
        "1Y": pd.DateOffset(years=1),
        "3Y": pd.DateOffset(years=3),
        "5Y": pd.DateOffset(years=5),
        "10Y": pd.DateOffset(years=10),
        "5D": pd.DateOffset(days=5),
        "20D": pd.DateOffset(days=20),
        "60D": pd.DateOffset(days=60),
        "ALL": None,
    }
    return mapping.get(period_key)


def _filter_by_period(frame: pd.DataFrame, date_col: str, period_key: str) -> pd.DataFrame:
    """Trim a time series frame to a requested trailing period.

    Args:
        frame: Input DataFrame containing the time column used for filtering.
        date_col: Column name containing daily dates or intraday timestamps.
        period_key: UI range preset understood by `_period_to_offset`.

    Returns:
        Copy of `frame` restricted to the trailing period. Empty input returns
        an empty copy, and `ALL` returns a full copy.
    """
    if frame.empty:
        return frame.copy()
    offset = _period_to_offset(period_key)
    if offset is None:
        return frame.copy()
    end_ts = pd.to_datetime(frame[date_col]).max()
    start_ts = end_ts - offset
    return frame.loc[pd.to_datetime(frame[date_col]) >= start_ts].copy()


def _ema(series: pd.Series, span: int) -> pd.Series:
    """Calculate an exponential moving average over a numeric series.

    Args:
        series: Price or volume series. Values are coerced to numeric before
            calculation so source strings do not break chart rendering.
        span: EMA span passed to `Series.ewm`.

    Returns:
        Series aligned to `series.index` with the computed EMA values.
    """
    return pd.to_numeric(series, errors="coerce").ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI using exponentially weighted gains and losses.

    Args:
        series: Closing-price series used to compute bar-to-bar deltas.
        period: RSI lookback period. The dashboard defaults to `14`.

    Returns:
        RSI series in the 0-100 range where enough history exists. Missing
        warm-up values are backfilled for a continuous chart trace.
    """
    close = pd.to_numeric(series, errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return pd.to_numeric(rsi, errors="coerce").bfill()


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Calculate MACD line, signal line, and histogram.

    Args:
        series: Closing-price series used as MACD input.
        fast: Fast EMA span.
        slow: Slow EMA span.
        signal: Signal-line EMA span applied to the MACD line.

    Returns:
        DataFrame with `macd`, `signal`, and `histogram` columns aligned to
        the input series index.
    """
    close = pd.to_numeric(series, errors="coerce")
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": macd_line - signal_line,
        }
    )


def _sma(series: pd.Series, period: int) -> pd.Series:
    """Calculate a simple moving average.

    Args:
        series: Numeric source series such as close price or volume.
        period: Rolling window length in bars.

    Returns:
        Series aligned to the input index with SMA values.
    """
    return pd.to_numeric(series, errors="coerce").rolling(period, min_periods=max(2, period // 3)).mean()


def _true_range(frame: pd.DataFrame) -> pd.Series:
    """Calculate True Range for an OHLC frame.

    Args:
        frame: DataFrame containing `high`, `low`, and `close` columns.

    Returns:
        Series containing the max of intrabar range and gap-adjusted ranges.
    """
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    previous_close = close.shift(1)
    ranges = pd.concat([(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1)
    return ranges.max(axis=1)


def _atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Wilder-style Average True Range.

    Args:
        frame: OHLC frame used to calculate True Range.
        period: Wilder smoothing window.

    Returns:
        ATR series aligned to `frame.index`.
    """
    return _true_range(frame).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _bollinger_bands(series: pd.Series, period: int = 20, deviations: float = 2.0) -> pd.DataFrame:
    """Calculate Bollinger Bands.

    Args:
        series: Closing-price series.
        period: Rolling mean and standard-deviation window.
        deviations: Number of standard deviations around the middle band.

    Returns:
        DataFrame with `bb_middle`, `bb_upper`, `bb_lower`, and
        `bb_width_pct` columns.
    """
    close = pd.to_numeric(series, errors="coerce")
    middle = close.rolling(period, min_periods=max(2, period // 2)).mean()
    sigma = close.rolling(period, min_periods=max(2, period // 2)).std()
    upper = middle + deviations * sigma
    lower = middle - deviations * sigma
    width_pct = (upper - lower) / middle.where(middle != 0) * 100.0
    return pd.DataFrame({"bb_middle": middle, "bb_upper": upper, "bb_lower": lower, "bb_width_pct": width_pct})


def _donchian_channel(frame: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Calculate Donchian channel boundaries.

    Args:
        frame: OHLC frame containing `high` and `low`.
        period: Rolling breakout window.

    Returns:
        DataFrame with `donchian_upper`, `donchian_lower`, and
        `donchian_middle` columns.
    """
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    upper = high.rolling(period, min_periods=max(2, period // 2)).max()
    lower = low.rolling(period, min_periods=max(2, period // 2)).min()
    return pd.DataFrame({"donchian_upper": upper, "donchian_lower": lower, "donchian_middle": (upper + lower) / 2.0})


def _keltner_channel(frame: pd.DataFrame, period: int = 20, multiplier: float = 2.0) -> pd.DataFrame:
    """Calculate Keltner channel boundaries.

    Args:
        frame: OHLC frame containing `high`, `low`, and `close`.
        period: EMA and ATR smoothing window.
        multiplier: ATR multiplier applied around the EMA center line.

    Returns:
        DataFrame with `keltner_middle`, `keltner_upper`, and
        `keltner_lower` columns.
    """
    middle = _ema(frame["close"], period)
    atr = _atr(frame, period)
    return pd.DataFrame(
        {
            "keltner_middle": middle,
            "keltner_upper": middle + multiplier * atr,
            "keltner_lower": middle - multiplier * atr,
        }
    )


def _stochastic(frame: pd.DataFrame, period: int = 14, smooth: int = 3) -> pd.DataFrame:
    """Calculate slow stochastic oscillator values.

    Args:
        frame: OHLC frame containing `high`, `low`, and `close`.
        period: Lookback window for highest high and lowest low.
        smooth: Moving-average window for `%D`.

    Returns:
        DataFrame with `stoch_k` and `stoch_d` columns in the 0-100 range.
    """
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    lowest_low = low.rolling(period, min_periods=max(2, period // 2)).min()
    highest_high = high.rolling(period, min_periods=max(2, period // 2)).max()
    denominator = (highest_high - lowest_low).where((highest_high - lowest_low) != 0)
    stoch_k = ((close - lowest_low) / denominator * 100.0).clip(0, 100)
    stoch_d = stoch_k.rolling(smooth, min_periods=1).mean()
    return pd.DataFrame({"stoch_k": stoch_k, "stoch_d": stoch_d})


def _williams_r(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Williams %R.

    Args:
        frame: OHLC frame containing `high`, `low`, and `close`.
        period: Lookback window.

    Returns:
        Williams %R series in the -100 to 0 range.
    """
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    highest_high = high.rolling(period, min_periods=max(2, period // 2)).max()
    lowest_low = low.rolling(period, min_periods=max(2, period // 2)).min()
    denominator = (highest_high - lowest_low).where((highest_high - lowest_low) != 0)
    return (-100.0 * (highest_high - close) / denominator).clip(-100, 0)


def _roc(series: pd.Series, period: int = 20) -> pd.Series:
    """Calculate Rate of Change in percentage points.

    Args:
        series: Closing-price series.
        period: Lookback window for the return calculation.

    Returns:
        Percentage return over `period` bars.
    """
    close = pd.to_numeric(series, errors="coerce")
    return close.pct_change(period) * 100.0


def _cci(frame: pd.DataFrame, period: int = 20) -> pd.Series:
    """Calculate Commodity Channel Index.

    Args:
        frame: OHLC frame containing `high`, `low`, and `close`.
        period: Rolling typical-price window.

    Returns:
        CCI series. Positive values indicate strength above the typical-price
        mean; negative values indicate weakness below it.
    """
    typical_price = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    typical_price = pd.to_numeric(typical_price, errors="coerce")
    average = typical_price.rolling(period, min_periods=max(2, period // 2)).mean()
    mean_deviation = typical_price.rolling(period, min_periods=max(2, period // 2)).apply(
        lambda values: abs(values - values.mean()).mean(),
        raw=False,
    )
    return (typical_price - average) / (0.015 * mean_deviation.where(mean_deviation != 0))


def _obv(frame: pd.DataFrame) -> pd.Series:
    """Calculate On-Balance Volume.

    Args:
        frame: OHLCV frame containing `close` and `volume`.

    Returns:
        Cumulative OBV series where volume is added on up bars and subtracted
        on down bars.
    """
    close_delta = pd.to_numeric(frame["close"], errors="coerce").diff().fillna(0)
    volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0)
    direction = close_delta.apply(lambda value: 1 if value > 0 else (-1 if value < 0 else 0))
    return (direction * volume).cumsum()


def _chaikin_money_flow(frame: pd.DataFrame, period: int = 20) -> pd.Series:
    """Calculate Chaikin Money Flow.

    Args:
        frame: OHLCV frame containing `high`, `low`, `close`, and `volume`.
        period: Rolling money-flow window.

    Returns:
        CMF series generally bounded between -1 and 1.
    """
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0)
    spread = (high - low).where((high - low) != 0)
    multiplier = ((close - low) - (high - close)) / spread
    money_flow_volume = multiplier.fillna(0) * volume
    volume_sum = volume.rolling(period, min_periods=max(2, period // 2)).sum().where(lambda values: values != 0)
    return money_flow_volume.rolling(period, min_periods=max(2, period // 2)).sum() / volume_sum


def _money_flow_index(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Money Flow Index.

    Args:
        frame: OHLCV frame containing `high`, `low`, `close`, and `volume`.
        period: Rolling money-flow window.

    Returns:
        MFI oscillator in the 0-100 range.
    """
    typical_price = pd.to_numeric((frame["high"] + frame["low"] + frame["close"]) / 3.0, errors="coerce")
    money_flow = typical_price * pd.to_numeric(frame["volume"], errors="coerce").fillna(0)
    positive_flow = money_flow.where(typical_price.diff() > 0, 0.0)
    negative_flow = money_flow.where(typical_price.diff() < 0, 0.0)
    positive_sum = positive_flow.rolling(period, min_periods=max(2, period // 2)).sum()
    negative_sum = negative_flow.rolling(period, min_periods=max(2, period // 2)).sum()
    money_ratio = positive_sum / negative_sum.where(negative_sum != 0)
    return (100.0 - (100.0 / (1.0 + money_ratio))).clip(0, 100)


def _adx(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calculate ADX and directional movement lines.

    Args:
        frame: OHLC frame containing `high`, `low`, and `close`.
        period: Wilder smoothing window.

    Returns:
        DataFrame with `adx14`, `plus_di14`, and `minus_di14` columns.
    """
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(0.0, index=frame.index)
    minus_dm = pd.Series(0.0, index=frame.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move
    atr = _atr(frame, period).where(lambda values: values != 0)
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).where(lambda values: values != 0)
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return pd.DataFrame({"adx14": adx.clip(0, 100), "plus_di14": plus_di.clip(0, 100), "minus_di14": minus_di.clip(0, 100)})


def _aroon(frame: pd.DataFrame, period: int = 25) -> pd.DataFrame:
    """Calculate Aroon Up and Aroon Down.

    Args:
        frame: OHLC frame containing `high` and `low`.
        period: Rolling lookback window used to find recent extremes.

    Returns:
        DataFrame with `aroon_up` and `aroon_down` columns in the 0-100 range.
    """
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")

    def latest_extreme_score(values: pd.Series, *, use_max: bool) -> float:
        """Score how recently the rolling high/low occurred.

        Args:
            values: Rolling window passed by pandas.
            use_max: When true, score the latest high; otherwise score the
                latest low.

        Returns:
            Aroon-style score in the 0-100 range.
        """
        position = int(values.argmax() if use_max else values.argmin())
        denominator = max(len(values) - 1, 1)
        return position / denominator * 100.0

    return pd.DataFrame(
        {
            "aroon_up": high.rolling(period, min_periods=max(2, period // 2)).apply(
                lambda values: latest_extreme_score(values, use_max=True),
                raw=False,
            ),
            "aroon_down": low.rolling(period, min_periods=max(2, period // 2)).apply(
                lambda values: latest_extreme_score(values, use_max=False),
                raw=False,
            ),
        }
    )


def _supertrend(frame: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    """Calculate the Supertrend trailing level.

    Args:
        frame: OHLC frame containing `high`, `low`, and `close`.
        period: ATR window used by the trailing band.
        multiplier: ATR multiplier applied to the basic upper/lower bands.

    Returns:
        Series containing the active Supertrend band. It behaves like a
        trailing support level in uptrends and resistance in downtrends.
    """
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    atr = _atr(frame, period)
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr
    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    supertrend = pd.Series(float("nan"), index=frame.index, dtype="float64")
    in_uptrend = True

    for index in range(1, len(frame)):
        previous = frame.index[index - 1]
        current = frame.index[index]
        if pd.isna(basic_upper.loc[current]) or pd.isna(basic_lower.loc[current]):
            continue
        if pd.isna(final_upper.loc[previous]):
            final_upper.loc[current] = basic_upper.loc[current]
        elif basic_upper.loc[current] < final_upper.loc[previous] or close.loc[previous] > final_upper.loc[previous]:
            final_upper.loc[current] = basic_upper.loc[current]
        else:
            final_upper.loc[current] = final_upper.loc[previous]
        if pd.isna(final_lower.loc[previous]):
            final_lower.loc[current] = basic_lower.loc[current]
        elif basic_lower.loc[current] > final_lower.loc[previous] or close.loc[previous] < final_lower.loc[previous]:
            final_lower.loc[current] = basic_lower.loc[current]
        else:
            final_lower.loc[current] = final_lower.loc[previous]
        previous_upper = final_upper.loc[previous] if pd.notna(final_upper.loc[previous]) else final_upper.loc[current]
        previous_lower = final_lower.loc[previous] if pd.notna(final_lower.loc[previous]) else final_lower.loc[current]
        if close.loc[current] > previous_upper:
            in_uptrend = True
        elif close.loc[current] < previous_lower:
            in_uptrend = False
        supertrend.loc[current] = final_lower.loc[current] if in_uptrend else final_upper.loc[current]
    return supertrend


def _normalize_to_100(series: pd.Series, window: int = 120) -> pd.Series:
    """Normalize a numeric series to a rolling 0-100 plotting scale.

    Args:
        series: Raw indicator series with arbitrary magnitude.
        window: Rolling window used for robust min/max bounds.

    Returns:
        Series clipped to 0-100. This is used only for the hidden-axis signal
        panel so indicators with different native units can be compared by
        shape without distorting each other.
    """
    values = pd.to_numeric(series, errors="coerce")
    low = values.rolling(window, min_periods=max(5, window // 4)).quantile(0.05)
    high = values.rolling(window, min_periods=max(5, window // 4)).quantile(0.95)
    denominator = (high - low).where((high - low) != 0)
    return ((values - low) / denominator * 100.0).clip(0, 100)


def _bounded_oscillator(series: pd.Series, lower: float, upper: float) -> pd.Series:
    """Map a bounded oscillator to the shared 0-100 signal-panel scale.

    Args:
        series: Oscillator series with known lower and upper bounds.
        lower: Native lower bound.
        upper: Native upper bound.

    Returns:
        Series clipped to the normalized 0-100 plotting range.
    """
    values = pd.to_numeric(series, errors="coerce")
    denominator = upper - lower
    return ((values - lower) / denominator * 100.0).clip(0, 100)


def _add_indicator_suite(frame: pd.DataFrame, *, include_intraday_vwap: bool = False, include_long_ma: bool = True) -> pd.DataFrame:
    """Attach the shared BQuant technical indicator suite to an OHLCV frame.

    Args:
        frame: Sorted OHLCV DataFrame with `open`, `high`, `low`, `close`, and
            `volume` columns. Intraday input must also include `session_date`
            when `include_intraday_vwap` is true.
        include_intraday_vwap: Whether to add session-reset VWAP for 15-minute
            charts.
        include_long_ma: Whether to compute 200-period trend lines.

    Returns:
        Copy of `frame` with trend, momentum, volatility, and volume features:
        EMA/SMA, Bollinger, Donchian, Keltner, Supertrend, RSI, Stochastic,
        Williams %R, ROC, CCI, MACD, ATR, ADX/DI, Aroon, MFI, CMF, and OBV.

    Notes:
        Raw indicator columns are preserved for metrics. Columns ending in
        `_plot` are normalized to the 0-100 panel scale used by the chart.
    """
    working = frame.copy()
    for column in ["open", "high", "low", "close", "volume"]:
        working[column] = pd.to_numeric(working[column], errors="coerce")

    working["ema20"] = _ema(working["close"], 20)
    working["ema50"] = _ema(working["close"], 50)
    working["sma20"] = _sma(working["close"], 20)
    working["sma50"] = _sma(working["close"], 50)
    if include_long_ma:
        working["ema200"] = _ema(working["close"], 200)
        working["sma200"] = _sma(working["close"], 200)
    if include_intraday_vwap:
        working["vwap"] = _session_vwap(working)

    bbands = _bollinger_bands(working["close"])
    donchian = _donchian_channel(working)
    keltner = _keltner_channel(working)
    stochastic = _stochastic(working)
    adx = _adx(working)
    aroon = _aroon(working)
    macd = _macd(working["close"])
    working = pd.concat([working, bbands, donchian, keltner, stochastic, adx, aroon, macd], axis=1)

    working["volume_ma20"] = _ema(working["volume"], 20)
    working["supertrend"] = _supertrend(working)
    working["rsi14"] = _rsi(working["close"], 14)
    working["williams_r"] = _williams_r(working)
    working["roc20"] = _roc(working["close"], 20)
    working["cci20"] = _cci(working)
    working["atr14"] = _atr(working, 14)
    working["atr14_pct"] = working["atr14"] / working["close"].where(working["close"] != 0) * 100.0
    working["mfi14"] = _money_flow_index(working, 14)
    working["cmf20"] = _chaikin_money_flow(working, 20)
    working["obv"] = _obv(working)

    working["macd_plot"] = _normalize_to_100(working["macd"])
    working["signal_plot"] = _normalize_to_100(working["signal"])
    working["histogram_plot"] = _normalize_to_100(working["histogram"])
    working["williams_r_plot"] = _bounded_oscillator(working["williams_r"], -100.0, 0.0)
    working["roc20_plot"] = _normalize_to_100(working["roc20"])
    working["cci20_plot"] = _normalize_to_100(working["cci20"])
    working["atr14_pct_plot"] = _normalize_to_100(working["atr14_pct"])
    working["cmf20_plot"] = _bounded_oscillator(working["cmf20"], -1.0, 1.0)
    working["obv_plot"] = _normalize_to_100(working["obv"])
    working["bb_width_pct_plot"] = _normalize_to_100(working["bb_width_pct"])
    return working


def _session_vwap(frame: pd.DataFrame) -> pd.Series:
    """Calculate per-session VWAP for intraday bars.

    Args:
        frame: Intraday OHLCV DataFrame with `high`, `low`, `close`, `volume`,
            and `session_date` columns.

    Returns:
        VWAP series aligned to `frame.index`. The cumulative sums reset for
        each `session_date`.
    """
    working = frame.copy()
    typical_price = (working["high"] + working["low"] + working["close"]) / 3.0
    px_vol = typical_price * working["volume"]
    cumulative_px_vol = px_vol.groupby(working["session_date"]).cumsum()
    cumulative_vol = working["volume"].groupby(working["session_date"]).cumsum().replace(0, pd.NA)
    return cumulative_px_vol / cumulative_vol


def _latest_value(series: pd.Series) -> float | None:
    """Return the latest non-null numeric value from a series.

    Args:
        series: Any pandas series that should represent numeric values.

    Returns:
        Latest finite numeric value as `float`, or `None` when the series has
        no usable value.
    """
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    return float(clean.iloc[-1])


def _fmt_float(value: float | None, suffix: str = "", decimals: int = 2) -> str:
    """Format a numeric metric for compact UI display.

    Args:
        value: Numeric value to display.
        suffix: Optional suffix appended after formatting, such as `%`.
        decimals: Number of decimal places.

    Returns:
        Formatted string or `N/A` when `value` is missing.
    """
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value:.{decimals}f}{suffix}"


def _fmt_pct(value: float | None) -> str:
    """Format a percentage metric with an explicit sign.

    Args:
        value: Percentage-point value, not a decimal fraction.

    Returns:
        Signed percentage string with two decimals, or `N/A` when missing.
    """
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value:+.2f}%"


def _trend_label(price: float | None, fast: float | None, slow: float | None) -> str:
    """Classify a coarse price trend from EMA ordering.

    Args:
        price: Latest close price.
        fast: Latest fast-moving average value.
        slow: Latest slow-moving average value.

    Returns:
        `Bullish`, `Bearish`, `Neutral`, or `N/A` for missing inputs.
    """
    if price is None or fast is None or slow is None:
        return "N/A"
    if price > fast > slow:
        return "Bullish"
    if price < fast < slow:
        return "Bearish"
    return "Neutral"


def _rsi_label(value: float | None) -> str:
    """Convert an RSI value into a coarse sentiment label.

    Args:
        value: Latest RSI value in the 0-100 range.

    Returns:
        One of `Overbought`, `Oversold`, `Positive`, `Negative`, `Neutral`,
        or `N/A`.
    """
    if value is None:
        return "N/A"
    if value >= 70:
        return "Overbought"
    if value <= 30:
        return "Oversold"
    if value >= 55:
        return "Positive"
    if value <= 45:
        return "Negative"
    return "Neutral"


def _macd_label(macd: float | None, signal: float | None) -> str:
    """Convert MACD and signal values into a directional label.

    Args:
        macd: Latest MACD line value.
        signal: Latest MACD signal-line value.

    Returns:
        `Bullish Cross`, `Bearish Cross`, or `N/A`.
    """
    if macd is None or signal is None:
        return "N/A"
    return "Bullish Cross" if macd >= signal else "Bearish Cross"


@lru_cache(maxsize=8)
def _load_local_daily_cached(refresh_version: int) -> pd.DataFrame:
    """Load the full local daily OHLCV dataset behind a refresh-version cache.

    Args:
        refresh_version: Current `dataset_refresh_state.refresh_version` for
            `daily_ohlcv_10y`. It is part of the LRU key so live jobs can force
            the web process to reload data after materialization.

    Returns:
        DataFrame with `symbol`, `trading_date`, `open`, `high`, `low`,
        `close`, and `volume` columns sorted by symbol/date.

    Side Effects:
        Emits structured web logs for cache refresh and stale/empty reads.
    """
    with get_connection(read_only=True) as conn:
        frame = conn.execute(
            """
            SELECT symbol, trading_date, open, high, low, close, volume
            FROM daily_ohlcv_base
            ORDER BY symbol, trading_date
            """
        ).df()
    if frame.empty:
        LOGGER.warning(
            "Local daily dataset is empty while reloading chart cache",
            event_type="stale_data_read",
            status="empty",
            dataset_name="daily_ohlcv_10y",
            refresh_version=refresh_version,
            channel="web",
        )
        return frame
    frame["trading_date"] = pd.to_datetime(frame["trading_date"])
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    latest_date = frame["trading_date"].max().date()
    if latest_date < (datetime.now().date() - timedelta(days=1)):
        LOGGER.warning(
            "Local daily dataset appears stale relative to current date",
            event_type="stale_data_read",
            status="warning",
            dataset_name="daily_ohlcv_10y",
            refresh_version=refresh_version,
            latest_data_date=str(latest_date),
        )
    LOGGER.log_web_event(
        "Reloaded daily chart cache for a new refresh version",
        event_type="cache_refresh",
        status="success",
        dataset_name="daily_ohlcv_10y",
        refresh_version=refresh_version,
        row_count=int(len(frame)),
    )
    return frame


def _load_local_daily() -> pd.DataFrame:
    """Load daily OHLCV using the current refresh version as the cache key.

    Returns:
        Copy of the cached daily OHLCV DataFrame. Returning a copy prevents
        page-specific indicator mutations from polluting shared cached data.
    """
    refresh_version = get_refresh_version("daily_ohlcv_10y")
    return _load_local_daily_cached(refresh_version).copy()


@lru_cache(maxsize=8)
def _load_market_index_daily_cached(refresh_version: int) -> pd.DataFrame:
    """Load canonical daily VNINDEX/VN30 OHLCV with refresh-aware caching.

    Args:
        refresh_version: Current refresh token for `market_index_daily_10y`.

    Returns:
        DataFrame with raw index OHLCV rows from `market_index_daily_base`.
        Symbols are limited to `VNINDEX` and `VN30`.

    Side Effects:
        Logs cache reloads and empty dataset conditions.
    """
    with get_connection(read_only=True) as conn:
        frame = conn.execute(
            """
            SELECT symbol, trading_date, open, high, low, close, adjusted_close, volume, source
            FROM market_index_daily_base
            WHERE symbol IN ('VNINDEX', 'VN30')
            ORDER BY symbol, trading_date
            """
        ).df()
    if frame.empty:
        LOGGER.warning(
            "Market index daily dataset is empty while reloading chart cache",
            event_type="stale_data_read",
            status="empty",
            dataset_name="market_index_daily_10y",
            refresh_version=refresh_version,
            channel="web",
        )
        return frame
    frame["trading_date"] = pd.to_datetime(frame["trading_date"])
    for column in ["open", "high", "low", "close", "adjusted_close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    LOGGER.log_web_event(
        "Reloaded market index chart cache for a new refresh version",
        event_type="cache_refresh",
        status="success",
        dataset_name="market_index_daily_10y",
        refresh_version=refresh_version,
        row_count=int(len(frame)),
    )
    return frame


def _load_market_index_daily() -> pd.DataFrame:
    """Load canonical VNINDEX/VN30 OHLCV using the index refresh version.

    Returns:
        Copy of the cached market index DataFrame so callers can add chart
        columns without mutating the shared cache.
    """
    refresh_version = get_refresh_version("market_index_daily_10y")
    return _load_market_index_daily_cached(refresh_version).copy()


@lru_cache(maxsize=128)
def _load_local_intraday_cached(symbol: str, base_refresh_version: int, delta_refresh_version: int) -> pd.DataFrame:
    """Load merged intraday plot rows for one symbol with refresh-aware caching.

    Args:
        symbol: VN30 ticker to read from `v_intraday_15m_plot_universe`.
        base_refresh_version: Refresh token for `intraday_ohlcv_15m_60d`.
        delta_refresh_version: Refresh token for `intraday_ohlcv_15m_delta`.

    Returns:
        DataFrame with `symbol`, `bar_time`, `session_date`, OHLCV columns.
        The base and delta versions are included in the cache key so live
        updates invalidate stale chart data across processes.

    Side Effects:
        Emits structured logs for empty reads and cache refreshes.
    """
    with get_connection(read_only=True) as conn:
        frame = conn.execute(
            """
            SELECT symbol, bar_time, session_date, open, high, low, close, volume
            FROM v_intraday_15m_plot_universe
            WHERE symbol = ?
            ORDER BY bar_time
            """,
            [symbol],
        ).df()
    if frame.empty:
        LOGGER.warning(
            f"Local intraday plot dataset is empty for {symbol}",
            event_type="stale_data_read",
            status="empty",
            dataset_name="intraday_ohlcv_15m_delta",
            symbol=symbol,
            base_refresh_version=base_refresh_version,
            delta_refresh_version=delta_refresh_version,
        )
        return frame
    frame["bar_time"] = pd.to_datetime(frame["bar_time"])
    frame["session_date"] = pd.to_datetime(frame["session_date"]).dt.date
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    LOGGER.log_web_event(
        f"Reloaded intraday chart cache for {symbol}",
        event_type="cache_refresh",
        status="success",
        dataset_name="intraday_ohlcv_15m_delta",
        symbol=symbol,
        base_refresh_version=base_refresh_version,
        delta_refresh_version=delta_refresh_version,
        row_count=int(len(frame)),
    )
    return frame


def _load_local_intraday(symbol: str) -> pd.DataFrame:
    """Load intraday bars for one symbol from the current base+delta plot view.

    Args:
        symbol: VN30 ticker to load.

    Returns:
        Copy of the merged intraday DataFrame for that symbol.
    """
    base_refresh_version = get_refresh_version("intraday_ohlcv_15m_60d")
    delta_refresh_version = get_refresh_version("intraday_ohlcv_15m_delta")
    return _load_local_intraday_cached(symbol, base_refresh_version, delta_refresh_version).copy()


def load_market_overview_data(period_key: str = "1Y") -> pd.DataFrame:
    """Build market overview rows from index OHLCV and VN30 breadth.

    Args:
        period_key: Dashboard period preset used to trim both index and
            constituent stock history before alignment.

    Returns:
        DataFrame keyed by `trading_date` with:
        `vnindex_*` OHLCV/source columns, `vn30_*` OHLCV/source columns,
        `constituent_volume`, `advancers`, `decliners`, `active_symbols`,
        `breadth_pct`, and `relative_strength`.

    Notes:
        VNINDEX/VN30 candles come from `market_index_daily_base`, while
        breadth and aggregate volume come from VN30 constituents in
        `daily_ohlcv_base`.
    """
    stock_frame = _filter_by_period(_load_local_daily(), "trading_date", period_key)
    index_frame = _filter_by_period(_load_market_index_daily(), "trading_date", period_key)
    if stock_frame.empty or index_frame.empty:
        return pd.DataFrame()

    working = stock_frame.sort_values(["symbol", "trading_date"]).copy()
    working["prev_close"] = working.groupby("symbol")["close"].shift(1)
    working["is_advancer"] = working["close"] > working["prev_close"]
    working["is_decliner"] = working["close"] < working["prev_close"]
    breadth = (
        working.groupby("trading_date")
        .agg(
            constituent_volume=("volume", "sum"),
            advancers=("is_advancer", "sum"),
            decliners=("is_decliner", "sum"),
            active_symbols=("symbol", "nunique"),
        )
        .reset_index()
    )
    breadth["breadth_pct"] = breadth["advancers"] / breadth["active_symbols"].replace(0, pd.NA) * 100.0

    output = breadth.copy()
    for symbol, prefix in [("VNINDEX", "vnindex"), ("VN30", "vn30")]:
        subset = index_frame.loc[index_frame["symbol"] == symbol].copy()
        if subset.empty:
            return pd.DataFrame()
        renamed = subset.rename(
            columns={
                "open": f"{prefix}_open",
                "high": f"{prefix}_high",
                "low": f"{prefix}_low",
                "close": f"{prefix}_close",
                "volume": f"{prefix}_volume",
                "source": f"{prefix}_source",
            }
        )
        keep_columns = [
            "trading_date",
            f"{prefix}_open",
            f"{prefix}_high",
            f"{prefix}_low",
            f"{prefix}_close",
            f"{prefix}_volume",
            f"{prefix}_source",
        ]
        output = output.merge(renamed[keep_columns], on="trading_date", how="inner")

    if output.empty:
        return output
    output = output.sort_values("trading_date").reset_index(drop=True)
    output["relative_strength"] = (output["vn30_close"] / output["vnindex_close"]) * 100.0
    return output


def _load_symbol_daily(symbol: str) -> pd.DataFrame:
    """Load daily OHLCV rows for a single symbol.

    Args:
        symbol: VN30 constituent ticker in canonical uppercase form.

    Returns:
        DataFrame sorted by `trading_date` with daily OHLCV columns. Unknown
        symbols return an empty DataFrame with the daily cache schema.
    """
    local = _load_local_daily()
    frame = local.loc[local["symbol"] == symbol].copy()
    return frame.sort_values("trading_date").reset_index(drop=True)


def _prepare_symbol_daily(symbol: str, period_key: str) -> pd.DataFrame:
    """Prepare daily OHLCV plus indicators for Symbol Explorer.

    Args:
        symbol: VN30 constituent ticker.
        period_key: Daily range preset such as `6M`, `5Y`, or `ALL`.

    Returns:
        DataFrame with daily OHLCV plus the shared technical indicator suite:
        trend overlays, momentum oscillators, volatility bands, volume-flow
        indicators, and normalized `_plot` columns for the signal panel.
    """
    frame = _filter_by_period(_load_symbol_daily(symbol), "trading_date", period_key)
    if frame.empty:
        return frame
    frame = frame.sort_values("trading_date").copy()
    return _add_indicator_suite(frame, include_intraday_vwap=False, include_long_ma=True)


def _prepare_symbol_intraday(symbol: str, period_key: str) -> pd.DataFrame:
    """Prepare intraday OHLCV plus indicators for Symbol Explorer.

    Args:
        symbol: VN30 constituent ticker.
        period_key: Intraday range preset such as `5D`, `20D`, or `60D`.

    Returns:
        DataFrame with 15-minute OHLCV plus the shared technical indicator
        suite and intraday session VWAP.
    """
    frame = _filter_by_period(_load_local_intraday(symbol), "bar_time", period_key)
    if frame.empty:
        return frame
    frame = frame.sort_values("bar_time").copy()
    return _add_indicator_suite(frame, include_intraday_vwap=True, include_long_ma=False)


def _symbol_relative_strength(symbol: str, lookback_days: int = 20) -> float | None:
    """Measure a symbol's trailing relative performance against VN30.

    Args:
        symbol: VN30 constituent ticker.
        lookback_days: Number of trailing trading rows used for the comparison.

    Returns:
        Percentage-point difference between the stock return and VN30 return,
        or `None` when either series is unavailable.
    """
    basket = load_market_overview_data("1Y")
    stock = _load_symbol_daily(symbol)
    if basket.empty or stock.empty:
        return None
    merged = stock.merge(basket[["trading_date", "vn30_close"]], on="trading_date", how="inner").sort_values("trading_date")
    if len(merged) < 2:
        return None
    merged = merged.tail(max(lookback_days + 1, 2))
    stock_ret = merged["close"].iloc[-1] / merged["close"].iloc[0] - 1.0
    basket_ret = merged["vn30_close"].iloc[-1] / merged["vn30_close"].iloc[0] - 1.0
    return (stock_ret - basket_ret) * 100.0


def _clean_number(value: Any) -> float | None:
    """Convert a value to a JSON-safe finite float.

    Args:
        value: Candidate numeric value from pandas, Python, or DuckDB.

    Returns:
        Finite `float`, or `None` for missing/non-numeric/infinite values.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _chart_time(value: Any, *, intraday: bool) -> str | int:
    """Convert pandas timestamps into Lightweight Charts time values.

    Args:
        value: Date-like or timestamp-like value.
        intraday: When true, convert to a Unix timestamp in seconds; otherwise
            return an ISO date string for daily charts.

    Returns:
        Integer Unix timestamp for intraday charts or `YYYY-MM-DD` string for
        daily charts.

    Notes:
        Naive intraday timestamps are treated as `Asia/Ho_Chi_Minh` because
        local market bars are stored without timezone in DuckDB.
    """
    timestamp = pd.Timestamp(value)
    if intraday:
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("Asia/Ho_Chi_Minh")
        return int(timestamp.timestamp())
    return timestamp.strftime("%Y-%m-%d")


def _candlestick_data(frame: pd.DataFrame, time_col: str, *, intraday: bool) -> list[dict[str, Any]]:
    """Build candlestick records for Lightweight Charts.

    Args:
        frame: Prepared OHLCV DataFrame containing `open`, `high`, `low`,
            `close`, and the selected time column.
        time_col: Name of the date/timestamp column to encode.
        intraday: Whether timestamps should be encoded as Unix seconds.

    Returns:
        List of dictionaries with `time`, `open`, `high`, `low`, and `close`.
        Rows with incomplete OHLC values are skipped.
    """
    rows: list[dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        payload = {
            "time": _chart_time(getattr(row, time_col), intraday=intraday),
            "open": _clean_number(getattr(row, "open")),
            "high": _clean_number(getattr(row, "high")),
            "low": _clean_number(getattr(row, "low")),
            "close": _clean_number(getattr(row, "close")),
        }
        if all(payload[key] is not None for key in ["open", "high", "low", "close"]):
            rows.append(payload)
    return rows


def _line_data(frame: pd.DataFrame, time_col: str, value_col: str, *, intraday: bool) -> list[dict[str, Any]]:
    """Build line-series records for Lightweight Charts.

    Args:
        frame: Prepared chart DataFrame.
        time_col: Date/timestamp column used for the x-axis.
        value_col: Numeric column to render as a line.
        intraday: Whether timestamps should be encoded as Unix seconds.

    Returns:
        List of `{time, value}` dictionaries. Missing values are skipped so
        optional indicators can be toggled without invalid JSON.
    """
    rows: list[dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        value = _clean_number(getattr(row, value_col))
        if value is None:
            continue
        rows.append({"time": _chart_time(getattr(row, time_col), intraday=intraday), "value": value})
    return rows


def _histogram_data(
    frame: pd.DataFrame,
    time_col: str,
    value_col: str,
    *,
    intraday: bool,
    colors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build histogram records, optionally with per-bar colors.

    Args:
        frame: Prepared chart DataFrame.
        time_col: Date/timestamp column used for the x-axis.
        value_col: Numeric column to render as bars.
        intraday: Whether timestamps should be encoded as Unix seconds.
        colors: Optional per-row color list aligned to `frame` order.

    Returns:
        List of histogram points with `time`, `value`, and optional `color`.
    """
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(frame.itertuples(index=False)):
        value = _clean_number(getattr(row, value_col))
        if value is None:
            continue
        payload = {"time": _chart_time(getattr(row, time_col), intraday=intraday), "value": value}
        if colors is not None and index < len(colors):
            payload["color"] = colors[index]
        rows.append(payload)
    return rows


def _volume_colors(frame: pd.DataFrame) -> list[str]:
    """Return translucent volume colors keyed to close-to-close direction.

    Args:
        frame: Prepared OHLCV DataFrame containing a `close` column.

    Returns:
        Color list aligned to `frame` rows. Green marks non-negative
        close-to-close movement; red marks negative movement.
    """
    close_delta = pd.to_numeric(frame["close"], errors="coerce").pct_change().fillna(0)
    return ["rgba(34,197,94,0.22)" if value >= 0 else "rgba(239,68,68,0.22)" for value in close_delta]


def _macd_histogram_colors(frame: pd.DataFrame) -> list[str]:
    """Return MACD histogram colors keyed to positive or negative bars.

    Args:
        frame: Prepared indicator DataFrame containing `histogram`.

    Returns:
        Color list aligned to `frame` rows. Green marks non-negative histogram
        values; red marks negative values.
    """
    return ["rgba(34,197,94,0.65)" if value >= 0 else "rgba(239,68,68,0.65)" for value in frame["histogram"].fillna(0)]


def _advanced_price_lines(*, include_vwap: bool = False, include_long_ma: bool = True) -> list[dict[str, Any]]:
    """Build optional price-overlay indicator definitions.

    Args:
        include_vwap: Whether to include session VWAP for intraday charts.
        include_long_ma: Whether 200-period averages are available.

    Returns:
        List of price-line metadata dictionaries consumed by `_build_payload`.
        Every dictionary defines `key`, `column`, `label`, `color`, and
        default `visible` state.
    """
    lines = [
        {"key": "ema20", "column": "ema20", "label": "EMA 20", "color": COLOR_EMA_FAST, "visible": True},
        {"key": "ema50", "column": "ema50", "label": "EMA 50", "color": COLOR_EMA_SLOW, "visible": True},
        {"key": "sma20", "column": "sma20", "label": "SMA 20", "color": COLOR_SMA, "visible": False},
        {"key": "sma50", "column": "sma50", "label": "SMA 50", "color": "rgba(203,213,225,0.72)", "visible": False},
        {"key": "bb_upper", "column": "bb_upper", "label": "BB Upper", "color": COLOR_BOLLINGER, "visible": False},
        {"key": "bb_middle", "column": "bb_middle", "label": "BB Mid", "color": "rgba(34,211,238,0.42)", "visible": False},
        {"key": "bb_lower", "column": "bb_lower", "label": "BB Lower", "color": COLOR_BOLLINGER, "visible": False},
        {"key": "donchian_upper", "column": "donchian_upper", "label": "Donchian High", "color": COLOR_DONCHIAN, "visible": False},
        {"key": "donchian_lower", "column": "donchian_lower", "label": "Donchian Low", "color": COLOR_DONCHIAN, "visible": False},
        {"key": "keltner_upper", "column": "keltner_upper", "label": "Keltner Upper", "color": COLOR_KELTNER, "visible": False},
        {"key": "keltner_middle", "column": "keltner_middle", "label": "Keltner Mid", "color": "rgba(52,211,153,0.42)", "visible": False},
        {"key": "keltner_lower", "column": "keltner_lower", "label": "Keltner Lower", "color": COLOR_KELTNER, "visible": False},
        {"key": "supertrend", "column": "supertrend", "label": "Supertrend", "color": COLOR_SUPERTREND, "visible": False},
    ]
    if include_long_ma:
        lines.extend(
            [
                {"key": "ema200", "column": "ema200", "label": "EMA 200", "color": COLOR_EMA_LONG, "visible": False},
                {"key": "sma200", "column": "sma200", "label": "SMA 200", "color": "rgba(56,189,248,0.62)", "visible": False},
            ]
        )
    if include_vwap:
        lines.insert(2, {"key": "vwap", "column": "vwap", "label": "VWAP", "color": COLOR_VWAP, "visible": True})
    return lines


def _signal_line_specs() -> list[dict[str, Any]]:
    """Return signal-panel line indicator definitions.

    Returns:
        List of line-series metadata. Columns ending in `_plot` are normalized
        to the shared 0-100 signal scale; bounded oscillators already live on
        that scale.
    """
    return [
        {"key": "rsi14", "column": "rsi14", "raw_column": "rsi14", "label": "RSI 14", "color": COLOR_RSI, "visible": False},
        {"key": "stoch_k", "column": "stoch_k", "raw_column": "stoch_k", "label": "Stoch %K", "color": COLOR_STOCH, "visible": False},
        {"key": "stoch_d", "column": "stoch_d", "raw_column": "stoch_d", "label": "Stoch %D", "color": "rgba(45,212,191,0.58)", "visible": False},
        {"key": "williams_r_plot", "column": "williams_r_plot", "raw_column": "williams_r", "label": "Williams %R", "color": "#fb7185", "visible": False},
        {"key": "mfi14", "column": "mfi14", "raw_column": "mfi14", "label": "MFI 14", "color": COLOR_MFI, "visible": False},
        {"key": "cci20_plot", "column": "cci20_plot", "raw_column": "cci20", "label": "CCI 20", "color": COLOR_CCI, "visible": False},
        {"key": "roc20_plot", "column": "roc20_plot", "raw_column": "roc20", "label": "ROC 20", "color": "#38bdf8", "visible": False},
        {"key": "adx14", "column": "adx14", "raw_column": "adx14", "label": "ADX 14", "color": COLOR_ADX, "visible": False},
        {"key": "plus_di14", "column": "plus_di14", "raw_column": "plus_di14", "label": "+DI 14", "color": COLOR_UP, "visible": False},
        {"key": "minus_di14", "column": "minus_di14", "raw_column": "minus_di14", "label": "-DI 14", "color": COLOR_DOWN, "visible": False},
        {"key": "aroon_up", "column": "aroon_up", "raw_column": "aroon_up", "label": "Aroon Up", "color": "#22c55e", "visible": False},
        {"key": "aroon_down", "column": "aroon_down", "raw_column": "aroon_down", "label": "Aroon Down", "color": "#ef4444", "visible": False},
        {"key": "atr14_pct_plot", "column": "atr14_pct_plot", "raw_column": "atr14_pct", "label": "ATR% 14", "color": "#f97316", "visible": False},
        {"key": "bb_width_pct_plot", "column": "bb_width_pct_plot", "raw_column": "bb_width_pct", "label": "BB Width", "color": COLOR_BOLLINGER, "visible": False},
        {"key": "cmf20_plot", "column": "cmf20_plot", "raw_column": "cmf20", "label": "CMF 20", "color": COLOR_VOLUME_SIGNAL, "visible": False},
        {"key": "obv_plot", "column": "obv_plot", "raw_column": "obv", "label": "OBV", "color": "#94a3b8", "visible": False},
        {"key": "macd_plot", "column": "macd_plot", "raw_column": "macd", "label": "MACD", "color": COLOR_MACD, "visible": False},
        {"key": "signal_plot", "column": "signal_plot", "raw_column": "signal", "label": "MACD Signal", "color": COLOR_SIGNAL, "visible": False},
    ]


def _signal_histogram_specs() -> list[dict[str, Any]]:
    """Return signal-panel histogram indicator definitions.

    Returns:
        List of histogram metadata consumed by `_build_payload`.
    """
    return [
        {
            "key": "macd_histogram",
            "column": "histogram_plot",
            "raw_column": "histogram",
            "label": "MACD Hist",
            "color": "rgba(148,163,184,0.65)",
            "visible": False,
        }
    ]


def _chart_container_html(element_id: str, title: str, height: int) -> str:
    """Return the static container HTML that Lightweight Charts will populate.

    Args:
        element_id: Unique id assigned to the chart root and child panes.
        title: Human-readable chart title shown above the canvas.
        height: Minimum total block height in pixels.

    Returns:
        HTML string containing the title, status line, price pane, signal pane,
        custom legend area, and floating hover tooltip container.

    Notes:
        The price and signal pane heights are calculated here so the JavaScript
        renderer can rely on stable DOM dimensions before canvas creation.
    """
    escaped_title = html_lib.escape(title)
    price_height = max(int(height * 0.66), 360)
    signal_height = max(int(height * 0.28), 190)
    return f"""
<div id="{element_id}" class="bq-lwc-root" style="position:relative; width:100%; min-height:{height}px; background:{COLOR_BG}; border:1px solid rgba(148,163,184,0.18); border-radius:8px; overflow:hidden;">
  <style>
    #{element_id} .bq-lwc-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 12px 4px 12px; color:{COLOR_TEXT}; font-family:Inter, ui-sans-serif, system-ui, sans-serif; }}
    #{element_id} .bq-lwc-title {{ font-size:15px; font-weight:700; }}
    #{element_id} .bq-lwc-status {{ font-size:12px; color:{COLOR_MUTED}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    #{element_id} .bq-lwc-pane {{ width:100%; }}
    #{element_id} .bq-lwc-legend {{ display:flex; flex-wrap:wrap; gap:8px; padding:8px 12px 12px 12px; border-top:1px solid rgba(148,163,184,0.12); }}
    #{element_id} .bq-lwc-legend button {{ border:1px solid rgba(148,163,184,0.25); color:{COLOR_TEXT}; background:rgba(15,23,42,0.72); border-radius:6px; padding:4px 8px; font-size:12px; cursor:pointer; }}
    #{element_id} .bq-lwc-legend button[data-active="false"] {{ opacity:0.42; }}
    #{element_id} .bq-lwc-tooltip {{ position:absolute; z-index:20; display:none; min-width:220px; max-width:360px; pointer-events:none; border:1px solid rgba(148,163,184,0.28); border-radius:8px; background:rgba(15,23,42,0.94); box-shadow:0 12px 28px rgba(0,0,0,0.32); color:{COLOR_TEXT}; font-family:Inter, ui-sans-serif, system-ui, sans-serif; font-size:12px; line-height:1.35; padding:10px 12px; backdrop-filter:blur(8px); }}
    #{element_id} .bq-lwc-tooltip-title {{ font-size:12px; font-weight:700; margin-bottom:6px; color:#f8fafc; }}
    #{element_id} .bq-lwc-tooltip-grid {{ display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:4px 8px; margin-bottom:8px; }}
    #{element_id} .bq-lwc-tooltip-k {{ color:{COLOR_MUTED}; font-size:10px; text-transform:uppercase; }}
    #{element_id} .bq-lwc-tooltip-v {{ font-weight:700; color:{COLOR_TEXT}; }}
    #{element_id} .bq-lwc-tooltip-line {{ display:flex; align-items:center; justify-content:space-between; gap:12px; border-top:1px solid rgba(148,163,184,0.12); padding-top:4px; margin-top:4px; }}
    #{element_id} .bq-lwc-tooltip-dot {{ display:inline-block; width:8px; height:8px; border-radius:999px; margin-right:6px; flex:none; }}
    #{element_id} .bq-lwc-error {{ color:#fca5a5; padding:16px; font-size:13px; }}
  </style>
  <div class="bq-lwc-head">
    <div class="bq-lwc-title">{escaped_title}</div>
    <div id="{element_id}-status" class="bq-lwc-status">Move crosshair over candles for OHLCV.</div>
  </div>
  <div id="{element_id}-price" class="bq-lwc-pane bq-lwc-price-pane" style="height:{price_height}px;"></div>
  <div id="{element_id}-signal" class="bq-lwc-pane bq-lwc-signal-pane" style="height:{signal_height}px;"></div>
  <div id="{element_id}-legend" class="bq-lwc-legend"></div>
  <div id="{element_id}-tooltip" class="bq-lwc-tooltip"></div>
</div>
"""


def _chart_script(element_id: str, payload: dict[str, Any]) -> str:
    """Return JavaScript that renders a Lightweight Charts block.

    Args:
        element_id: DOM root id created by `_chart_container_html`.
        payload: JSON-serializable chart payload containing candle rows,
            rendered indicator series, raw hover-series values, price-line
            metadata, and display options.

    Returns:
        JavaScript snippet that lazy-loads TradingView Lightweight Charts,
        renders price/signal panes, wires legend toggles, syncs time ranges,
        and shows a floating hoverboard from crosshair events.

    Raises:
        TypeError: If `payload` contains values that cannot be serialized to
        strict JSON. This is intentional so pandas timestamps/NaN values are
        caught before the page renders a broken chart.
    """
    payload_json = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return f"""
(function() {{
  const payload = {payload_json};
  const scriptSrc = "{LIGHTWEIGHT_CHARTS_CDN}";

  function ensureLibrary() {{
    if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
    if (!window.__bquantLightweightChartsPromise) {{
      window.__bquantLightweightChartsPromise = new Promise((resolve, reject) => {{
        const script = document.createElement("script");
        script.src = scriptSrc;
        script.async = true;
        script.onload = () => resolve(window.LightweightCharts);
        script.onerror = () => reject(new Error("Unable to load TradingView Lightweight Charts"));
        document.head.appendChild(script);
      }});
    }}
    return window.__bquantLightweightChartsPromise;
  }}

  function addSeries(chart, seriesName, options) {{
    const lwc = window.LightweightCharts;
    if (chart.addSeries && lwc && lwc[seriesName]) return chart.addSeries(lwc[seriesName], options || {{}});
    const legacy = {{
      CandlestickSeries: "addCandlestickSeries",
      HistogramSeries: "addHistogramSeries",
      LineSeries: "addLineSeries"
    }}[seriesName];
    if (legacy && chart[legacy]) return chart[legacy](options || {{}});
    throw new Error("Unsupported Lightweight Charts series: " + seriesName);
  }}

  function baseOptions(height) {{
    return {{
      height: height,
      autoSize: true,
      layout: {{
        background: {{ type: "solid", color: "{COLOR_BG}" }},
        textColor: "{COLOR_TEXT}",
        fontSize: 12
      }},
      grid: {{
        vertLines: {{ color: "{COLOR_GRID}" }},
        horzLines: {{ color: "{COLOR_GRID}" }}
      }},
      rightPriceScale: {{ visible: false, borderVisible: false }},
      leftPriceScale: {{ visible: false, borderVisible: false }},
      crosshair: {{ mode: 1 }},
      timeScale: {{
        borderVisible: false,
        timeVisible: payload.intraday,
        secondsVisible: false
      }}
    }};
  }}

  function setData(series, rows) {{
    if (series && rows && rows.length) series.setData(rows);
  }}

  function render() {{
    const root = document.getElementById(payload.id);
    const priceEl = document.getElementById(payload.id + "-price");
    const signalEl = document.getElementById(payload.id + "-signal");
    const legendEl = document.getElementById(payload.id + "-legend");
    const statusEl = document.getElementById(payload.id + "-status");
    const tooltipEl = document.getElementById(payload.id + "-tooltip");
    if (!root || !priceEl || !signalEl || !legendEl) {{
      window.setTimeout(render, 50);
      return;
    }}

    if (root.__bquantCharts) {{
      root.__bquantCharts.forEach(chart => chart.remove && chart.remove());
    }}
    legendEl.innerHTML = "";

    const priceChart = window.LightweightCharts.createChart(priceEl, baseOptions(priceEl.clientHeight || 460));
    const signalChart = window.LightweightCharts.createChart(signalEl, baseOptions(signalEl.clientHeight || 170));
    root.__bquantCharts = [priceChart, signalChart];

    function makeTimeMap(rows) {{
      const output = new Map();
      (rows || []).forEach(row => output.set(String(row.time), row));
      return output;
    }}
    const candleByTime = makeTimeMap(payload.candles);
    const hoverSeries = payload.hoverSeries || payload.series || {{}};
    const hoverMaps = new Map(Object.entries(hoverSeries).map(([key, rows]) => [key, makeTimeMap(rows)]));

    const candle = addSeries(priceChart, "CandlestickSeries", {{
      upColor: "{COLOR_UP}",
      downColor: "{COLOR_DOWN}",
      borderUpColor: "{COLOR_UP}",
      borderDownColor: "{COLOR_DOWN}",
      wickUpColor: "{COLOR_UP}",
      wickDownColor: "{COLOR_DOWN}"
    }});
    setData(candle, payload.candles);
    candle.priceScale().applyOptions({{ scaleMargins: {{ top: 0.08, bottom: 0.28 }} }});

    const seriesEntries = [];
    function addLine(chart, key, label, color, visible, pane, options) {{
      const series = addSeries(chart, "LineSeries", Object.assign({{
        color: color,
        lineWidth: 1,
        visible: visible,
        priceLineVisible: false,
        lastValueVisible: false
      }}, options || {{}}));
      setData(series, payload.series[key] || []);
      seriesEntries.push({{ key, label, color, visible, series, pane }});
      return series;
    }}
    function addHistogram(chart, key, label, visible, pane, options) {{
      const series = addSeries(chart, "HistogramSeries", Object.assign({{
        visible: visible,
        priceLineVisible: false,
        lastValueVisible: false
      }}, options || {{}}));
      setData(series, payload.series[key] || []);
      seriesEntries.push({{ key, label, color: options && options.color ? options.color : "{COLOR_MUTED}", visible, series, pane }});
      return series;
    }}

    function escapeHtml(value) {{
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }}

    function formatNumber(value, decimals) {{
      const number = Number(value);
      if (!Number.isFinite(number)) return "N/A";
      return number.toLocaleString(undefined, {{
        maximumFractionDigits: decimals,
        minimumFractionDigits: decimals
      }});
    }}

    function formatPrice(value) {{
      const number = Number(value);
      if (!Number.isFinite(number)) return "N/A";
      return formatNumber(number, Math.abs(number) >= 1000 ? 2 : 3);
    }}

    function formatIndicator(value) {{
      const number = Number(value);
      if (!Number.isFinite(number)) return "N/A";
      if (Math.abs(number) >= 1000000) return number.toLocaleString(undefined, {{ maximumFractionDigits: 0 }});
      if (Math.abs(number) >= 1000) return number.toLocaleString(undefined, {{ maximumFractionDigits: 1 }});
      return number.toLocaleString(undefined, {{ maximumFractionDigits: 3 }});
    }}

    function formatTime(time) {{
      if (payload.intraday) {{
        return new Date(Number(time) * 1000).toLocaleString("vi-VN", {{
          timeZone: "Asia/Ho_Chi_Minh",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit"
        }});
      }}
      return String(time);
    }}

    function tooltipLine(entry, value) {{
      return '<div class="bq-lwc-tooltip-line"><span><span class="bq-lwc-tooltip-dot" style="background:' +
        entry.color + '"></span>' + escapeHtml(entry.label) + '</span><strong>' +
        escapeHtml(formatIndicator(value)) + '</strong></div>';
    }}

    function moveTooltip(param, paneEl) {{
      if (!tooltipEl || !param || param.time == null || !param.point) {{
        if (tooltipEl) tooltipEl.style.display = "none";
        return;
      }}
      const timeKey = String(param.time);
      const candleData = candleByTime.get(timeKey);
      if (!candleData) {{
        tooltipEl.style.display = "none";
        return;
      }}
      const volumeData = hoverMaps.get("volume") ? hoverMaps.get("volume").get(timeKey) : null;
      const volumeText = volumeData && volumeData.value != null ? Number(volumeData.value).toLocaleString() : "N/A";
      const activeRows = seriesEntries
        .filter(entry => entry.visible && entry.key !== "volume")
        .map(entry => {{
          const source = hoverMaps.get(entry.key);
          const point = source ? source.get(timeKey) : null;
          return point && point.value != null ? tooltipLine(entry, point.value) : "";
        }})
        .filter(Boolean)
        .slice(0, 14)
        .join("");

      tooltipEl.innerHTML =
        '<div class="bq-lwc-tooltip-title">' + escapeHtml(payload.title || "Chart") + ' · ' + escapeHtml(formatTime(param.time)) + '</div>' +
        '<div class="bq-lwc-tooltip-grid">' +
        '<div><div class="bq-lwc-tooltip-k">Open</div><div class="bq-lwc-tooltip-v">' + escapeHtml(formatPrice(candleData.open)) + '</div></div>' +
        '<div><div class="bq-lwc-tooltip-k">High</div><div class="bq-lwc-tooltip-v">' + escapeHtml(formatPrice(candleData.high)) + '</div></div>' +
        '<div><div class="bq-lwc-tooltip-k">Low</div><div class="bq-lwc-tooltip-v">' + escapeHtml(formatPrice(candleData.low)) + '</div></div>' +
        '<div><div class="bq-lwc-tooltip-k">Close</div><div class="bq-lwc-tooltip-v">' + escapeHtml(formatPrice(candleData.close)) + '</div></div>' +
        '</div>' +
        '<div class="bq-lwc-tooltip-line"><span>Volume</span><strong>' + escapeHtml(volumeText) + '</strong></div>' +
        activeRows;
      tooltipEl.style.display = "block";

      const rootRect = root.getBoundingClientRect();
      const paneRect = paneEl.getBoundingClientRect();
      const preferredLeft = paneRect.left - rootRect.left + param.point.x + 14;
      const preferredTop = paneRect.top - rootRect.top + param.point.y + 14;
      const tooltipRect = tooltipEl.getBoundingClientRect();
      const maxLeft = Math.max(root.clientWidth - tooltipRect.width - 10, 10);
      const maxTop = Math.max(root.clientHeight - tooltipRect.height - 10, 10);
      tooltipEl.style.left = Math.min(Math.max(preferredLeft, 10), maxLeft) + "px";
      tooltipEl.style.top = Math.min(Math.max(preferredTop, 10), maxTop) + "px";
    }}

    (payload.priceLines || []).forEach(item => addLine(priceChart, item.key, item.label, item.color, item.visible, "price"));

    const volume = addHistogram(priceChart, "volume", "Volume", true, "price", {{
      priceScaleId: "",
      color: "rgba(148,163,184,0.25)",
      priceFormat: {{ type: "volume" }}
    }});
    volume.priceScale().applyOptions({{ scaleMargins: {{ top: 0.78, bottom: 0 }} }});
    addLine(priceChart, "volume_ma20", "Volume EMA 20", "rgba(245,158,11,0.78)", false, "price", {{
      priceScaleId: ""
    }});

    (payload.signalLines || []).forEach(item => addLine(signalChart, item.key, item.label, item.color, item.visible, "signal"));
    (payload.signalHistograms || []).forEach(item => addHistogram(signalChart, item.key, item.label, item.visible, "signal", {{
      color: item.color || "rgba(148,163,184,0.65)"
    }}));

    function addLegend(entry) {{
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.active = String(entry.visible);
      button.innerHTML = '<span style="display:inline-block;width:9px;height:9px;border-radius:99px;background:' + entry.color + ';margin-right:6px;"></span>' + entry.label;
      button.onclick = () => {{
        entry.visible = !entry.visible;
        entry.series.applyOptions({{ visible: entry.visible }});
        button.dataset.active = String(entry.visible);
      }};
      legendEl.appendChild(button);
    }}
    seriesEntries.forEach(addLegend);

    function sync(source, target) {{
      source.timeScale().subscribeVisibleLogicalRangeChange(range => {{
        if (range && !source.__syncing) {{
          target.__syncing = true;
          target.timeScale().setVisibleLogicalRange(range);
          target.__syncing = false;
        }}
      }});
    }}
    sync(priceChart, signalChart);
    sync(signalChart, priceChart);

    priceChart.timeScale().fitContent();
    signalChart.timeScale().fitContent();
    if (payload.defaultVisibleBars && payload.candles.length > payload.defaultVisibleBars) {{
      const from = Math.max(payload.candles.length - payload.defaultVisibleBars, 0);
      const to = payload.candles.length + 4;
      priceChart.timeScale().setVisibleLogicalRange({{ from, to }});
      signalChart.timeScale().setVisibleLogicalRange({{ from, to }});
    }}

    priceChart.subscribeCrosshairMove(param => {{
      const data = param && param.seriesData ? param.seriesData.get(candle) : null;
      if (data && statusEl) {{
        const volumePoint = param.seriesData.get(volume);
        const volumeText = volumePoint && volumePoint.value != null ? " V " + Number(volumePoint.value).toLocaleString() : "";
        statusEl.textContent = "O " + data.open + " H " + data.high + " L " + data.low + " C " + data.close + volumeText;
      }}
      moveTooltip(param, priceEl);
    }});
    signalChart.subscribeCrosshairMove(param => moveTooltip(param, signalEl));
    priceEl.addEventListener("mouseleave", () => {{
      if (tooltipEl) tooltipEl.style.display = "none";
    }});
    signalEl.addEventListener("mouseleave", () => {{
      if (tooltipEl) tooltipEl.style.display = "none";
    }});

    const observer = new ResizeObserver(() => {{
      priceChart.applyOptions({{ width: priceEl.clientWidth, height: priceEl.clientHeight }});
      signalChart.applyOptions({{ width: signalEl.clientWidth, height: signalEl.clientHeight }});
    }});
    observer.observe(root);
    root.__bquantResizeObserver = observer;
  }}

  ensureLibrary().then(render).catch(error => {{
    const root = document.getElementById("{element_id}");
    if (root) root.innerHTML = '<div class="bq-lwc-error">' + error.message + '</div>';
  }});
}})();
"""


def _build_lightweight_spec(payload: dict[str, Any], *, title: str, height: int) -> LightweightChartSpec:
    """Create a full Lightweight Charts render spec.

    Args:
        payload: JSON-serializable chart payload without the DOM id.
        title: Human-readable title displayed in the chart header.
        height: Minimum chart block height in pixels.

    Returns:
        LightweightChartSpec containing a unique element id, HTML container,
        and JavaScript render script.
    """
    element_id = f"bq-lwc-{uuid.uuid4().hex}"
    payload = dict(payload)
    payload["id"] = element_id
    return LightweightChartSpec(
        element_id=element_id,
        html=_chart_container_html(element_id, title, height),
        script=_chart_script(element_id, payload),
    )


def _build_payload(
    frame: pd.DataFrame,
    *,
    time_col: str,
    intraday: bool,
    title: str,
    price_lines: list[dict[str, Any]],
    height: int,
    default_visible_bars: int | None,
) -> LightweightChartSpec:
    """Build the common Lightweight Charts payload for a prepared OHLCV frame.

    Args:
        frame: Prepared chart DataFrame with OHLCV, price overlays, signal
            indicator columns, and raw values for hover display.
        time_col: Date/timestamp column used for the x-axis.
        intraday: Whether `time_col` should be encoded as Unix seconds.
        title: Chart title.
        price_lines: Price-overlay definitions. Each item maps a payload key
            to a DataFrame column.
        height: Minimum chart block height.
        default_visible_bars: Optional initial visible bar count. The full
            dataset remains loaded even when only the latest bars are shown.

    Returns:
        LightweightChartSpec containing HTML and JavaScript render assets.

    Notes:
        `series` is the drawing payload, while `hoverSeries` stores raw values
        for tooltip display. This lets normalized signal-panel lines keep
        scale-safe rendering without hiding the true indicator value on hover.
    """
    colors = _volume_colors(frame)
    signal_lines = [item for item in _signal_line_specs() if item["column"] in frame.columns]
    signal_histograms = [item for item in _signal_histogram_specs() if item["column"] in frame.columns]
    payload = {
        "title": title,
        "intraday": intraday,
        "defaultVisibleBars": default_visible_bars,
        "candles": _candlestick_data(frame, time_col, intraday=intraday),
        "priceLines": price_lines,
        "signalLines": [{key: item[key] for key in ["key", "label", "color", "visible"]} for item in signal_lines],
        "signalHistograms": [{key: item[key] for key in ["key", "label", "color", "visible"]} for item in signal_histograms],
        "series": {
            "volume": _histogram_data(frame, time_col, "volume", intraday=intraday, colors=colors),
            "volume_ma20": _line_data(frame, time_col, "volume_ma20", intraday=intraday),
        },
        "hoverSeries": {
            "volume": _histogram_data(frame, time_col, "volume", intraday=intraday),
            "volume_ma20": _line_data(frame, time_col, "volume_ma20", intraday=intraday),
        },
    }
    for item in price_lines:
        if item["column"] in frame.columns:
            payload["series"][item["key"]] = _line_data(frame, time_col, item["column"], intraday=intraday)
            payload["hoverSeries"][item["key"]] = _line_data(frame, time_col, item["column"], intraday=intraday)
    for item in signal_lines:
        payload["series"][item["key"]] = _line_data(frame, time_col, item["column"], intraday=intraday)
        raw_column = item.get("raw_column", item["column"])
        if raw_column in frame.columns:
            payload["hoverSeries"][item["key"]] = _line_data(frame, time_col, raw_column, intraday=intraday)
    for item in signal_histograms:
        payload["series"][item["key"]] = _histogram_data(
            frame,
            time_col,
            item["column"],
            intraday=intraday,
            colors=_macd_histogram_colors(frame) if item["key"] == "macd_histogram" else None,
        )
        raw_column = item.get("raw_column", item["column"])
        if raw_column in frame.columns:
            payload["hoverSeries"][item["key"]] = _histogram_data(frame, time_col, raw_column, intraday=intraday)
    return _build_lightweight_spec(payload, title=title, height=height)


def build_market_overview_chart(
    period_key: str = "1Y",
    candle_source: str = "VN30",
    *,
    show_comparison: bool = True,
    height: int = 720,
) -> tuple[LightweightChartSpec, list[dict[str, str]], str]:
    """Build a market overview chart for VN30 or VNIndex.

    Args:
        period_key: Dashboard range preset. `10Y` and `ALL` load the full
            available history but default the viewport to the latest 60 bars.
        candle_source: Market index to render as candles. Supported values are
            `VN30` and `VNIndex`.
        show_comparison: Whether to overlay the other market index as a price
            comparison line.
        height: Minimum chart block height in pixels.

    Returns:
        Tuple containing the Lightweight Charts render spec, metric-card rows,
        and a source/indicator note for the UI.

    Raises:
        ValueError: If aligned market overview data is unavailable.

    Notes:
        Candles use raw `vnstock:VCI` index OHLCV from `market_index_daily_base`.
        Breadth and constituent volume still come from `daily_ohlcv_base`.
    """
    frame = load_market_overview_data(period_key)
    if frame.empty:
        raise ValueError("No market overview data available")

    selected_source = candle_source.upper()
    if selected_source == "VNINDEX":
        selected_label = VNINDEX_LABEL
        selected_prefix = "vnindex"
        comparison_label = VN30_LABEL
        rs_label = f"RS vs {comparison_label} (20d)"
    else:
        selected_label = VN30_LABEL
        selected_prefix = "vn30"
        comparison_label = VNINDEX_LABEL
        rs_label = f"RS vs {comparison_label} (20d)"

    chart_frame = frame.copy()
    for column in PRICE_COLUMNS:
        chart_frame[column] = pd.to_numeric(chart_frame[f"{selected_prefix}_{column}"], errors="coerce")
    chart_frame["volume"] = pd.to_numeric(chart_frame[f"{selected_prefix}_volume"], errors="coerce")
    chart_frame = _add_indicator_suite(chart_frame, include_intraday_vwap=False, include_long_ma=True)

    if show_comparison:
        comparison_prefix = "vn30" if selected_prefix == "vnindex" else "vnindex"
        chart_frame["comparison_close"] = pd.to_numeric(chart_frame[f"{comparison_prefix}_close"], errors="coerce")
    else:
        chart_frame["comparison_close"] = pd.NA

    price_lines = _advanced_price_lines(include_vwap=False, include_long_ma=True)
    if show_comparison:
        price_lines.append(
            {
                "key": "comparison_close",
                "column": "comparison_close",
                "label": comparison_label,
                "color": COLOR_BENCH,
                "visible": True,
            }
        )

    chart = _build_payload(
        chart_frame,
        time_col="trading_date",
        intraday=False,
        title=f"{selected_label} Candlestick",
        price_lines=price_lines,
        height=height,
        default_visible_bars=DEFAULT_DAILY_VIEW_BARS if period_key in {"10Y", "ALL"} else None,
    )

    latest = chart_frame.iloc[-1]
    prev_20 = chart_frame.iloc[-21] if len(chart_frame) > 20 else chart_frame.iloc[0]
    relative_strength_20d = None
    if show_comparison and pd.notna(latest["comparison_close"]) and pd.notna(prev_20["comparison_close"]):
        selected_return = (latest["close"] / prev_20["close"] - 1.0) * 100.0
        comparison_return = (latest["comparison_close"] / prev_20["comparison_close"] - 1.0) * 100.0
        relative_strength_20d = selected_return - comparison_return

    metrics = [
        {
            "label": "Trend",
            "value": _trend_label(_latest_value(chart_frame["close"]), _latest_value(chart_frame["ema20"]), _latest_value(chart_frame["ema50"])),
        },
        {
            "label": "Breadth",
            "value": f"{int(latest['advancers'])}/{int(latest['decliners'])} | {_fmt_float(float(latest['breadth_pct']), suffix='%', decimals=1)} advancing",
        },
        {
            "label": "RSI 14",
            "value": f"{_fmt_float(_latest_value(chart_frame['rsi14']))} ({_rsi_label(_latest_value(chart_frame['rsi14']))})",
        },
        {"label": "MACD", "value": _macd_label(_latest_value(chart_frame["macd"]), _latest_value(chart_frame["signal"]))},
        {"label": "ADX 14", "value": _fmt_float(_latest_value(chart_frame["adx14"]))},
        {"label": "ATR% 14", "value": _fmt_float(_latest_value(chart_frame["atr14_pct"]), suffix="%")},
        {"label": "MFI 14", "value": _fmt_float(_latest_value(chart_frame["mfi14"]))},
        {"label": rs_label, "value": _fmt_pct(relative_strength_20d)},
    ]
    note = (
        f"{selected_label} uses raw vnstock:VCI OHLCV from market_index_daily_base. "
        "Advanced overlays include EMA/SMA, Bollinger, Donchian, Keltner, and Supertrend; "
        "the lower panel includes normalized momentum, volatility, trend-strength, and volume-flow indicators hidden by default."
    )
    return chart, metrics, note


def build_symbol_chart(symbol: str, mode: str = "daily", period_key: str = "1Y") -> tuple[LightweightChartSpec, list[dict[str, str]], str]:
    """Build the Symbol Explorer chart for daily or 15-minute price action.

    Args:
        symbol: VN30 constituent ticker.
        mode: Dataset mode. `daily` reads the 10-year base table; `intraday`
            reads the 15-minute base+delta plotting view.
        period_key: UI range preset used to trim the loaded series.

    Returns:
        Tuple containing the Lightweight Charts render spec, metric-card rows,
        and a source/indicator note.

    Raises:
        ValueError: If the requested symbol/mode has no data available.
    """
    if mode == "intraday":
        frame = _prepare_symbol_intraday(symbol, period_key)
        if frame.empty:
            raise ValueError(f"No intraday data available for {symbol}")
        x_col = "bar_time"
        price_lines = _advanced_price_lines(include_vwap=True, include_long_ma=False)
        chart = _build_payload(
            frame,
            time_col=x_col,
            intraday=True,
            title=f"{symbol} 15m Candlestick",
            price_lines=price_lines,
            height=780,
            default_visible_bars=None,
        )
        metrics = [
            {"label": "Last Price", "value": _fmt_float(_latest_value(frame["close"]))},
            {"label": "Trend", "value": _trend_label(_latest_value(frame["close"]), _latest_value(frame["ema20"]), _latest_value(frame["ema50"]))},
            {"label": "RSI 14", "value": f"{_fmt_float(_latest_value(frame['rsi14']))} ({_rsi_label(_latest_value(frame['rsi14']))})"},
            {"label": "MACD", "value": _macd_label(_latest_value(frame["macd"]), _latest_value(frame["signal"]))},
            {"label": "ADX 14", "value": _fmt_float(_latest_value(frame["adx14"]))},
            {"label": "ATR% 14", "value": _fmt_float(_latest_value(frame["atr14_pct"]), suffix="%")},
            {"label": "MFI 14", "value": _fmt_float(_latest_value(frame["mfi14"]))},
        ]
        note = (
            "Intraday chart uses local vnstock:VCI 15m bars with session VWAP, trend overlays, volatility bands, "
            "and a normalized lower panel for momentum, volatility, trend-strength, and volume-flow indicators."
        )
        return chart, metrics, note

    frame = _prepare_symbol_daily(symbol, period_key)
    if frame.empty:
        raise ValueError(f"No daily data available for {symbol}")
    price_lines = _advanced_price_lines(include_vwap=False, include_long_ma=True)
    chart = _build_payload(
        frame,
        time_col="trading_date",
        intraday=False,
        title=f"{symbol} Daily Candlestick",
        price_lines=price_lines,
        height=780,
        default_visible_bars=DEFAULT_DAILY_VIEW_BARS if period_key in {"10Y", "ALL"} else None,
    )

    previous_close = frame["close"].iloc[-2] if len(frame) > 1 else None
    last_close = _latest_value(frame["close"])
    day_change = None
    if previous_close not in (None, 0) and last_close is not None:
        day_change = (last_close / previous_close - 1.0) * 100.0
    metrics = [
        {"label": "Last Close", "value": _fmt_float(last_close)},
        {"label": "1D Move", "value": _fmt_pct(day_change)},
        {"label": "Trend", "value": _trend_label(last_close, _latest_value(frame["ema50"]), _latest_value(frame["ema200"]))},
        {"label": "RS vs VN30 (20d)", "value": _fmt_pct(_symbol_relative_strength(symbol, 20))},
        {"label": "RSI 14", "value": f"{_fmt_float(_latest_value(frame['rsi14']))} ({_rsi_label(_latest_value(frame['rsi14']))})"},
        {"label": "MACD", "value": _macd_label(_latest_value(frame["macd"]), _latest_value(frame["signal"]))},
        {"label": "ADX 14", "value": _fmt_float(_latest_value(frame["adx14"]))},
        {"label": "ATR% 14", "value": _fmt_float(_latest_value(frame["atr14_pct"]), suffix="%")},
        {"label": "MFI 14", "value": _fmt_float(_latest_value(frame["mfi14"]))},
    ]
    note = (
        "Daily chart uses local vnstock:VCI 10-year OHLCV with an expanded technical suite. "
        "The full dataset is loaded, while 10Y/ALL defaults to the latest 60 bars for the first view; advanced indicators are toggled from the legend."
    )
    return chart, metrics, note
