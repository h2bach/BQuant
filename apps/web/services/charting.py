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


OVERVIEW_PERIOD_OPTIONS = ["6M", "1Y", "3Y", "5Y"]
SYMBOL_DAILY_RANGE_OPTIONS = ["3M", "6M", "1Y", "3Y", "5Y", "ALL"]
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

LOGGER = BQuantLogger("web_charting", component="web", subcomponent="charting", default_channel="web")


def _flatten_yf_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        frame = df.copy()
        frame.columns = [column[0] if isinstance(column, tuple) else column for column in frame.columns]
        return frame
    return df


def _period_to_offset(period_key: str) -> pd.DateOffset | None:
    mapping = {
        "3M": pd.DateOffset(months=3),
        "6M": pd.DateOffset(months=6),
        "1Y": pd.DateOffset(years=1),
        "3Y": pd.DateOffset(years=3),
        "5Y": pd.DateOffset(years=5),
        "5D": pd.DateOffset(days=5),
        "20D": pd.DateOffset(days=20),
        "60D": pd.DateOffset(days=60),
        "ALL": None,
    }
    return mapping.get(period_key)


def _period_to_yahoo(period_key: str) -> str:
    mapping = {
        "3M": "3mo",
        "6M": "6mo",
        "1Y": "1y",
        "3Y": "3y",
        "5Y": "5y",
        "ALL": "max",
    }
    return mapping.get(period_key, "1y")


