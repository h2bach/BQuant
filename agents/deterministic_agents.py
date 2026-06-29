"""Deterministic recommendation agents for the BQuant v1 foundation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


AGENT_VERSION = "deterministic-v1"


@dataclass(frozen=True)
class AgentDecision:
    """Recommendation produced for one symbol.

    Attributes:
        symbol: Stock ticker.
        recommendation: Action bucket for downstream UI/agent consumption.
        score: Normalized score in `[0, 1]`.
        confidence: Normalized confidence in `[0, 1]`.
        risk_level: `low`, `medium`, or `high`.
        suggested_weight: Portfolio weight after optimizer overlay.
        rationale: JSON-serializable explanation payload.
    """

    symbol: str
    recommendation: str
    score: float
    confidence: float
    risk_level: str
    suggested_weight: float
    rationale: dict[str, Any]


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a nullable scalar to a finite float.

    Args:
        value: Scalar from a pandas row.
        default: Fallback for null, NaN, or non-finite values.

    Returns:
        Finite float.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    """Clamp a score to a bounded interval."""
    return max(lower, min(upper, value))


def _quality_blocks(row: pd.Series, config: dict[str, Any]) -> tuple[bool, list[str]]:
    """Evaluate whether data quality should block a symbol.

    Args:
        row: One row from `mart_agent_context_daily`.
        config: Agent runtime configuration.

    Returns:
        Tuple of block flag and human-readable reasons.
    """
    thresholds = config.get("thresholds", {})
    max_stale_days = int(thresholds.get("max_stale_days", 3))
    quality_status = str(row.get("data_quality_status", "missing"))
    stale_days = _safe_float(row.get("stale_days"), default=999.0)
    reasons: list[str] = []
    if quality_status != "pass":
        reasons.append(f"data_quality_status={quality_status}")
    if stale_days > max_stale_days:
        reasons.append(f"stale_days={stale_days:g}>{max_stale_days}")
    return bool(reasons), reasons


def _market_score(row: pd.Series, config: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score the market regime contribution for one context row."""
    agent_cfg = config.get("agents", {}).get("market_regime_agent", {})
    regime = str(row.get("market_regime", "neutral"))
    volatility_regime = str(row.get("volatility_regime", "normal_volatility"))
    raw_regime_score = _safe_float(row.get("regime_score"), default=0.5)
    contribution = 0.0
    if regime == "bullish":
        contribution += _safe_float(agent_cfg.get("bullish_bonus"), 0.15)
    elif regime == "bearish":
        contribution -= _safe_float(agent_cfg.get("bearish_penalty"), 0.25)
    if volatility_regime == "high_volatility":
        contribution -= _safe_float(agent_cfg.get("high_volatility_penalty"), 0.10)
    contribution += (raw_regime_score - 0.5) * 0.20
    return contribution, {
        "market_regime": regime,
        "volatility_regime": volatility_regime,
        "regime_score": raw_regime_score,
        "contribution": round(contribution, 4),
    }


