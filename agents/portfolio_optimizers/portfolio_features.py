"""Feature loading for BQuant QAOA portfolio optimization."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from warehouse.duckdb_connection import get_connection


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "qaoa_optimizer.yaml"


def load_qaoa_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load QAOA optimizer configuration.

    Args:
        path: YAML config path.

    Returns:
        Configuration mapping with conservative defaults.
    """
    config: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    defaults = {
        "candidate_pool_size": 12,
        "max_positions": 8,
        "max_qiskit_qubits": 8,
        "risk_aversion": 0.65,
        "turnover_penalty": 0.10,
        "concentration_penalty": 0.15,
        "bad_quality_penalty": 0.40,
        "min_liquidity_percentile": 0.25,
        "max_symbol_weight": 0.15,
        "min_cash_weight": 0.05,
        "lookback_days": 252,
        "random_seed": 42,
    }
    return {**defaults, **config}


def load_qaoa_candidates(as_of_date: date | str, config: dict[str, Any]) -> pd.DataFrame:
    """Load ranked QAOA candidate rows from the agent context mart.

    Args:
        as_of_date: Trading date ceiling used for the optimizer.
        config: QAOA configuration mapping.

    Returns:
        Candidate frame ordered by TA composite score and liquidity.
    """
    target_date = pd.Timestamp(as_of_date).date()
    pool_size = int(config.get("candidate_pool_size", 12))
    min_liquidity = float(config.get("min_liquidity_percentile", 0.25))
    with get_connection(read_only=True) as conn:
        frame = conn.execute(
            """
            SELECT
                symbol,
                trading_date,
                close,
                return_1d,
                return_5d,
                return_20d,
                return_60d,
                volatility_20d,
                volatility_60d,
                drawdown_from_peak,
                relative_strength_20d,
                relative_strength_60d,
                traded_value_cross_section_percentile,
                trend_state,
                liquidity_state,
                market_regime,
                volatility_regime,
                data_quality_status,
                ta_trend_score,
                ta_momentum_score,
                ta_volatility_score,
                ta_liquidity_score,
                ta_relative_strength_score,
                ta_composite_score,
                ta_action_bias,
                ta_risk_flag,
                rsi_14,
                macd_histogram,
                adx_14,
                atr_pct_14,
                beta_vs_vn30_60d
            FROM analytics_marts.mart_agent_context_daily
            WHERE trading_date = ?
              AND data_quality_status = 'pass'
              AND coalesce(traded_value_cross_section_percentile, 0) >= ?
              AND ta_composite_score IS NOT NULL
            ORDER BY ta_composite_score DESC,
                     traded_value_cross_section_percentile DESC,
                     relative_strength_20d DESC NULLS LAST,
                     symbol
            LIMIT ?
            """,
            [target_date, min_liquidity, pool_size],
        ).df()
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame["expected_alpha"] = frame.apply(_expected_alpha, axis=1)
    return frame


def _expected_alpha(row: pd.Series) -> float:
    """Estimate expected alpha from TA composite, momentum, and risk fields."""
    composite = float(row.get("ta_composite_score") or 0.5)
    return_20d = float(row.get("return_20d") or 0.0)
    rs20 = float(row.get("relative_strength_20d") or 0.0)
    risk_penalty = 0.025 if str(row.get("ta_risk_flag")).lower() == "high" else 0.0
    alpha = (composite - 0.5) * 0.08 + return_20d * 0.20 + rs20 * 0.20 - risk_penalty
    return float(max(min(alpha, 0.08), -0.08))


def load_return_covariance(symbols: list[str], as_of_date: date | str, lookback_days: int) -> np.ndarray:
    """Load annualized covariance matrix for candidate daily returns.

    Args:
        symbols: Candidate ticker list.
        as_of_date: Latest allowed trading date.
        lookback_days: Rolling window length.

    Returns:
        Annualized covariance matrix with diagonal fallback when data is sparse.
    """
    if not symbols:
        return np.zeros((0, 0), dtype=float)
    target_date = pd.Timestamp(as_of_date).date()
    placeholders = ", ".join(["?"] * len(symbols))
    with get_connection(read_only=True) as conn:
        frame = conn.execute(
            f"""
            WITH returns AS (
                SELECT
                    symbol,
                    trading_date,
                    ln(close / nullif(lag(close) OVER (PARTITION BY symbol ORDER BY trading_date), 0)) AS log_return
                FROM daily_ohlcv_base
                WHERE symbol IN ({placeholders})
                  AND trading_date <= ?
            ),
            ranked AS (
                SELECT *,
                       row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                FROM returns
            )
            SELECT symbol, trading_date, log_return
            FROM ranked
            WHERE rn <= ?
            """,
            [*symbols, target_date, int(lookback_days)],
        ).df()
    if frame.empty:
        return np.eye(len(symbols), dtype=float) * 0.04
    pivot = frame.pivot_table(index="trading_date", columns="symbol", values="log_return").reindex(columns=symbols)
    cov = pivot.cov(min_periods=20).fillna(0.0).to_numpy(dtype=float) * 252.0
    if cov.shape != (len(symbols), len(symbols)):
        return np.eye(len(symbols), dtype=float) * 0.04
    diag = np.diag(cov)
    fallback_var = float(np.nanmedian(diag[diag > 0])) if np.any(diag > 0) else 0.04
    cov = np.where(np.isfinite(cov), cov, 0.0)
    for i in range(len(symbols)):
        if cov[i, i] <= 0:
            cov[i, i] = fallback_var
    return cov


def candidate_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a candidate DataFrame into JSON-safe records."""
    records: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        item: dict[str, Any] = {}
        for key, value in row.to_dict().items():
            if isinstance(value, (pd.Timestamp, date)):
                item[key] = pd.Timestamp(value).date().isoformat()
            elif pd.isna(value):
                item[key] = None
            elif hasattr(value, "item"):
                item[key] = value.item()
            else:
                item[key] = value
        records.append(item)
    return records