def _filter_by_period(frame: pd.DataFrame, date_col: str, period_key: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    offset = _period_to_offset(period_key)
    if offset is None:
        return frame.copy()
    end_ts = pd.to_datetime(frame[date_col]).max()
    start_ts = end_ts - offset
    return frame.loc[pd.to_datetime(frame[date_col]) >= start_ts].copy()


def _build_trading_rangebreaks(dates: pd.Series) -> list[dict[str, Any]]:
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


def _normalize_base_100(series: pd.Series) -> pd.Series:
    clean = pd.to_numeric(series, errors="coerce")
    first_valid = clean.dropna().iloc[0] if not clean.dropna().empty else None
    if first_valid in (None, 0):
        return clean
    return clean / first_valid * 100.0


def _ema(series: pd.Series, span: int) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    close = pd.to_numeric(series, errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi.bfill()


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
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
    working = frame.copy()
    typical_price = (working["high"] + working["low"] + working["close"]) / 3.0
    px_vol = typical_price * working["volume"]
    cumulative_px_vol = px_vol.groupby(working["session_date"]).cumsum()
    cumulative_vol = working["volume"].groupby(working["session_date"]).cumsum().replace(0, pd.NA)
    return cumulative_px_vol / cumulative_vol


def _latest_value(series: pd.Series) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    return float(clean.iloc[-1])


def _fmt_float(value: float | None, suffix: str = "", decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value:.{decimals}f}{suffix}"


def _fmt_pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value:+.2f}%"


def _trend_label(price: float | None, fast: float | None, slow: float | None) -> str:
    if price is None or fast is None or slow is None:
        return "N/A"
    if price > fast > slow:
        return "Bullish"
    if price < fast < slow:
        return "Bearish"
    return "Neutral"


def _rsi_label(value: float | None) -> str:
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
    if macd is None or signal is None:
        return "N/A"
    return "Bullish Cross" if macd >= signal else "Bearish Cross"


@lru_cache(maxsize=8)
def _load_local_daily_cached(refresh_version: int) -> pd.DataFrame:
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
    refresh_version = get_refresh_version("daily_ohlcv_10y")
    return _load_local_daily_cached(refresh_version).copy()


@lru_cache(maxsize=128)
def _load_local_intraday_cached(symbol: str, base_refresh_version: int, delta_refresh_version: int) -> pd.DataFrame:
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
    base_refresh_version = get_refresh_version("intraday_ohlcv_15m_60d")
    delta_refresh_version = get_refresh_version("intraday_ohlcv_15m_delta")
    return _load_local_intraday_cached(symbol, base_refresh_version, delta_refresh_version).copy()


def _load_yahoo_first_available(
    candidates: list[tuple[str, str]],
    period: str,
) -> tuple[pd.DataFrame, str, str] | None:
    for ticker, label in candidates:
        frame = _load_yahoo_history(ticker, period)
        if not frame.empty:
            return frame, label, ticker
    return None


def load_market_overview_data(period_key: str = "1Y") -> pd.DataFrame:
    vn30 = _load_local_daily()
    if vn30.empty:
        return pd.DataFrame()
    vn30 = _filter_by_period(vn30, "trading_date", period_key)
    if vn30.empty:
        return pd.DataFrame()

    working = vn30.sort_values(["symbol", "trading_date"]).copy()
    working["symbol_return_1d"] = working.groupby("symbol")["close"].pct_change()
    working["normalized_close"] = working.groupby("symbol")["close"].transform(_normalize_base_100)
    basket = (
        working.groupby("trading_date", as_index=False)
        .agg(
            vn30_close=("normalized_close", "mean"),
            vn30_volume=("volume", "sum"),
            advancers=("symbol_return_1d", lambda s: int((s > 0).sum())),
            decliners=("symbol_return_1d", lambda s: int((s < 0).sum())),
            active_symbols=("symbol", "nunique"),
        )
        .sort_values("trading_date")
    )
    basket["local_basket_close"] = basket["vn30_close"]
    basket["breadth_pct"] = (basket["advancers"] / basket["active_symbols"]) * 100.0
    basket["volume_ma20"] = _ema(basket["vn30_volume"], 20)

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
        basket["vn30_close"] = basket["local_basket_close"]
        basket["benchmark_label"] = VN30_BASKET_LABEL
        basket["benchmark_ticker"] = "LOCAL_BASKET"
    else:
        benchmark_frame, benchmark_label, benchmark_ticker = benchmark_result
        benchmark_frame = benchmark_frame.copy()
        benchmark_frame["benchmark_close"] = _normalize_base_100(benchmark_frame["close"])
        basket = basket.merge(
            benchmark_frame[["trading_date", "benchmark_close"]],
            on="trading_date",
            how="left",
        ).sort_values("trading_date")
        basket["vn30_close"] = basket["benchmark_close"]
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
            basket["vn30_close"] = basket["local_basket_close"]
            basket["benchmark_label"] = VN30_BASKET_LABEL
            basket["benchmark_ticker"] = "LOCAL_BASKET"
        else:
            basket = basket.dropna(subset=["vn30_close"]).copy()

    basket["ema20"] = _ema(basket["vn30_close"], 20)
    basket["ema50"] = _ema(basket["vn30_close"], 50)
    basket["rsi14"] = _rsi(basket["vn30_close"], 14)
    macd = _macd(basket["vn30_close"])
    basket = pd.concat([basket, macd], axis=1)

    vnindex = _load_yahoo_history(VNINDEX_TICKER, _period_to_yahoo(period_key))
    if vnindex.empty:
        basket["vnindex_close"] = pd.NA
        basket["relative_strength"] = pd.NA
        return basket

    vnindex = vnindex.copy()
    vnindex["vnindex_close"] = _normalize_base_100(vnindex["close"])
    common_end = min(basket["trading_date"].max(), vnindex["trading_date"].max())
    basket = basket.loc[basket["trading_date"] <= common_end].copy()
    vnindex = vnindex.loc[vnindex["trading_date"] <= common_end, ["trading_date", "vnindex_close"]].copy()
    merged = basket.merge(vnindex, on="trading_date", how="left").sort_values("trading_date")
    merged["vnindex_close_raw"] = merged["vnindex_close"]
    merged["vnindex_missing_source"] = merged["vnindex_close"].isna()
    merged["vnindex_close"] = merged["vnindex_close"].ffill().bfill()
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


def build_market_overview_chart(period_key: str = "1Y") -> tuple[go.Figure, list[dict[str, str]], str]:
    frame = load_market_overview_data(period_key)
    if frame.empty:
        raise ValueError("No market overview data available")

    price_delta = frame["vn30_close"].pct_change().fillna(0)
    hist_colors = [COLOR_UP if value >= 0 else COLOR_DOWN for value in frame["histogram"].fillna(0)]
    volume_colors = [COLOR_UP if value >= 0 else COLOR_DOWN for value in price_delta]

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.66, 0.17, 0.17],
        specs=[[{"secondary_y": True}], [{}], [{}]],
        subplot_titles=(
            "VNIndex vs VN30",
            "RSI (14)",
            "MACD (12, 26, 9)",
        ),
    )

    fig.add_trace(
        go.Scatter(
            x=frame["trading_date"],
            y=frame["vnindex_close"],
            mode="lines",
            name=VNINDEX_LABEL,
            line=dict(color=COLOR_BENCH, width=2),
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["trading_date"],
            y=frame["vn30_close"],
            mode="lines",
            name=str(frame["benchmark_label"].iloc[-1]),
            line=dict(color=COLOR_ACCENT, width=3),
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["trading_date"],
            y=frame["ema20"],
            mode="lines",
            name="EMA 20",
            line=dict(color=COLOR_EMA_FAST, width=1.8, dash="dot"),
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["trading_date"],
            y=frame["ema50"],
            mode="lines",
            name="EMA 50",
            line=dict(color=COLOR_EMA_SLOW, width=1.8, dash="dash"),
        ),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Bar(
            x=frame["trading_date"],
            y=frame["vn30_volume"],
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
            x=frame["trading_date"],
            y=frame["volume_ma20"],
            mode="lines",
            name="Volume EMA 20",
            line=dict(color="rgba(245, 158, 11, 0.55)", width=1.2),
        ),
        row=1,
        col=1,
        secondary_y=True,
    )

    fig.add_trace(
        go.Scatter(
            x=frame["trading_date"],
            y=frame["rsi14"],
            mode="lines",
            name="RSI 14",
            line=dict(color=COLOR_RSI, width=2),
        ),
        row=2,
        col=1,
    )
    for level, color in [(70, COLOR_DOWN), (50, "rgba(226,232,240,0.35)"), (30, COLOR_UP)]:
        fig.add_hline(y=level, line_color=color, line_width=1, line_dash="dot", row=2, col=1)

    fig.add_trace(
        go.Bar(
            x=frame["trading_date"],
            y=frame["histogram"],
            name="MACD Hist",
            marker_color=hist_colors,
            opacity=0.75,
        ),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["trading_date"],
            y=frame["macd"],
            mode="lines",
            name="MACD",
            line=dict(color=COLOR_MACD, width=2),
        ),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["trading_date"],
            y=frame["signal"],
            mode="lines",
            name="Signal",
            line=dict(color=COLOR_SIGNAL, width=1.8),
        ),
        row=3,
        col=1,
    )
    fig.add_hline(y=0, line_color="rgba(226,232,240,0.35)", line_width=1, row=3, col=1)

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=COLOR_BG,
        plot_bgcolor=COLOR_BG,
        font=dict(color=COLOR_TEXT),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=40, r=30, t=70, b=30),
        height=980,
        xaxis_rangeslider_visible=False,
    )
    fig.update_xaxes(showgrid=False, rangebreaks=_build_trading_rangebreaks(frame["trading_date"]))
    fig.update_yaxes(showgrid=True, gridcolor=COLOR_GRID, zeroline=False, secondary_y=False, row=1, col=1)
    fig.update_yaxes(showgrid=False, showticklabels=False, zeroline=False, secondary_y=True, row=1, col=1)
    fig.update_yaxes(showgrid=True, gridcolor=COLOR_GRID, zeroline=False, row=2, col=1)
    fig.update_yaxes(showgrid=True, gridcolor=COLOR_GRID, zeroline=False, row=3, col=1)

    latest = frame.iloc[-1]
    prev_20 = frame.iloc[-21] if len(frame) > 20 else frame.iloc[0]
    relative_strength_20d = None
    if pd.notna(latest["vnindex_close"]) and pd.notna(prev_20["vnindex_close"]):
        vn30_return = (latest["vn30_close"] / prev_20["vn30_close"] - 1.0) * 100.0
        vni_return = (latest["vnindex_close"] / prev_20["vnindex_close"] - 1.0) * 100.0
        relative_strength_20d = vn30_return - vni_return

    metrics = [
        {"label": "Trend", "value": _trend_label(_latest_value(frame["vn30_close"]), _latest_value(frame["ema20"]), _latest_value(frame["ema50"]))},
        {
            "label": "Breadth",
            "value": f"{int(latest['advancers'])}/{int(latest['decliners'])} | {_fmt_float(float(latest['breadth_pct']), suffix='%', decimals=1)} advancing",
        },
        {"label": "RSI 14", "value": f"{_fmt_float(_latest_value(frame['rsi14']))} ({_rsi_label(_latest_value(frame['rsi14']))})"},
        {"label": "MACD", "value": _macd_label(_latest_value(frame["macd"]), _latest_value(frame["signal"]))},
        {"label": "RS vs VNIndex (20d)", "value": _fmt_pct(relative_strength_20d)},
    ]
    benchmark_label = str(frame["benchmark_label"].iloc[-1])
    benchmark_ticker = str(frame["benchmark_ticker"].iloc[-1])
    missing_vnindex_days = int(frame["vnindex_missing_source"].sum()) if "vnindex_missing_source" in frame.columns else 0
    note = (
        f"VN30 line uses {benchmark_label} ({benchmark_ticker}) from Yahoo Finance. "
        "Volume and breadth are aggregated from local VN30 constituents. "
        f"VNIndex uses Yahoo historical Vietnam VN Index series. Missing Yahoo benchmark dates are forward-filled on the VN30 trading calendar for chart continuity ({missing_vnindex_days} filled days in this range)."
    )
    return fig, metrics, note


