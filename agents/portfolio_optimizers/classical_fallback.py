"""Classical fallback solver for small BQuant QUBO models."""

from __future__ import annotations

from itertools import product
from typing import Any

import numpy as np

from agents.portfolio_optimizers.qubo_builder import QuboModel


def solve_qubo_bruteforce(model: QuboModel) -> dict[str, Any]:
    """Solve a small QUBO exactly by exhaustive enumeration.

    Args:
        model: Binary QUBO model. Intended for candidate pools up to 12 assets.

    Returns:
        Solution dictionary with selected symbols, energy, and bit vector.
    """
    size = len(model.symbols)
    best_bits: np.ndarray | None = None
    best_energy = float("inf")
    evaluated = 0
    feasible = 0

    for raw_bits in product([0, 1], repeat=size):
        bits = np.asarray(raw_bits, dtype=int)
        selected_count = int(bits.sum())
        evaluated += 1
        if selected_count <= 0 or selected_count > model.max_positions:
            continue
        feasible += 1
        energy = model.energy(bits)
        if energy < best_energy:
            best_energy = energy
            best_bits = bits

    if best_bits is None:
        best_bits = np.zeros(size, dtype=int)
        best_energy = model.energy(best_bits)

    return {
        "backend": "classical_bruteforce",
        "fallback_used": True,
        "bits": best_bits.astype(int).tolist(),
        "selected_symbols": [
            symbol for symbol, bit in zip(model.symbols, best_bits, strict=False) if int(bit) == 1
        ],
        "energy": float(best_energy),
        "evaluated_states": evaluated,
        "feasible_states": feasible,
        "probability": None,
        "optimizer_result": {"method": "exhaustive_enumeration"},
    }
