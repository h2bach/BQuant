"""QUBO construction for BQuant portfolio selection.

The optimizer solves binary asset selection. Weight sizing remains a
deterministic post-processing step because continuous weights would expand the
QUBO beyond a practical local simulator footprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class QuboModel:
    """Binary QUBO model for one portfolio selection date.

    Attributes:
        symbols: Candidate symbols in bit order.
        linear: Linear coefficient for each asset bit.
        quadratic: Upper-triangular quadratic coefficients keyed by `(i, j)`.
        constant: Constant objective term.
        max_positions: Maximum selected positions allowed after validation.
        metadata: JSON-safe model metadata for audit and prompt context.
    """

    symbols: list[str]
    linear: np.ndarray
    quadratic: dict[tuple[int, int], float]
    constant: float
    max_positions: int
    metadata: dict[str, Any]

    def energy(self, bits: np.ndarray | list[int]) -> float:
        """Evaluate minimization energy for one bit vector.

        Args:
            bits: Binary vector in the same order as `symbols`.

        Returns:
            Scalar QUBO energy. Lower is better.
        """
        vector = np.asarray(bits, dtype=float)
        value = float(self.constant + np.dot(self.linear, vector))
        for (i, j), coeff in self.quadratic.items():
            value += float(coeff) * float(vector[i]) * float(vector[j])
        return value

    def symmetric_matrix(self) -> np.ndarray:
        """Return a symmetric matrix representation for diagnostics/tests.

        Returns:
            Matrix `Q` where `x.T @ Q @ x + constant` equals `energy(x)`.
        """
        size = len(self.symbols)
        matrix = np.zeros((size, size), dtype=float)
        np.fill_diagonal(matrix, self.linear)
        for (i, j), coeff in self.quadratic.items():
            matrix[i, j] += coeff / 2.0
            matrix[j, i] += coeff / 2.0
        return matrix


def build_portfolio_qubo(
    candidates: list[dict[str, Any]],
    covariance: np.ndarray,
    *,
    current_weights: dict[str, float] | None = None,
    max_positions: int,
    risk_aversion: float,
    turnover_penalty: float,
    concentration_penalty: float,
    bad_quality_penalty: float,
) -> QuboModel:
    """Build a minimization QUBO for portfolio candidate selection.

    Args:
        candidates: Ordered candidate dictionaries from the TA signal layer.
        covariance: Candidate return covariance matrix.
        current_weights: Current portfolio weights by symbol.
        max_positions: Target/maximum selected position count.
        risk_aversion: Penalty multiplier for covariance risk.
        turnover_penalty: Linear penalty for opening new names and reward for
            keeping existing names.
        concentration_penalty: Quadratic position-count penalty.
        bad_quality_penalty: Extra linear penalty for high-risk candidates.

    Returns:
        QUBO model ready for QAOA or classical fallback solving.
    """
    if not candidates:
        raise ValueError("QUBO requires at least one portfolio candidate")
    symbols = [str(row["symbol"]).upper() for row in candidates]
    size = len(symbols)
    cov = np.asarray(covariance, dtype=float)
    if cov.shape != (size, size):
        cov = np.eye(size, dtype=float) * 0.02
    weights = current_weights or {}
    target_positions = min(int(max_positions), size)

    linear = np.zeros(size, dtype=float)
    quadratic: dict[tuple[int, int], float] = {}
    alpha_terms: dict[str, float] = {}
    risk_terms: dict[str, float] = {}

    for i, candidate in enumerate(candidates):
        symbol = symbols[i]
        expected_alpha = float(candidate.get("expected_alpha", 0.0) or 0.0)
        alpha_terms[symbol] = expected_alpha
        linear[i] += -expected_alpha
        linear[i] += float(risk_aversion) * max(float(cov[i, i]), 0.0)
        risk_terms[symbol] = float(cov[i, i])
        linear[i] += -float(turnover_penalty) if weights.get(symbol, 0.0) > 0 else float(turnover_penalty)
        if str(candidate.get("ta_risk_flag", "")).lower() == "high":
            linear[i] += float(bad_quality_penalty)

    for i in range(size):
        for j in range(i + 1, size):
            coeff = float(risk_aversion) * float(cov[i, j])
            quadratic[(i, j)] = quadratic.get((i, j), 0.0) + coeff

    # Encourage a full but capped basket. The feasibility check still enforces
    # max_positions, while this term helps QAOA avoid trivial all-cash solutions.
    penalty = float(concentration_penalty)
    for i in range(size):
        linear[i] += penalty * (1.0 - 2.0 * target_positions)
        for j in range(i + 1, size):
            quadratic[(i, j)] = quadratic.get((i, j), 0.0) + 2.0 * penalty
    constant = penalty * target_positions * target_positions

    return QuboModel(
        symbols=symbols,
        linear=linear,
        quadratic=quadratic,
        constant=float(constant),
        max_positions=target_positions,
        metadata={
            "target_positions": target_positions,
            "alpha_terms": alpha_terms,
            "risk_terms": risk_terms,
            "risk_aversion": risk_aversion,
            "turnover_penalty": turnover_penalty,
            "concentration_penalty": concentration_penalty,
        },
    )