def _symbol_score(row: pd.Series, config: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score symbol-level trend, momentum, volatility, and liquidity features."""
    weights = config.get("agents", {}).get("symbol_signal_agent", {}).get("weights", {})
    trend_state = str(row.get("trend_state", "neutral"))
    trend_value = {"uptrend": 1.0, "sideways": 0.5, "downtrend": 0.0}.get(trend_state, 0.5)
    relative_strength = _clamp(0.5 + _safe_float(row.get("relative_strength_20d")) * 2.0)
    momentum = _clamp(0.5 + _safe_float(row.get("return_20d")) * 3.0)
    volatility = _clamp(1.0 - _safe_float(row.get("volatility_20d")) * 3.0)
    liquidity = _clamp(_safe_float(row.get("traded_value_cross_section_percentile")) / 100.0)

    component_scores = {
        "trend": trend_value,
        "relative_strength": relative_strength,
        "momentum": momentum,
        "volatility": volatility,
        "liquidity": liquidity,
    }
    contribution = 0.0
    weight_total = 0.0
    for key, value in component_scores.items():
        weight = _safe_float(weights.get(key), default=0.0)
        contribution += weight * (value - 0.5)
        weight_total += weight
    if weight_total <= 0:
        contribution = 0.0
    return contribution, {
        "trend_state": trend_state,
        "component_scores": {key: round(value, 4) for key, value in component_scores.items()},
        "contribution": round(contribution, 4),
    }


def _risk_level(row: pd.Series) -> str:
    """Classify risk level from volatility regime and drawdown."""
    volatility_regime = str(row.get("volatility_regime", "normal_volatility"))
    drawdown = abs(_safe_float(row.get("drawdown_from_peak")))
    volatility_20d = _safe_float(row.get("volatility_20d"))
    if volatility_regime == "high_volatility" or drawdown > 0.18 or volatility_20d > 0.06:
        return "high"
    if drawdown > 0.08 or volatility_20d > 0.035:
        return "medium"
    return "low"


def score_context_row(row: pd.Series, config: dict[str, Any]) -> AgentDecision:
    """Score one symbol context row into an agent recommendation.

    Args:
        row: One pandas row from the dbt agent context mart.
        config: Agent runtime configuration.

    Returns:
        Agent decision with score, confidence, risk, and rationale.
    """
    symbol = str(row["symbol"]).upper()
    thresholds = config.get("thresholds", {})
    candidate_score = _safe_float(thresholds.get("candidate_score"), default=0.60)
    risk_off_score = _safe_float(thresholds.get("risk_off_score"), default=0.25)
    min_confidence = _safe_float(thresholds.get("min_confidence"), default=0.35)

    blocked, block_reasons = _quality_blocks(row, config)
    market_contribution, market_rationale = _market_score(row, config)
    symbol_contribution, symbol_rationale = _symbol_score(row, config)
    risk_level = _risk_level(row)

    score = _clamp(0.50 + market_contribution + symbol_contribution)
    confidence = _clamp(min_confidence + abs(score - 0.50) + (0.15 if not blocked else -0.10))
    if risk_level == "high":
        confidence = _clamp(confidence - 0.10)

    market_regime = str(row.get("market_regime", "neutral"))
    if blocked:
        recommendation = "blocked_data_quality"
        score = min(score, 0.20)
        confidence = _clamp(confidence)
    elif market_regime == "bearish" or score <= risk_off_score:
        recommendation = "avoid_or_reduce"
    elif score >= candidate_score:
        recommendation = "candidate_long"
    else:
        recommendation = "watch"

    rationale = {
        "agent_version": AGENT_VERSION,
        "quality": {
            "blocked": blocked,
            "reasons": block_reasons,
            "data_quality_status": str(row.get("data_quality_status", "")),
            "stale_days": _safe_float(row.get("stale_days"), default=0.0),
        },
        "market": market_rationale,
        "symbol": symbol_rationale,
        "risk": {
            "risk_level": risk_level,
            "drawdown_from_peak": _safe_float(row.get("drawdown_from_peak")),
            "volatility_20d": _safe_float(row.get("volatility_20d")),
        },
    }
    return AgentDecision(
        symbol=symbol,
        recommendation=recommendation,
        score=round(score, 6),
        confidence=round(confidence, 6),
        risk_level=risk_level,
        suggested_weight=0.0,
        rationale=rationale,
    )


def apply_portfolio_overlay(decisions: list[AgentDecision], config: dict[str, Any]) -> list[AgentDecision]:
    """Assign classical fallback portfolio weights to candidate decisions.

    Args:
        decisions: Symbol decisions before portfolio weights.
        config: Agent runtime configuration.

    Returns:
        Decisions with `suggested_weight` filled for candidate long symbols.
    """
    optimizer_cfg = config.get("agents", {}).get("portfolio_optimizer_agent", {})
    max_positions = int(optimizer_cfg.get("max_positions", 10))
    max_weight = _safe_float(optimizer_cfg.get("max_weight"), default=0.15)
    min_weight = _safe_float(optimizer_cfg.get("min_weight"), default=0.02)

    candidates = [
        decision
        for decision in decisions
        if decision.recommendation == "candidate_long" and decision.confidence > 0
    ]
    candidates = sorted(candidates, key=lambda item: (item.score, item.confidence), reverse=True)[:max_positions]
    score_total = sum(max(decision.score, 0.0) for decision in candidates)
    weight_by_symbol: dict[str, float] = {}
    if score_total > 0:
        raw_weights = {
            decision.symbol: max(min_weight, min(max_weight, decision.score / score_total))
            for decision in candidates
        }
        clipped_total = sum(raw_weights.values())
        if clipped_total > 0:
            weight_by_symbol = {
                symbol: round(weight / clipped_total, 6)
                for symbol, weight in raw_weights.items()
            }

    weighted: list[AgentDecision] = []
    for decision in decisions:
        weight = weight_by_symbol.get(decision.symbol, 0.0)
        rationale = dict(decision.rationale)
        rationale["portfolio_overlay"] = {
            "mode": str(optimizer_cfg.get("mode", "classical_fallback")),
            "qaoa_enabled": bool(optimizer_cfg.get("qaoa_enabled", False)),
            "suggested_weight": weight,
        }
        weighted.append(
            AgentDecision(
                symbol=decision.symbol,
                recommendation=decision.recommendation,
                score=decision.score,
                confidence=decision.confidence,
                risk_level=decision.risk_level,
                suggested_weight=weight,
                rationale=rationale,
            )
        )
    return weighted


def decisions_to_frame(decisions: list[AgentDecision], *, run_id: str, as_of_date: Any) -> pd.DataFrame:
    """Convert agent decisions to a warehouse-ready DataFrame.

    Args:
        decisions: Agent decisions after portfolio overlay.
        run_id: Current agent cycle id.
        as_of_date: Trading date represented by the input context.

    Returns:
        DataFrame matching `agent_recommendations` insert columns.
    """
    return pd.DataFrame(
        [
            {
                "run_id": run_id,
                "as_of_date": pd.Timestamp(as_of_date).date(),
                "symbol": decision.symbol,
                "recommendation": decision.recommendation,
                "confidence": decision.confidence,
                "score": decision.score,
                "risk_level": decision.risk_level,
                "suggested_weight": decision.suggested_weight,
                "rationale_json": json.dumps(decision.rationale, sort_keys=True),
            }
            for decision in decisions
        ]
    )
