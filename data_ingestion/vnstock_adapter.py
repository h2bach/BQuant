"""Shared vnstock adapter for BQuant market datasets."""

from __future__ import annotations

import contextlib
import io
from typing import Any

import pandas as pd


DEFAULT_SOURCE = "VCI"
DEFAULT_SOURCE_LABEL = "VCI-data-source"
STANDARD_COLUMNS = [
    "symbol",
    "trading_date",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "source",
]
INTRADAY_COLUMNS = [
    "symbol",
    "bar_time",
    "session_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "interval",
    "source",
]


def _import_quote() -> tuple[Any, str]:
    """Import `vnstock.Quote` while capturing package notices.

    Returns:
        Tuple containing the imported `Quote` class and captured stdout/stderr
        text emitted during import.

    Side Effects:
        Imports the third-party `vnstock` package lazily so modules that only
        need schemas can load without initializing the provider client.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        from vnstock import Quote  # type: ignore

    return Quote, buffer.getvalue().strip()


def _call_suppressed(func: Any, *args: Any, **kwargs: Any) -> tuple[Any, str]:
    """Call a noisy vnstock function while preserving its output.

    Args:
        func: Callable to execute, usually `Quote(...)` or `quote.history(...)`.
        *args: Positional arguments passed through to `func`.
        **kwargs: Keyword arguments passed through to `func`.

    Returns:
        Tuple of the callable result and captured stdout/stderr text.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        result = func(*args, **kwargs)
    return result, buffer.getvalue().strip()


def _empty_daily_frame() -> pd.DataFrame:
    """Return an empty frame with the canonical daily OHLCV schema.

    Returns:
        Empty DataFrame with `STANDARD_COLUMNS` in stable order.
    """
    return pd.DataFrame(columns=STANDARD_COLUMNS)


def _empty_intraday_frame() -> pd.DataFrame:
    """Return an empty frame with the canonical intraday OHLCV schema.

    Returns:
        Empty DataFrame with `INTRADAY_COLUMNS` in stable order.
    """
    return pd.DataFrame(columns=INTRADAY_COLUMNS)


