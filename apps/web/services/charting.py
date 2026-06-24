"""Chart data loaders and Plotly figure builders for the BQuant web app."""

from __future__ import annotations

from functools import lru_cache
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from plotly.subplots import make_subplots

from utils.logger import BQuantLogger
from warehouse.duckdb_connection import get_connection
from warehouse.refresh_state import get_refresh_version


OVERVIEW_PERIOD_OPTIONS = ["6M", "1Y", "3Y", "5Y", "10Y", "ALL"]
MARKET_CANDLE_OPTIONS = ["VN30", "VNIndex"]
SYMBOL_DAILY_RANGE_OPTIONS = ["3M", "6M", "1Y", "3Y", "5Y", "10Y", "ALL"]
SYMBOL_INTRADAY_RANGE_OPTIONS = ["5D", "20D", "60D"]

VNINDEX_TICKER = "0P0000HY8X.VN"
VNINDEX_LABEL = "VNIndex"
VN30_BENCHMARK_CANDIDATES = [
    ("E1VFVN30.VN", "VN30 ETF Proxy"),
    ("FUEMAV30.VN", "VN30 ETF Proxy"),
]
VN30_BASKET_LABEL = "VN30 Breadth Basket"

COLOR_BG = "#0f172a"
COLOR_GRID = "rgba(148, 163, 184, 0.18)"
COLOR_TEXT = "#e2e8f0"
COLOR_ACCENT = "#22d3ee"
COLOR_BENCH = "rgba(226, 232, 240, 0.40)"
COLOR_EMA_FAST = "#f59e0b"
COLOR_EMA_SLOW = "#a78bfa"
COLOR_EMA_LONG = "#38bdf8"
COLOR_UP = "#22c55e"
COLOR_DOWN = "#ef4444"
COLOR_RSI = "#c084fc"
COLOR_MACD = "#60a5fa"
COLOR_SIGNAL = "#f97316"
COLOR_VWAP = "#eab308"
LINE_WIDTH_THIN = 1.15
LINE_WIDTH_MAIN = 1.45
AUX_TRACE_VISIBILITY = "legendonly"
DEFAULT_DAILY_VIEW_BARS = 60
PRICE_COLUMNS = ("open", "high", "low", "close")

LOGGER = BQuantLogger("web_charting", component="web", subcomponent="charting", default_channel="web")