def _load_symbol_daily(symbol: str) -> pd.DataFrame:
    local = _load_local_daily()
    frame = local.loc[local["symbol"] == symbol].copy()
    return frame.sort_values("trading_date").reset_index(drop=True)


def _prepare_symbol_daily(symbol: str, period_key: str) -> pd.DataFrame:
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
    if mode == "intraday":
        frame = _prepare_symbol_intraday(symbol, period_key)
        if frame.empty:
            raise ValueError(f"No intraday data available for {symbol}")
        x_col = "bar_time"
        fig = make_subplots(
            rows=3,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.68, 0.16, 0.16],
            specs=[[{"secondary_y": True}], [{}], [{}]],
            subplot_titles=(
                f"{symbol} 15m Candlestick",
                "RSI (14)",
                "MACD (12, 26, 9)",
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
        for series_name, color, dash in [
            ("ema20", COLOR_EMA_FAST, "dot"),
            ("ema50", COLOR_EMA_SLOW, "dash"),
            ("vwap", COLOR_VWAP, "solid"),
        ]:
            fig.add_trace(
                go.Scatter(
                    x=frame[x_col],
                    y=frame[series_name],
                    mode="lines",
                    name=series_name.upper(),
                    line=dict(color=color, width=1.8, dash=dash),
                ),
                row=1,
                col=1,
            )
        note = "Intraday chart uses local 15m bars with session VWAP, EMA 20/50, RSI, and MACD."
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
            rows=3,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.68, 0.16, 0.16],
            specs=[[{"secondary_y": True}], [{}], [{}]],
            subplot_titles=(
                f"{symbol} Daily Candlestick",
                "RSI (14)",
                "MACD (12, 26, 9)",
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
        for series_name, color, dash in [
            ("ema20", COLOR_EMA_FAST, "dot"),
            ("ema50", COLOR_EMA_SLOW, "dash"),
            ("ema200", COLOR_EMA_LONG, "solid"),
        ]:
            fig.add_trace(
                go.Scatter(
                    x=frame[x_col],
                    y=frame[series_name],
                    mode="lines",
                    name=series_name.upper(),
                    line=dict(color=color, width=1.8, dash=dash),
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
        note = "Daily chart uses local 10-year OHLCV with EMA 20/50/200, volume, RSI, and MACD."

    price_delta = frame["close"].pct_change().fillna(0)
    volume_colors = [COLOR_UP if value >= 0 else COLOR_DOWN for value in price_delta]
    hist_colors = [COLOR_UP if value >= 0 else COLOR_DOWN for value in frame["histogram"].fillna(0)]

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
            line=dict(color="rgba(245, 158, 11, 0.55)", width=1.2),
        ),
        row=1,
        col=1,
        secondary_y=True,
    )
    fig.add_trace(
        go.Scatter(
            x=frame[x_col],
            y=frame["rsi14"],
            mode="lines",
            name="RSI 14",
            line=dict(color=COLOR_RSI, width=2),
        ),
        row=2,
        col=1,
    )
    for level, color in [(70, COLOR_DOWN), (50, "rgba(226,232,240,0.35)"), (30, COLOR_UP)]:
        fig.add_hline(y=level, line_color=color, line_width=1, line_dash="dot", row=2, col=1)
    fig.add_trace(
        go.Bar(
            x=frame[x_col],
            y=frame["histogram"],
            name="MACD Hist",
            marker_color=hist_colors,
            opacity=0.75,
        ),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame[x_col],
            y=frame["macd"],
            mode="lines",
            name="MACD",
            line=dict(color=COLOR_MACD, width=2),
        ),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame[x_col],
            y=frame["signal"],
            mode="lines",
            name="Signal",
            line=dict(color=COLOR_SIGNAL, width=1.8),
        ),
        row=3,
        col=1,
    )
    fig.add_hline(y=0, line_color="rgba(226,232,240,0.35)", line_width=1, row=3, col=1)

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=COLOR_BG,
        plot_bgcolor=COLOR_BG,
        font=dict(color=COLOR_TEXT),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=40, r=30, t=70, b=30),
        height=980,
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(showgrid=True, gridcolor=COLOR_GRID, zeroline=False, secondary_y=False, row=1, col=1)
    fig.update_yaxes(showgrid=False, showticklabels=False, zeroline=False, secondary_y=True, row=1, col=1)
    fig.update_yaxes(showgrid=True, gridcolor=COLOR_GRID, zeroline=False, row=2, col=1)
    fig.update_yaxes(showgrid=True, gridcolor=COLOR_GRID, zeroline=False, row=3, col=1)
    if mode == "daily":
        fig.update_xaxes(showgrid=False, rangebreaks=_build_trading_rangebreaks(frame["trading_date"]))
    else:
        fig.update_xaxes(showgrid=False)
    return fig, metrics, note
