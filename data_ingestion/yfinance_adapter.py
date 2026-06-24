"""Shared yfinance adapter for BQuant base datasets."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_SOURCES_CONFIG_PATH = REPO_ROOT / "configs" / "data_sources.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML config file into a dictionary."""
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def source_config() -> dict[str, Any]:
    """Return the yfinance source configuration block."""
    return _load_yaml(DATA_SOURCES_CONFIG_PATH).get("yfinance", {})


def symbol_to_ticker(symbol: str) -> str:
    """Map a local exchange symbol to the configured Yahoo Finance ticker format."""
    suffix = str(source_config().get("parameters", {}).get("symbol_suffix", ".VN"))
    symbol = str(symbol).upper()
    return symbol if symbol.endswith(suffix) else f"{symbol}{suffix}"


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten multi-index Yahoo columns into a simple OHLCV column set."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [column[0] if isinstance(column, tuple) else column for column in df.columns]
    return df


def _download_with_retry(**kwargs: Any) -> pd.DataFrame:
    """Call `yf.download` with config-driven retry and backoff behavior."""
    cfg = source_config()
    retries = int(cfg.get("rate_limit", {}).get("retry_attempts", 3))
    delay = int(cfg.get("rate_limit", {}).get("retry_delay_seconds", 3))

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            df = yf.download(progress=False, threads=False, **kwargs)
            return _flatten_columns(df)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(delay)
    if last_error is not None:
        raise last_error
    return pd.DataFrame()


def _repair_ohlc_bounds(frame: pd.DataFrame) -> pd.DataFrame:
    """Repair high/low bounds so each row remains internally consistent."""
    output = frame.copy()
    output["high"] = output[["open", "high", "low", "close"]].max(axis=1)
    output["low"] = output[["open", "high", "low", "close"]].min(axis=1)
    return output


def fetch_daily_history(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch standardized daily OHLCV history for one symbol from yfinance."""
    ticker = symbol_to_ticker(symbol)
    params = source_config().get("parameters", {})
    end_exclusive = (datetime.fromisoformat(end_date).date() + timedelta(days=1)).isoformat()
    df = _download_with_retry(
        tickers=ticker,
        start=start_date,
        end=end_exclusive,
        interval=str(params.get("daily_interval", "1d")),
        auto_adjust=bool(params.get("auto_adjust", False)),
        repair=bool(params.get("repair", True)),
    )
    if df.empty:
        return pd.DataFrame(
            columns=[
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
        )

    frame = df.reset_index().rename(columns={"Date": "trading_date", "Datetime": "trading_date"})
    frame["trading_date"] = pd.to_datetime(frame["trading_date"], errors="coerce").dt.date
    frame["symbol"] = str(symbol).upper()
    frame["source"] = "yfinance"

    output = frame[
        ["symbol", "trading_date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "source"]
    ].rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Adj Close": "adjusted_close",
            "Volume": "volume",
        }
    )
    for column in ["open", "high", "low", "close", "adjusted_close", "volume"]:
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output = output.dropna(subset=["trading_date", "open", "high", "low", "close", "volume"])
    output = _repair_ohlc_bounds(output)
    output = output.sort_values(["symbol", "trading_date"]).drop_duplicates(["symbol", "trading_date"], keep="last")
    return output.reset_index(drop=True)


def fetch_intraday_history(symbol: str, start_date: str, end_date: str, interval: str = "15m") -> pd.DataFrame:
    """Fetch standardized intraday OHLCV history for one symbol from yfinance."""
    ticker = symbol_to_ticker(symbol)
    params = source_config().get("parameters", {})
    lookback_days = int(params.get("intraday_window_days", 60))
    start_dt = pd.to_datetime(start_date, errors="coerce")
    end_dt = pd.to_datetime(end_date, errors="coerce")

    # Yahoo intraday history is constrained by rolling lookback windows. Prefer the
    # native `period` form when the request spans the configured full window.
    download_kwargs: dict[str, Any] = {
        "tickers": ticker,
        "interval": interval or str(params.get("intraday_interval", "15m")),
        "auto_adjust": bool(params.get("auto_adjust", False)),
        "repair": bool(params.get("repair", True)),
    }
    if pd.notna(start_dt) and pd.notna(end_dt):
        if start_dt <= end_dt and (end_dt - start_dt).days >= lookback_days - 2:
            download_kwargs["period"] = f"{lookback_days}d"
        else:
            download_kwargs["start"] = start_dt.strftime("%Y-%m-%d")
            download_kwargs["end"] = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        download_kwargs["period"] = f"{lookback_days}d"

    df = _download_with_retry(
        **download_kwargs,
    )
    if df.empty:
        return pd.DataFrame(
            columns=[
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
        )

    frame = df.reset_index().rename(columns={"Datetime": "bar_time", "Date": "bar_time"})
    bar_time = pd.to_datetime(frame["bar_time"], errors="coerce")
    if getattr(bar_time.dt, "tz", None) is not None:
        # Normalize all downstream intraday timestamps to local naive exchange time.
        bar_time = bar_time.dt.tz_convert("Asia/Ho_Chi_Minh").dt.tz_localize(None)
    frame["bar_time"] = bar_time
    frame["session_date"] = frame["bar_time"].dt.date
    frame["symbol"] = str(symbol).upper()
    frame["interval"] = interval or str(params.get("intraday_interval", "15m"))
    frame["source"] = "yfinance"

    output = frame[
        ["symbol", "bar_time", "session_date", "Open", "High", "Low", "Close", "Volume", "interval", "source"]
    ].rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    for column in ["open", "high", "low", "close", "volume"]:
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output = output.dropna(subset=["bar_time", "open", "high", "low", "close", "volume"])
    output = _repair_ohlc_bounds(output)
    output = output.sort_values(["symbol", "bar_time"]).drop_duplicates(["symbol", "bar_time"], keep="last")
    return output.reset_index(drop=True)