def _flatten_yf_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse Yahoo Finance multi-index columns into a flat OHLCV layout."""
    if isinstance(df.columns, pd.MultiIndex):
        frame = df.copy()
        frame.columns = [column[0] if isinstance(column, tuple) else column for column in frame.columns]
        return frame
    return df


def _period_to_offset(period_key: str) -> pd.DateOffset | None:
    """Map a UI period preset to a pandas date offset."""
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


def _period_to_yahoo(period_key: str) -> str:
    """Translate a UI period preset into a Yahoo Finance history window."""
    mapping = {
        "3M": "3mo",
        "6M": "6mo",
        "1Y": "1y",
        "3Y": "3y",
        "5Y": "5y",
        "10Y": "10y",
        "ALL": "max",
    }
    return mapping.get(period_key, "1y")


def _filter_by_period(frame: pd.DataFrame, date_col: str, period_key: str) -> pd.DataFrame:
    """Trim a time series frame to the requested trailing period."""
    if frame.empty:
        return frame.copy()
    offset = _period_to_offset(period_key)
    if offset is None:
        return frame.copy()
    end_ts = pd.to_datetime(frame[date_col]).max()
    start_ts = end_ts - offset
    return frame.loc[pd.to_datetime(frame[date_col]) >= start_ts].copy()


def _build_trading_rangebreaks(dates: pd.Series) -> list[dict[str, Any]]:
    """Build Plotly rangebreaks that remove weekends and local exchange holidays."""
    normalized = pd.to_datetime(dates, errors="coerce").dropna().dt.normalize().drop_duplicates().sort_values()
    if normalized.empty:
        return [dict(bounds=["sat", "mon"])]
    trading_days = set(normalized.tolist())
    calendar_days = pd.date_range(normalized.iloc[0], normalized.iloc[-1], freq="D")
    exchange_holidays = [
        day.strftime("%Y-%m-%d")
        for day in calendar_days
        if day.weekday() < 5 and day not in trading_days
    ]
    rangebreaks: list[dict[str, Any]] = [dict(bounds=["sat", "mon"])]
    if exchange_holidays:
        rangebreaks.append(dict(values=exchange_holidays))
    return rangebreaks


def _default_recent_view_range(dates: pd.Series, lookback_bars: int) -> list[str] | None:
    """Return the default x-axis window anchored on the latest N bars."""
    ordered = pd.to_datetime(dates, errors="coerce").dropna().drop_duplicates().sort_values()
    if ordered.empty:
        return None
    start_index = max(len(ordered) - lookback_bars, 0)
    start_value = ordered.iloc[start_index]
    end_value = ordered.iloc[-1]
    return [
        start_value.to_pydatetime().isoformat(),
        end_value.to_pydatetime().isoformat(),
    ]


def _default_recent_view_frame(frame: pd.DataFrame, lookback_bars: int) -> pd.DataFrame:
    """Return the trailing slice used for initial autoscaling decisions."""
    if frame.empty:
        return frame.copy()
    return frame.tail(lookback_bars).copy()


def _compute_price_axis_range(
    frame: pd.DataFrame,
    *,
    low_col: str,
    high_col: str,
    anchor_col: str,
    extra_series: list[pd.Series] | None = None,
    anchor_padding_ratio: float = 0.15,
) -> list[float] | None:
    """Compute a positive-only price axis range with at least +/- padding around the latest anchor."""
    if frame.empty:
        return None
    low_values = pd.to_numeric(frame[low_col], errors="coerce").dropna()
    high_values = pd.to_numeric(frame[high_col], errors="coerce").dropna()
    anchor_values = pd.to_numeric(frame[anchor_col], errors="coerce").dropna()
    low_values = low_values[low_values > 0]
    high_values = high_values[high_values > 0]
    anchor_values = anchor_values[anchor_values > 0]
    if low_values.empty or high_values.empty or anchor_values.empty:
        return None

    data_low = float(low_values.min())
    data_high = float(high_values.max())
    anchor_value = float(anchor_values.iloc[-1])
    padded_low = anchor_value * (1.0 - anchor_padding_ratio)
    padded_high = anchor_value * (1.0 + anchor_padding_ratio)

    if extra_series:
        for series in extra_series:
            extra_values = pd.to_numeric(series, errors="coerce").dropna()
            extra_values = extra_values[extra_values > 0]
            if extra_values.empty:
                continue
            data_low = min(data_low, float(extra_values.min()))
            data_high = max(data_high, float(extra_values.max()))

    range_low = min(data_low, padded_low)
    range_high = max(data_high, padded_high)
    if range_low == range_high:
        delta = max(abs(anchor_value) * 0.05, 1.0)
        return [anchor_value - delta, anchor_value + delta]
    return [range_low, range_high]


def _normalize_base_100(series: pd.Series) -> pd.Series:
    """Rebase a price series so the first valid value starts at 100."""
    clean = pd.to_numeric(series, errors="coerce")
    first_valid = clean.dropna().iloc[0] if not clean.dropna().empty else None
    if first_valid in (None, 0):
        return clean
    return clean / first_valid * 100.0


def _normalize_ohlc_frame(frame: pd.DataFrame, *, prefix: str) -> pd.DataFrame:
    """Add rebased OHLC columns for overlay-friendly candlestick comparisons."""
    normalized = frame.copy()
    close = pd.to_numeric(normalized["close"], errors="coerce")
    first_valid = close.dropna().iloc[0] if not close.dropna().empty else None
    scale = None if first_valid in (None, 0) else 100.0 / float(first_valid)
    for column in PRICE_COLUMNS:
        values = pd.to_numeric(normalized[column], errors="coerce")
        normalized[f"{prefix}_{column}"] = values * scale if scale is not None else pd.NA
    return normalized


def _ema(series: pd.Series, span: int) -> pd.Series:
    """Calculate an exponential moving average over a price-like series."""
    return pd.to_numeric(series, errors="coerce").ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI using exponentially weighted gains and losses."""
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
    """Calculate MACD line, signal line, and histogram for a price-like series."""
    close = pd.to_numeric(series, errors="coerce")
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": histogram,
        }
    )


def _session_vwap(frame: pd.DataFrame) -> pd.Series:
    """Calculate per-session VWAP for intraday bars."""
    working = frame.copy()
    typical_price = (working["high"] + working["low"] + working["close"]) / 3.0
    px_vol = typical_price * working["volume"]
    cumulative_px_vol = px_vol.groupby(working["session_date"]).cumsum()
    cumulative_vol = working["volume"].groupby(working["session_date"]).cumsum().replace(0, pd.NA)
    return cumulative_px_vol / cumulative_vol


def _latest_value(series: pd.Series) -> float | None:
    """Return the latest non-null numeric value from a series."""
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    return float(clean.iloc[-1])


def _fmt_float(value: float | None, suffix: str = "", decimals: int = 2) -> str:
    """Format a numeric metric for compact UI display."""
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value:.{decimals}f}{suffix}"


def _fmt_pct(value: float | None) -> str:
    """Format a percentage metric with an explicit sign."""
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value:+.2f}%"


def _trend_label(price: float | None, fast: float | None, slow: float | None) -> str:
    """Classify price trend from price, fast EMA, and slow EMA ordering."""
    if price is None or fast is None or slow is None:
        return "N/A"
    if price > fast > slow:
        return "Bullish"
    if price < fast < slow:
        return "Bearish"
    return "Neutral"