def _inclusive_time_bounds(start_value: str, end_value: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Parse start/end strings into inclusive timestamp bounds.

    Args:
        start_value: Start date or datetime accepted by `pd.to_datetime`.
        end_value: End date or datetime accepted by `pd.to_datetime`.

    Returns:
        Tuple of `(start_ts, end_ts)`. Date-only end values are extended to the
        final microsecond of that day so intraday filters include all bars.

    Raises:
        ValueError: If either bound cannot be parsed.
    """
    start_ts = pd.to_datetime(start_value, errors="coerce")
    end_ts = pd.to_datetime(end_value, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts):
        raise ValueError(f"Invalid datetime bounds: start={start_value}, end={end_value}")
    if len(str(end_value).strip()) <= 10:
        end_ts = end_ts + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return start_ts, end_ts


def _repair_ohlc_bounds(frame: pd.DataFrame) -> pd.DataFrame:
    """Repair high/low bounds so OHLC rows remain internally consistent.

    Args:
        frame: DataFrame containing `open`, `high`, `low`, and `close`.

    Returns:
        Copy of `frame` where `high` is the row-wise max of OHLC and `low` is
        the row-wise min of OHLC.
    """
    repaired = frame.copy()
    price_columns = ["open", "high", "low", "close"]
    repaired["high"] = repaired[price_columns].max(axis=1)
    repaired["low"] = repaired[price_columns].min(axis=1)
    return repaired


def fetch_daily_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    source: str = DEFAULT_SOURCE,
) -> pd.DataFrame:
    """Fetch standardized daily OHLCV history for one symbol from vnstock.

    Args:
        symbol: Exchange ticker or index symbol accepted by the vnstock
            `VCI-data-source` provider.
        start_date: Inclusive start date in `YYYY-MM-DD` form.
        end_date: Inclusive end date in `YYYY-MM-DD` form.
        source: vnstock API source code. BQuant defaults to `VCI`, which is
            documented as the `VCI-data-source` provider to avoid confusion
            with the `VCI` stock symbol.

    Returns:
        DataFrame with `symbol`, `trading_date`, `open`, `high`, `low`,
        `close`, `adjusted_close`, `volume`, and `source`. Empty/provider-error
        responses return an empty frame with the same schema.

    Notes:
        vnstock can return rows outside the requested range, so this adapter
        applies a post-fetch date filter and removes duplicate `(symbol,
        trading_date)` rows before returning.
    """
    normalized_symbol = str(symbol).upper()
    normalized_source = str(source or DEFAULT_SOURCE).upper()
    Quote, _ = _import_quote()
    quote, _ = _call_suppressed(Quote, symbol=normalized_symbol, source=normalized_source, show_log=False)
    raw, _ = _call_suppressed(
        quote.history,
        symbol=normalized_symbol,
        start=start_date,
        end=end_date,
        interval="d",
    )
    if raw is None or raw.empty:
        return _empty_daily_frame()

    frame = raw.copy()
    if "time" not in frame.columns:
        return _empty_daily_frame()

    frame["trading_date"] = pd.to_datetime(frame["time"], errors="coerce").dt.date
    start_bound = pd.to_datetime(start_date, errors="coerce").date()
    end_bound = pd.to_datetime(end_date, errors="coerce").date()
    frame = frame.loc[(frame["trading_date"] >= start_bound) & (frame["trading_date"] <= end_bound)].copy()
    if frame.empty:
        return _empty_daily_frame()

    frame["symbol"] = normalized_symbol
    frame["source"] = f"vnstock:{normalized_source.lower()}"
    frame["adjusted_close"] = frame["close"]
    output = frame[STANDARD_COLUMNS].copy()
    for column in ["open", "high", "low", "close", "adjusted_close", "volume"]:
        output[column] = pd.to_numeric(output[column], errors="coerce")

    output = output.dropna(subset=["trading_date", "open", "high", "low", "close", "volume"])
    output = output.loc[
        (output["open"] > 0)
        & (output["high"] > 0)
        & (output["low"] > 0)
        & (output["close"] > 0)
        & (output["volume"] >= 0)
    ].copy()
    if output.empty:
        return _empty_daily_frame()

    output = _repair_ohlc_bounds(output)
    output = output.sort_values(["symbol", "trading_date"]).drop_duplicates(["symbol", "trading_date"], keep="last")
    return output.reset_index(drop=True)


def fetch_intraday_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    interval: str = "15m",
    source: str = DEFAULT_SOURCE,
) -> pd.DataFrame:
    """Fetch standardized intraday OHLCV history for one symbol from vnstock.

    Args:
        symbol: Exchange ticker accepted by the vnstock `VCI-data-source`
            provider.
        start_date: Inclusive start date/datetime bound.
        end_date: Inclusive end date/datetime bound.
        interval: vnstock intraday interval such as `15m`.
        source: vnstock API source code. BQuant defaults to `VCI`, which is
            documented as the `VCI-data-source` provider to avoid confusion
            with the `VCI` stock symbol.

    Returns:
        DataFrame with `symbol`, `bar_time`, `session_date`, OHLCV, `interval`,
        and `source`. Empty/provider-error responses return an empty frame with
        the same schema.

    Notes:
        Timestamp output is normalized to timezone-naive Asia/Ho_Chi_Minh wall
        time because DuckDB stores BQuant intraday bars without timezone.
    """
    normalized_symbol = str(symbol).upper()
    normalized_source = str(source or DEFAULT_SOURCE).upper()
    normalized_interval = str(interval or "15m")
    Quote, _ = _import_quote()
    quote, _ = _call_suppressed(Quote, symbol=normalized_symbol, source=normalized_source, show_log=False)
    provider = getattr(quote, "provider", quote)
    raw, _ = _call_suppressed(
        provider.history,
        start=start_date,
        end=end_date,
        interval=normalized_interval,
        show_log=False,
    )
    if raw is None or raw.empty:
        return _empty_intraday_frame()

    frame = raw.copy()
    if "time" not in frame.columns:
        return _empty_intraday_frame()

    start_bound, end_bound = _inclusive_time_bounds(start_date, end_date)
    frame["bar_time"] = pd.to_datetime(frame["time"], errors="coerce")
    if getattr(frame["bar_time"].dt, "tz", None) is not None:
        frame["bar_time"] = frame["bar_time"].dt.tz_convert("Asia/Ho_Chi_Minh").dt.tz_localize(None)
    frame = frame.loc[(frame["bar_time"] >= start_bound) & (frame["bar_time"] <= end_bound)].copy()
    if frame.empty:
        return _empty_intraday_frame()

    frame["symbol"] = normalized_symbol
    frame["session_date"] = frame["bar_time"].dt.date
    frame["interval"] = normalized_interval.lower()
    frame["source"] = f"vnstock:{normalized_source.lower()}"
    output = frame[INTRADAY_COLUMNS].copy()
    for column in ["open", "high", "low", "close", "volume"]:
        output[column] = pd.to_numeric(output[column], errors="coerce")

    output = output.dropna(subset=["bar_time", "session_date", "open", "high", "low", "close", "volume"])
    output = output.loc[
        (output["open"] > 0)
        & (output["high"] > 0)
        & (output["low"] > 0)
        & (output["close"] > 0)
        & (output["volume"] >= 0)
    ].copy()
    if output.empty:
        return _empty_intraday_frame()

    output = _repair_ohlc_bounds(output)
    output = output.sort_values(["symbol", "bar_time"]).drop_duplicates(["symbol", "bar_time"], keep="last")
    return output.reset_index(drop=True)