def _rsi_label(value: float | None) -> str:
    """Convert an RSI value into a coarse sentiment label."""
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
    """Convert MACD and signal values into a directional label."""
    if macd is None or signal is None:
        return "N/A"
    return "Bullish Cross" if macd >= signal else "Bearish Cross"


def _bottom_legend_config() -> dict[str, Any]:
    """Keep legend controls below the chart so they do not cover price action."""
    return {
        "orientation": "h",
        "yanchor": "top",
        "y": -0.18,
        "xanchor": "left",
        "x": 0,
        "font": {"size": 11},
        "tracegroupgap": 10,
    }


def _add_horizontal_reference(
    fig: go.Figure,
    *,
    x_values: pd.Series,
    y_value: float,
    color: str,
    row: int,
    secondary_y: bool = False,
) -> None:
    """Add a static horizontal guide line that stays JSON-serializable for NiceGUI."""
    if x_values.empty:
        return
    start_value = x_values.iloc[0]
    end_value = x_values.iloc[-1]
    if isinstance(start_value, pd.Timestamp):
        start_value = start_value.to_pydatetime().isoformat()
    if isinstance(end_value, pd.Timestamp):
        end_value = end_value.to_pydatetime().isoformat()
    fig.add_trace(
        go.Scatter(
            x=[start_value, end_value],
            y=[y_value, y_value],
            mode="lines",
            line=dict(color=color, width=1, dash="dot"),
            showlegend=False,
            hoverinfo="skip",
        ),
        row=row,
        col=1,
        secondary_y=secondary_y,
    )


def _add_signal_panel(fig: go.Figure, *, frame: pd.DataFrame, x_col: str, row: int) -> None:
    """Render RSI and MACD overlays into the shared lower signal panel."""
    hist_colors = [COLOR_UP if value >= 0 else COLOR_DOWN for value in frame["histogram"].fillna(0)]

    fig.add_trace(
        go.Scatter(
            x=frame[x_col],
            y=frame["rsi14"],
            mode="lines",
            name="RSI 14",
            line=dict(color=COLOR_RSI, width=LINE_WIDTH_THIN),
            visible=AUX_TRACE_VISIBILITY,
        ),
        row=row,
        col=1,
    )
    for level, color in [(70, COLOR_DOWN), (50, "rgba(226,232,240,0.35)"), (30, COLOR_UP)]:
        _add_horizontal_reference(fig, x_values=frame[x_col], y_value=level, color=color, row=row, secondary_y=False)

    fig.add_trace(
        go.Bar(
            x=frame[x_col],
            y=frame["histogram"],
            name="MACD Hist",
            marker_color=hist_colors,
            opacity=0.72,
            visible=AUX_TRACE_VISIBILITY,
        ),
        row=row,
        col=1,
        secondary_y=True,
    )
    fig.add_trace(
        go.Scatter(
            x=frame[x_col],
            y=frame["macd"],
            mode="lines",
            name="MACD",
            line=dict(color=COLOR_MACD, width=LINE_WIDTH_THIN),
            visible=AUX_TRACE_VISIBILITY,
        ),
        row=row,
        col=1,
        secondary_y=True,
    )
    fig.add_trace(
        go.Scatter(
            x=frame[x_col],
            y=frame["signal"],
            mode="lines",
            name="Signal",
            line=dict(color=COLOR_SIGNAL, width=LINE_WIDTH_THIN),
            visible=AUX_TRACE_VISIBILITY,
        ),
        row=row,
        col=1,
        secondary_y=True,
    )
    _add_horizontal_reference(
        fig,
        x_values=frame[x_col],
        y_value=0,
        color="rgba(226,232,240,0.35)",
        row=row,
        secondary_y=True,
    )


@lru_cache(maxsize=8)
def _load_local_daily_cached(refresh_version: int) -> pd.DataFrame:
    """Load the full local daily dataset and tag stale-cache conditions in logs."""
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
    """Load daily OHLCV using the current refresh version as the cache key."""
    refresh_version = get_refresh_version("daily_ohlcv_10y")
    return _load_local_daily_cached(refresh_version).copy()


@lru_cache(maxsize=128)
def _load_local_intraday_cached(symbol: str, base_refresh_version: int, delta_refresh_version: int) -> pd.DataFrame:
    """Load the merged intraday plot dataset for one symbol with refresh-aware caching."""
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


@lru_cache(maxsize=16)
def _load_yahoo_history(ticker: str, period: str) -> pd.DataFrame:
    """Load 1D Yahoo Finance history and normalize it to the local OHLCV schema."""
    frame = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    if frame.empty:
        return pd.DataFrame(columns=["trading_date", "open", "high", "low", "close", "volume"])
    frame = _flatten_yf_columns(frame).reset_index()
    date_column = "Date" if "Date" in frame.columns else frame.columns[0]
    frame["trading_date"] = pd.to_datetime(frame[date_column]).dt.tz_localize(None)
    renamed = frame.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    columns = ["trading_date", "open", "high", "low", "close", "volume"]
    return renamed[columns].copy()


def _load_local_intraday(symbol: str) -> pd.DataFrame:
    """Load intraday bars for one symbol from the current base+delta plot view."""
    base_refresh_version = get_refresh_version("intraday_ohlcv_15m_60d")
    delta_refresh_version = get_refresh_version("intraday_ohlcv_15m_delta")
    return _load_local_intraday_cached(symbol, base_refresh_version, delta_refresh_version).copy()


def _load_yahoo_first_available(
    candidates: list[tuple[str, str]],
    period: str,
) -> tuple[pd.DataFrame, str, str] | None:
    """Return the first Yahoo ticker candidate that yields a non-empty daily history."""
    for ticker, label in candidates:
        frame = _load_yahoo_history(ticker, period)
        if not frame.empty:
            return frame, label, ticker
    return None


def load_market_overview_data(period_key: str = "1Y") -> pd.DataFrame:
    """Load aligned market-level benchmark, breadth, and volume series for the home dashboard."""
    vn30 = _load_local_daily()
    if vn30.empty:
        return pd.DataFrame()
    vn30 = _filter_by_period(vn30, "trading_date", period_key)
    if vn30.empty:
        return pd.DataFrame()

    # Rebase each constituent so the synthetic VN30 basket preserves relative performance
    # while still producing stable OHLC averages across the selected window.
    working = vn30.sort_values(["symbol", "trading_date"]).copy()
    working["symbol_return_1d"] = working.groupby("symbol")["close"].pct_change()
    symbol_base_close = working.groupby("symbol")["close"].transform("first").replace(0, pd.NA)
    for column in PRICE_COLUMNS:
        working[f"normalized_{column}"] = pd.to_numeric(working[column], errors="coerce") / symbol_base_close * 100.0
    basket = (
        working.groupby("trading_date", as_index=False)
        .agg(
            local_basket_open=("normalized_open", "mean"),
            local_basket_high=("normalized_high", "mean"),
            local_basket_low=("normalized_low", "mean"),
            local_basket_close=("normalized_close", "mean"),
            vn30_volume=("volume", "sum"),
            advancers=("symbol_return_1d", lambda s: int((s > 0).sum())),
            decliners=("symbol_return_1d", lambda s: int((s < 0).sum())),
            active_symbols=("symbol", "nunique"),
        )
        .sort_values("trading_date")
    )
    basket["breadth_pct"] = (basket["advancers"] / basket["active_symbols"]) * 100.0
    basket["volume_ma20"] = _ema(basket["vn30_volume"], 20)
    basket["benchmark_gap_filled"] = False

    # Prefer a tradable Yahoo VN30 proxy, but backfill missing proxy days from the local basket
    # so the overview chart stays continuous across the VN30 trading calendar.
    benchmark_result = _load_yahoo_first_available(
        VN30_BENCHMARK_CANDIDATES,
        _period_to_yahoo(period_key),
    )
    if benchmark_result is None:
        LOGGER.warning(
            "Falling back to local VN30 basket because Yahoo benchmark proxy is unavailable",
            event_type="benchmark_fallback",
            status="warning",
            dataset_name="daily_ohlcv_10y",
        )
        for column in PRICE_COLUMNS:
            basket[f"vn30_{column}"] = basket[f"local_basket_{column}"]
        basket["benchmark_label"] = VN30_BASKET_LABEL
        basket["benchmark_ticker"] = "LOCAL_BASKET"
    else:
        benchmark_frame, benchmark_label, benchmark_ticker = benchmark_result
        benchmark_frame = _normalize_ohlc_frame(benchmark_frame.copy(), prefix="benchmark")
        basket = basket.merge(
            benchmark_frame[
                [
                    "trading_date",
                    "benchmark_open",
                    "benchmark_high",
                    "benchmark_low",
                    "benchmark_close",
                ]
            ],
            on="trading_date",
            how="left",
        ).sort_values("trading_date")
        benchmark_gap_mask = basket["benchmark_close"].isna()
        benchmark_gap_days = int(benchmark_gap_mask.sum())
        if benchmark_gap_days > 0:
            LOGGER.warning(
                "VN30 benchmark proxy contains missing dates; filled from local basket for continuity",
                event_type="benchmark_gap_fill",
                status="warning",
                dataset_name="daily_ohlcv_10y",
                benchmark_ticker=benchmark_ticker,
                missing_days=benchmark_gap_days,
                period_key=period_key,
            )
        for column in PRICE_COLUMNS:
            basket[f"vn30_{column}"] = basket[f"benchmark_{column}"].combine_first(basket[f"local_basket_{column}"])
        basket["benchmark_gap_filled"] = benchmark_gap_mask
        basket["benchmark_label"] = benchmark_label
        basket["benchmark_ticker"] = benchmark_ticker
        if basket["vn30_close"].dropna().empty:
            LOGGER.warning(
                "Yahoo benchmark proxy returned no usable VN30 rows; using local basket instead",
                event_type="benchmark_fallback",
                status="warning",
                dataset_name="daily_ohlcv_10y",
                benchmark_ticker=benchmark_ticker,
            )
            for column in PRICE_COLUMNS:
                basket[f"vn30_{column}"] = basket[f"local_basket_{column}"]
            basket["benchmark_label"] = VN30_BASKET_LABEL
            basket["benchmark_ticker"] = "LOCAL_BASKET"
        else:
            basket = basket.dropna(subset=["vn30_close"]).copy()

    basket["ema20"] = _ema(basket["vn30_close"], 20)
    basket["ema50"] = _ema(basket["vn30_close"], 50)
    basket["rsi14"] = _rsi(basket["vn30_close"], 14)
    macd = _macd(basket["vn30_close"])
    basket = pd.concat([basket, macd], axis=1)

    # VNIndex remains a Yahoo-sourced overlay. Missing source days are converted into
    # continuity candles/lines later so the dashboard does not fragment visually.
    vnindex = _load_yahoo_history(VNINDEX_TICKER, _period_to_yahoo(period_key))
    if vnindex.empty:
        for column in PRICE_COLUMNS:
            basket[f"vnindex_{column}"] = pd.NA
            basket[f"vnindex_plot_{column}"] = pd.NA
        basket["vnindex_close"] = pd.NA
        basket["vnindex_missing_source"] = False
        basket["relative_strength"] = pd.NA
        return basket

    vnindex = _normalize_ohlc_frame(vnindex.copy(), prefix="vnindex")
    common_end = min(basket["trading_date"].max(), vnindex["trading_date"].max())
    basket = basket.loc[basket["trading_date"] <= common_end].copy()
    vnindex = vnindex.loc[
        vnindex["trading_date"] <= common_end,
        [
            "trading_date",
            "vnindex_open",
            "vnindex_high",
            "vnindex_low",
            "vnindex_close",
        ],
    ].copy()
    merged = basket.merge(vnindex, on="trading_date", how="left").sort_values("trading_date")
    merged["vnindex_close_raw"] = merged["vnindex_close"]
    merged["vnindex_missing_source"] = merged["vnindex_close"].isna()
    filled_vnindex_close = merged["vnindex_close"].ffill().bfill()
    for column in PRICE_COLUMNS:
        merged[f"vnindex_plot_{column}"] = merged[f"vnindex_{column}"].fillna(filled_vnindex_close)
    merged["vnindex_close"] = filled_vnindex_close
    missing_days = int(merged["vnindex_missing_source"].sum())
    if missing_days > 0:
        LOGGER.warning(
            "VNIndex benchmark contains missing dates; forward-filled for continuity",
            event_type="benchmark_gap_fill",
            status="warning",
            dataset_name="daily_ohlcv_10y",
            missing_days=missing_days,
            period_key=period_key,
        )
    merged["relative_strength"] = (merged["vn30_close"] / merged["vnindex_close"]) * 100.0
    return merged


def build_market_overview_chart(period_key: str = "1Y", candle_source: str = "VN30") -> tuple[go.Figure, list[dict[str, str]], str]:
    """Build the home dashboard chart with selectable VN30/VNIndex candlestick focus."""
    frame = load_market_overview_data(period_key)
    if frame.empty:
        raise ValueError("No market overview data available")

    selected_source = candle_source.upper()
    # The selected source owns the candlestick, trend indicators, and Y-axis scaling.
    # The other market index stays as a comparison line behind the price action.
    if selected_source == "VNINDEX":
        selected_label = VNINDEX_LABEL
        selected_prefix = "vnindex_plot"
        selected_close_series = frame["vnindex_close"]
        comparison_label = str(frame["benchmark_label"].iloc[-1])
        comparison_series = frame["vn30_close"]
        comparison_color = COLOR_ACCENT
        rs_label = f"RS vs {comparison_label} (20d)"
    else:
        selected_source = "VN30"
        selected_label = str(frame["benchmark_label"].iloc[-1])
        selected_prefix = "vn30"
        selected_close_series = frame["vn30_close"]
        comparison_label = VNINDEX_LABEL
        comparison_series = frame["vnindex_close"]
        comparison_color = COLOR_BENCH
        rs_label = f"RS vs {comparison_label} (20d)"

    chart_frame = frame.copy()
    for column in PRICE_COLUMNS:
        chart_frame[f"selected_{column}"] = pd.to_numeric(chart_frame[f"{selected_prefix}_{column}"], errors="coerce")
    chart_frame["selected_close"] = pd.to_numeric(selected_close_series, errors="coerce")
    chart_frame["comparison_close"] = pd.to_numeric(comparison_series, errors="coerce")
    chart_frame["ema20"] = _ema(chart_frame["selected_close"], 20)
    chart_frame["ema50"] = _ema(chart_frame["selected_close"], 50)
    chart_frame["rsi14"] = _rsi(chart_frame["selected_close"], 14)
    macd = _macd(chart_frame["selected_close"])
    chart_frame["macd"] = macd["macd"]
    chart_frame["signal"] = macd["signal"]
    chart_frame["histogram"] = macd["histogram"]

    price_delta = chart_frame["selected_close"].pct_change().fillna(0)
    volume_colors = [COLOR_UP if value >= 0 else COLOR_DOWN for value in price_delta]

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.76, 0.24],
        specs=[[{"secondary_y": True}], [{"secondary_y": True}]],
        subplot_titles=(
            f"{selected_label} Candlestick vs {comparison_label}",
            "Signal Panel (toggle indicators from the legend)",
        ),
    )

    fig.add_trace(
        go.Scatter(
            x=chart_frame["trading_date"],
            y=chart_frame["comparison_close"],
            mode="lines",
            name=comparison_label,
            line=dict(color=comparison_color, width=LINE_WIDTH_THIN),
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Candlestick(
            x=chart_frame["trading_date"],
            name=selected_label,
            open=chart_frame["selected_open"],
            high=chart_frame["selected_high"],
            low=chart_frame["selected_low"],
            close=chart_frame["selected_close"],
            increasing_line_color=COLOR_UP,
            decreasing_line_color=COLOR_DOWN,
            increasing_fillcolor=COLOR_UP,
            decreasing_fillcolor=COLOR_DOWN,
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=chart_frame["trading_date"],
            y=chart_frame["ema20"],
            mode="lines",
            name="EMA 20",
            line=dict(color=COLOR_EMA_FAST, width=LINE_WIDTH_THIN),
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=chart_frame["trading_date"],
            y=chart_frame["ema50"],
            mode="lines",
            name="EMA 50",
            line=dict(color=COLOR_EMA_SLOW, width=LINE_WIDTH_THIN),
        ),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Bar(
            x=chart_frame["trading_date"],
            y=chart_frame["vn30_volume"],
            name="Volume",
            marker_color=volume_colors,
            opacity=0.18,
        ),
        row=1,
        col=1,
        secondary_y=True,
    )
    fig.add_trace(
        go.Scatter(
            x=chart_frame["trading_date"],
            y=chart_frame["volume_ma20"],
            mode="lines",
            name="Volume EMA 20",
            line=dict(color="rgba(245, 158, 11, 0.75)", width=LINE_WIDTH_THIN),
            visible=AUX_TRACE_VISIBILITY,
        ),
        row=1,
        col=1,
        secondary_y=True,
    )
    _add_signal_panel(fig, frame=chart_frame, x_col="trading_date", row=2)

    visible_frame = (
        _default_recent_view_frame(chart_frame, DEFAULT_DAILY_VIEW_BARS)
        if period_key in {"10Y", "ALL"}
        else chart_frame
    )
    price_axis_range = _compute_price_axis_range(
        visible_frame,
        low_col="selected_low",
        high_col="selected_high",
        anchor_col="selected_close",
        extra_series=[
            visible_frame["comparison_close"],
            visible_frame["ema20"],
            visible_frame["ema50"],
        ],
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=COLOR_BG,
        plot_bgcolor=COLOR_BG,
        font=dict(color=COLOR_TEXT),
        legend=_bottom_legend_config(),
        margin=dict(l=40, r=30, t=70, b=130),
        height=920,
        xaxis_rangeslider_visible=False,
    )
    if period_key in {"10Y", "ALL"}:
        default_range = _default_recent_view_range(chart_frame["trading_date"], DEFAULT_DAILY_VIEW_BARS)
        if default_range is not None:
            fig.update_xaxes(range=default_range)
    fig.update_xaxes(showgrid=False, rangebreaks=_build_trading_rangebreaks(chart_frame["trading_date"]))
    fig.update_yaxes(
        showgrid=True,
        gridcolor=COLOR_GRID,
        zeroline=False,
        secondary_y=False,
        row=1,
        col=1,
        range=price_axis_range,
    )
    fig.update_yaxes(showgrid=False, showticklabels=False, zeroline=False, secondary_y=True, row=1, col=1)
    fig.update_yaxes(showgrid=True, gridcolor=COLOR_GRID, zeroline=False, range=[0, 100], row=2, col=1)
    fig.update_yaxes(showgrid=False, zeroline=False, secondary_y=True, row=2, col=1)

    latest = chart_frame.iloc[-1]
    prev_20 = chart_frame.iloc[-21] if len(chart_frame) > 20 else chart_frame.iloc[0]
    relative_strength_20d = None
    if pd.notna(latest["comparison_close"]) and pd.notna(prev_20["comparison_close"]):
        selected_return = (latest["selected_close"] / prev_20["selected_close"] - 1.0) * 100.0
        comparison_return = (latest["comparison_close"] / prev_20["comparison_close"] - 1.0) * 100.0
        relative_strength_20d = selected_return - comparison_return

    metrics = [
        {
            "label": "Trend",
            "value": _trend_label(
                _latest_value(chart_frame["selected_close"]),
                _latest_value(chart_frame["ema20"]),
                _latest_value(chart_frame["ema50"]),
            ),
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
        {"label": rs_label, "value": _fmt_pct(relative_strength_20d)},
    ]
    benchmark_label = str(frame["benchmark_label"].iloc[-1])
    benchmark_ticker = str(frame["benchmark_ticker"].iloc[-1])
    benchmark_gap_filled_days = int(frame["benchmark_gap_filled"].sum()) if "benchmark_gap_filled" in frame.columns else 0
    missing_vnindex_days = int(frame["vnindex_missing_source"].sum()) if "vnindex_missing_source" in frame.columns else 0
    note = (
        f"Candlestick source: {selected_label}. Comparison overlay: {comparison_label}. "
        f"VN30 benchmark data uses {benchmark_label} ({benchmark_ticker}) and is rebased to 100 at the start of the selected range. "
        "Volume and breadth are aggregated from local VN30 constituents. "
        f"Missing VN30 benchmark candles are backfilled from the local VN30 basket for continuity ({benchmark_gap_filled_days} filled days in this range). "
        f"VNIndex uses Yahoo historical Vietnam VN Index series rebased to the same start point; missing VNIndex dates are forward-filled into doji continuity candles on the VN30 trading calendar ({missing_vnindex_days} filled days in this range). "
        "Auxiliary signal traces are hidden by default; click the legends below the plot to enable them."
    )
    return fig, metrics, note


def _load_symbol_daily(symbol: str) -> pd.DataFrame:
    """Load daily OHLCV rows for a single symbol."""
    local = _load_local_daily()
    frame = local.loc[local["symbol"] == symbol].copy()
    return frame.sort_values("trading_date").reset_index(drop=True)


def _prepare_symbol_daily(symbol: str, period_key: str) -> pd.DataFrame:
    """Prepare daily OHLCV plus indicators for the symbol explorer."""
    frame = _filter_by_period(_load_symbol_daily(symbol), "trading_date", period_key)
    if frame.empty:
        return frame
    frame = frame.sort_values("trading_date").copy()
    frame["ema20"] = _ema(frame["close"], 20)
    frame["ema50"] = _ema(frame["close"], 50)
    frame["ema200"] = _ema(frame["close"], 200)
    frame["volume_ma20"] = _ema(frame["volume"], 20)
    frame["rsi14"] = _rsi(frame["close"], 14)
    macd = _macd(frame["close"])
    return pd.concat([frame, macd], axis=1)


def _prepare_symbol_intraday(symbol: str, period_key: str) -> pd.DataFrame:
    """Prepare intraday OHLCV plus indicators for the symbol explorer."""
    frame = _filter_by_period(_load_local_intraday(symbol), "bar_time", period_key)
    if frame.empty:
        return frame
    frame = frame.sort_values("bar_time").copy()
    frame["ema20"] = _ema(frame["close"], 20)
    frame["ema50"] = _ema(frame["close"], 50)
    frame["vwap"] = _session_vwap(frame)
    frame["volume_ma20"] = _ema(frame["volume"], 20)
    frame["rsi14"] = _rsi(frame["close"], 14)
    macd = _macd(frame["close"])
    return pd.concat([frame, macd], axis=1)


def _symbol_relative_strength(symbol: str, lookback_days: int = 20) -> float | None:
    """Measure a symbol's trailing relative performance against the VN30 benchmark basket."""
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


def build_symbol_chart(symbol: str, mode: str = "daily", period_key: str = "1Y") -> tuple[go.Figure, list[dict[str, str]], str]:
    """Build the symbol explorer chart for either daily or 15-minute price action."""
    if mode == "intraday":
        frame = _prepare_symbol_intraday(symbol, period_key)
        if frame.empty:
            raise ValueError(f"No intraday data available for {symbol}")
        x_col = "bar_time"
        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.76, 0.24],
            specs=[[{"secondary_y": True}], [{"secondary_y": True}]],
            subplot_titles=(
                f"{symbol} 15m Candlestick",
                "Signal Panel (toggle indicators from the legend)",
            ),
        )
        candle = go.Candlestick(
            x=frame[x_col],
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name="Price",
            increasing_line_color=COLOR_UP,
            decreasing_line_color=COLOR_DOWN,
        )
        fig.add_trace(candle, row=1, col=1)
        for series_name, color in [
            ("ema20", COLOR_EMA_FAST),
            ("ema50", COLOR_EMA_SLOW),
            ("vwap", COLOR_VWAP),
        ]:
            fig.add_trace(
                go.Scatter(
                    x=frame[x_col],
                    y=frame[series_name],
                    mode="lines",
                    name=series_name.upper(),
                    line=dict(color=color, width=LINE_WIDTH_THIN),
                ),
                row=1,
                col=1,
            )
        note = "Intraday chart uses local 15m bars with session VWAP and EMA 20/50 on the price panel. Auxiliary signals live in the lower signal panel and are hidden by default until you click their legends."
        metrics = [
            {"label": "Last Price", "value": _fmt_float(_latest_value(frame["close"]))},
            {"label": "Trend", "value": _trend_label(_latest_value(frame["close"]), _latest_value(frame["ema20"]), _latest_value(frame["ema50"]))},
            {"label": "RSI 14", "value": f"{_fmt_float(_latest_value(frame['rsi14']))} ({_rsi_label(_latest_value(frame['rsi14']))})"},
            {"label": "MACD", "value": _macd_label(_latest_value(frame["macd"]), _latest_value(frame["signal"]))},
        ]
    else:
        frame = _prepare_symbol_daily(symbol, period_key)
        if frame.empty:
            raise ValueError(f"No daily data available for {symbol}")
        x_col = "trading_date"
        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.76, 0.24],
            specs=[[{"secondary_y": True}], [{"secondary_y": True}]],
            subplot_titles=(
                f"{symbol} Daily Candlestick",
                "Signal Panel (toggle indicators from the legend)",
            ),
        )
        candle = go.Candlestick(
            x=frame[x_col],
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name="Price",
            increasing_line_color=COLOR_UP,
            decreasing_line_color=COLOR_DOWN,
        )
        fig.add_trace(candle, row=1, col=1)
        for series_name, color in [
            ("ema20", COLOR_EMA_FAST),
            ("ema50", COLOR_EMA_SLOW),
            ("ema200", COLOR_EMA_LONG),
        ]:
            fig.add_trace(
                go.Scatter(
                    x=frame[x_col],
                    y=frame[series_name],
                    mode="lines",
                    name=series_name.upper(),
                    line=dict(color=color, width=LINE_WIDTH_THIN),
                ),
                row=1,
                col=1,
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
        ]
        note = "Daily chart uses local 10-year OHLCV with EMA 20/50/200 on the price panel. Auxiliary signals live in the lower signal panel and are hidden by default until you click their legends."

    price_delta = frame["close"].pct_change().fillna(0)
    volume_colors = [COLOR_UP if value >= 0 else COLOR_DOWN for value in price_delta]

    fig.add_trace(
        go.Bar(
            x=frame[x_col],
            y=frame["volume"],
            name="Volume",
            marker_color=volume_colors,
            opacity=0.18,
        ),
        row=1,
        col=1,
        secondary_y=True,
    )
    fig.add_trace(
        go.Scatter(
            x=frame[x_col],
            y=frame["volume_ma20"],
            mode="lines",
            name="Volume EMA 20",
            line=dict(color="rgba(245, 158, 11, 0.75)", width=LINE_WIDTH_THIN),
            visible=AUX_TRACE_VISIBILITY,
        ),
        row=1,
        col=1,
        secondary_y=True,
    )
    _add_signal_panel(fig, frame=frame, x_col=x_col, row=2)

    if mode == "daily" and period_key in {"10Y", "ALL"}:
        visible_frame = _default_recent_view_frame(frame, DEFAULT_DAILY_VIEW_BARS)
        price_axis_range = _compute_price_axis_range(
            visible_frame,
            low_col="low",
            high_col="high",
            anchor_col="close",
            extra_series=[
                visible_frame["ema20"],
                visible_frame["ema50"],
                visible_frame["ema200"],
            ],
        )
    elif mode == "intraday":
        price_axis_range = _compute_price_axis_range(
            frame,
            low_col="low",
            high_col="high",
            anchor_col="close",
            extra_series=[
                frame["ema20"],
                frame["ema50"],
                frame["vwap"],
            ],
        )
    else:
        price_axis_range = _compute_price_axis_range(
            frame,
            low_col="low",
            high_col="high",
            anchor_col="close",
            extra_series=[
                frame["ema20"],
                frame["ema50"],
                frame["ema200"],
            ],
        )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=COLOR_BG,
        plot_bgcolor=COLOR_BG,
        font=dict(color=COLOR_TEXT),
        legend=_bottom_legend_config(),
        margin=dict(l=40, r=30, t=70, b=130),
        height=920,
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor=COLOR_GRID,
        zeroline=False,
        secondary_y=False,
        row=1,
        col=1,
        range=price_axis_range,
    )
    fig.update_yaxes(showgrid=False, showticklabels=False, zeroline=False, secondary_y=True, row=1, col=1)
    fig.update_yaxes(showgrid=True, gridcolor=COLOR_GRID, zeroline=False, range=[0, 100], row=2, col=1)
    fig.update_yaxes(showgrid=False, zeroline=False, secondary_y=True, row=2, col=1)
    if mode == "daily":
        if period_key in {"10Y", "ALL"}:
            default_range = _default_recent_view_range(frame["trading_date"], DEFAULT_DAILY_VIEW_BARS)
            if default_range is not None:
                fig.update_xaxes(range=default_range)
        fig.update_xaxes(showgrid=False, rangebreaks=_build_trading_rangebreaks(frame["trading_date"]))
    else:
        fig.update_xaxes(showgrid=False)
    return fig, metrics, note
