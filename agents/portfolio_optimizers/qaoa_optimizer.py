"""QAOA portfolio optimizer signal for BQuant LLM/backtest workflows."""

from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from agents.portfolio_optimizers.classical_fallback import solve_qubo_bruteforce
from agents.portfolio_optimizers.portfolio_features import (
    candidate_records,
    load_qaoa_candidates,
    load_qaoa_config,
    load_return_covariance,
)
from agents.portfolio_optimizers.qubo_builder import QuboModel, build_portfolio_qubo
from warehouse.duckdb_connection import get_connection


def ensure_qaoa_tables() -> None:
    """Create QAOA optimizer audit tables if schema bootstrap has not run."""
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qaoa_optimizer_runs (
                run_id VARCHAR PRIMARY KEY,
                as_of_date DATE NOT NULL,
                status VARCHAR NOT NULL,
                backend VARCHAR NOT NULL,
                fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                selected_count INTEGER NOT NULL DEFAULT 0,
                energy DOUBLE,
                config_json VARCHAR NOT NULL,
                error_message VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qaoa_optimizer_candidates (
                run_id VARCHAR NOT NULL,
                as_of_date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                rank INTEGER NOT NULL,
                expected_alpha DOUBLE,
                ta_composite_score DOUBLE,
                ta_risk_flag VARCHAR,
                liquidity_percentile DOUBLE,
                volatility_20d DOUBLE,
                selected BOOLEAN NOT NULL DEFAULT FALSE,
                raw_json VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (run_id, symbol)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qaoa_optimizer_solution_assets (
                run_id VARCHAR NOT NULL,
                as_of_date DATE NOT NULL,
                symbol VARCHAR NOT NULL,
                selected BOOLEAN NOT NULL,
                proposed_weight DOUBLE NOT NULL DEFAULT 0,
                bit_value INTEGER NOT NULL DEFAULT 0,
                energy_contribution DOUBLE,
                rank INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (run_id, symbol)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS qaoa_optimizer_solution_summary (
                run_id VARCHAR PRIMARY KEY,
                as_of_date DATE NOT NULL,
                selected_symbols_json VARCHAR NOT NULL,
                proposed_weights_json VARCHAR NOT NULL,
                qubo_metadata_json VARCHAR NOT NULL,
                optimizer_result_json VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def run_qaoa_optimizer(
    *,
    as_of_date: date | str,
    current_weights: dict[str, float] | None = None,
    config: dict[str, Any] | None = None,
    run_id: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run QAOA portfolio selection and return an LLM-ready signal.

    Args:
        as_of_date: Signal date. All inputs are loaded at exactly this date.
        current_weights: Current simulated portfolio weights by symbol.
        config: Optional QAOA configuration mapping.
        run_id: Optional externally supplied run id.
        persist: Whether to write audit rows to DuckDB.

    Returns:
        JSON-safe QAOA signal with selected symbols and proposed weights.
    """
    cfg = config or load_qaoa_config()
    target_date = pd.Timestamp(as_of_date).date()
    resolved_run_id = run_id or str(uuid.uuid4())
    ensure_qaoa_tables()
    candidates_frame = load_qaoa_candidates(target_date, cfg)
    candidates = candidate_records(candidates_frame)
    if not candidates:
        result = {
            "run_id": resolved_run_id,
            "as_of_date": target_date.isoformat(),
            "status": "no_candidates",
            "backend": "none",
            "fallback_used": True,
            "candidate_count": 0,
            "selected_symbols": [],
            "proposed_weights": {},
            "candidates": [],
            "energy": None,
            "optimizer_result": {},
            "qubo_metadata": {},
        }
        if persist:
            _persist_qaoa_result(result, cfg)
        return result

    symbols = [row["symbol"] for row in candidates]
    covariance = load_return_covariance(symbols, target_date, int(cfg.get("lookback_days", 252)))
    model = build_portfolio_qubo(
        candidates,
        covariance,
        current_weights=current_weights or {},
        max_positions=int(cfg.get("max_positions", 8)),
        risk_aversion=float(cfg.get("risk_aversion", 0.65)),
        turnover_penalty=float(cfg.get("turnover_penalty", 0.10)),
        concentration_penalty=float(cfg.get("concentration_penalty", 0.15)),
        bad_quality_penalty=float(cfg.get("bad_quality_penalty", 0.40)),
    )

    try:
        max_qiskit_qubits = int(cfg.get("max_qiskit_qubits", 8))
        if len(candidates) > max_qiskit_qubits:
            raise RuntimeError(
                f"candidate_count={len(candidates)} exceeds max_qiskit_qubits={max_qiskit_qubits}"
            )
        solution = _solve_with_qiskit_statevector(model, cfg)
    except Exception as exc:
        solution = solve_qubo_bruteforce(model)
        solution["fallback_reason"] = f"{type(exc).__name__}: {exc}"

    proposed_weights = _proposed_weights(
        candidates=candidates,
        selected_symbols=solution["selected_symbols"],
        max_symbol_weight=float(cfg.get("max_symbol_weight", 0.15)),
        min_cash_weight=float(cfg.get("min_cash_weight", 0.05)),
    )
    result = {
        "run_id": resolved_run_id,
        "as_of_date": target_date.isoformat(),
        "status": "success",
        "backend": solution["backend"],
        "fallback_used": bool(solution.get("fallback_used", False)),
        "candidate_count": len(candidates),
        "selected_symbols": solution["selected_symbols"],
        "proposed_weights": proposed_weights,
        "candidates": candidates,
        "bits": solution["bits"],
        "energy": solution.get("energy"),
        "probability": solution.get("probability"),
        "optimizer_result": solution.get("optimizer_result", {}),
        "qubo_metadata": model.metadata,
        "fallback_reason": solution.get("fallback_reason"),
    }
    if persist:
        _persist_qaoa_result(result, cfg)
    return result


def _solve_with_qiskit_statevector(model: QuboModel, config: dict[str, Any]) -> dict[str, Any]:
    """Solve a QUBO with a p=1 Qiskit statevector QAOA circuit.

    Args:
        model: QUBO model.
        config: QAOA configuration.

    Returns:
        Solution dictionary. Raises on missing Qiskit or simulator failure.
    """
    from qiskit import QuantumCircuit
    from qiskit.circuit.library import Diagonal
    from qiskit.quantum_info import Statevector

    size = len(model.symbols)
    if size <= 0:
        raise ValueError("QAOA requires at least one qubit")
    energies, bit_matrix = _energy_table(model)
    rng = np.random.default_rng(int(config.get("random_seed", 42)))

    def expected_energy(params: np.ndarray) -> float:
        gamma = float(params[0])
        beta = float(params[1])
        phases = np.exp(-1j * gamma * energies)
        circuit = QuantumCircuit(size)
        circuit.h(range(size))
        circuit.append(Diagonal(phases.tolist()), range(size))
        for qubit in range(size):
            circuit.rx(2.0 * beta, qubit)
        state = Statevector.from_instruction(circuit)
        probabilities = np.asarray(state.probabilities(), dtype=float)
        return float(np.dot(probabilities, energies))

    initial = np.asarray([0.7 + rng.normal(0, 0.02), 0.4 + rng.normal(0, 0.02)], dtype=float)
    optimized = minimize(
        expected_energy,
        initial,
        method=str(config.get("optimizer", "COBYLA")),
        options={"maxiter": int(config.get("max_iterations", 60)), "rhobeg": 0.4},
    )
    gamma, beta = optimized.x
    phases = np.exp(-1j * float(gamma) * energies)
    circuit = QuantumCircuit(size)
    circuit.h(range(size))
    circuit.append(Diagonal(phases.tolist()), range(size))
    for qubit in range(size):
        circuit.rx(2.0 * float(beta), qubit)
    probabilities = np.asarray(Statevector.from_instruction(circuit).probabilities(), dtype=float)
    order = np.argsort(probabilities)[::-1]
    chosen_idx = None
    for idx in order:
        bits = bit_matrix[idx]
        selected_count = int(bits.sum())
        if 0 < selected_count <= model.max_positions:
            chosen_idx = int(idx)
            break
    if chosen_idx is None:
        feasible = [idx for idx, bits in enumerate(bit_matrix) if 0 < int(bits.sum()) <= model.max_positions]
        chosen_idx = min(feasible, key=lambda idx: energies[idx]) if feasible else int(np.argmin(energies))
    bits = bit_matrix[chosen_idx].astype(int)
    return {
        "backend": "qiskit_statevector",
        "fallback_used": False,
        "bits": bits.tolist(),
        "selected_symbols": [
            symbol for symbol, bit in zip(model.symbols, bits, strict=False) if int(bit) == 1
        ],
        "energy": float(energies[chosen_idx]),
        "probability": float(probabilities[chosen_idx]),
        "optimizer_result": {
            "method": str(config.get("optimizer", "COBYLA")),
            "success": bool(optimized.success),
            "message": str(optimized.message),
            "fun": float(optimized.fun),
            "nit": int(getattr(optimized, "nit", 0) or 0),
            "gamma": float(gamma),
            "beta": float(beta),
        },
    }


def _energy_table(model: QuboModel) -> tuple[np.ndarray, np.ndarray]:
    """Precompute energies for all bitstrings in Qiskit qubit order."""
    size = len(model.symbols)
    states = 2**size
    bit_matrix = np.zeros((states, size), dtype=int)
    energies = np.zeros(states, dtype=float)
    for idx in range(states):
        bits = np.asarray([(idx >> qubit) & 1 for qubit in range(size)], dtype=int)
        bit_matrix[idx] = bits
        energies[idx] = model.energy(bits)
    return energies, bit_matrix


def _proposed_weights(
    *,
    candidates: list[dict[str, Any]],
    selected_symbols: list[str],
    max_symbol_weight: float,
    min_cash_weight: float,
) -> dict[str, float]:
    """Post-process binary QAOA selection into capped portfolio weights."""
    selected = [row for row in candidates if row["symbol"] in set(selected_symbols)]
    if not selected:
        return {}
    scores: dict[str, float] = {}
    for row in selected:
        vol = float(row.get("volatility_20d") or row.get("realized_volatility_20d") or 0.02)
        score = max(float(row.get("ta_composite_score") or 0.01), 0.01) / max(vol, 0.01)
        scores[str(row["symbol"])] = score
    total = sum(scores.values())
    gross_budget = max(0.0, 1.0 - float(min_cash_weight))
    raw = {symbol: gross_budget * score / total for symbol, score in scores.items()}
    capped = {symbol: min(float(max_symbol_weight), weight) for symbol, weight in raw.items()}
    capped_total = sum(capped.values())
    if capped_total <= 0:
        return {}
    return {symbol: round(weight / capped_total * min(gross_budget, capped_total), 6) for symbol, weight in capped.items()}


def _persist_qaoa_result(result: dict[str, Any], config: dict[str, Any]) -> None:
    """Persist QAOA run, candidates, and selected assets."""
    run_id = str(result["run_id"])
    as_of_date = pd.Timestamp(result["as_of_date"]).date()
    selected_symbols = set(result.get("selected_symbols") or [])
    proposed_weights = result.get("proposed_weights") or {}
    candidates = result.get("candidates") or []
    bits = result.get("bits") or [0] * len(candidates)
    now = datetime.now()
    with get_connection(read_only=False) as conn:
        conn.execute(
            """
            INSERT INTO qaoa_optimizer_runs (
                run_id, as_of_date, status, backend, fallback_used, candidate_count,
                selected_count, energy, config_json, error_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                status = excluded.status,
                backend = excluded.backend,
                fallback_used = excluded.fallback_used,
                candidate_count = excluded.candidate_count,
                selected_count = excluded.selected_count,
                energy = excluded.energy,
                config_json = excluded.config_json,
                error_message = excluded.error_message
            """,
            [
                run_id,
                as_of_date,
                result.get("status", "unknown"),
                result.get("backend", "unknown"),
                bool(result.get("fallback_used", False)),
                int(result.get("candidate_count", 0) or 0),
                len(selected_symbols),
                result.get("energy"),
                json.dumps(config, sort_keys=True, default=str),
                result.get("fallback_reason"),
                now,
            ],
        )
        for rank, candidate in enumerate(candidates, start=1):
            symbol = str(candidate["symbol"])
            conn.execute(
                """
                INSERT INTO qaoa_optimizer_candidates (
                    run_id, as_of_date, symbol, rank, expected_alpha,
                    ta_composite_score, ta_risk_flag, liquidity_percentile,
                    volatility_20d, selected, raw_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, symbol) DO UPDATE SET
                    rank = excluded.rank,
                    expected_alpha = excluded.expected_alpha,
                    ta_composite_score = excluded.ta_composite_score,
                    ta_risk_flag = excluded.ta_risk_flag,
                    liquidity_percentile = excluded.liquidity_percentile,
                    volatility_20d = excluded.volatility_20d,
                    selected = excluded.selected,
                    raw_json = excluded.raw_json
                """,
                [
                    run_id,
                    as_of_date,
                    symbol,
                    rank,
                    candidate.get("expected_alpha"),
                    candidate.get("ta_composite_score"),
                    candidate.get("ta_risk_flag"),
                    candidate.get("traded_value_cross_section_percentile"),
                    candidate.get("volatility_20d"),
                    symbol in selected_symbols,
                    json.dumps(candidate, sort_keys=True, default=str),
                    now,
                ],
            )
            bit = int(bits[rank - 1]) if rank - 1 < len(bits) else 0
            conn.execute(
                """
                INSERT INTO qaoa_optimizer_solution_assets (
                    run_id, as_of_date, symbol, selected, proposed_weight,
                    bit_value, energy_contribution, rank, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, symbol) DO UPDATE SET
                    selected = excluded.selected,
                    proposed_weight = excluded.proposed_weight,
                    bit_value = excluded.bit_value,
                    energy_contribution = excluded.energy_contribution,
                    rank = excluded.rank
                """,
                [
                    run_id,
                    as_of_date,
                    symbol,
                    symbol in selected_symbols,
                    float(proposed_weights.get(symbol, 0.0)),
                    bit,
                    candidate.get("expected_alpha"),
                    rank,
                    now,
                ],
            )
        conn.execute(
            """
            INSERT INTO qaoa_optimizer_solution_summary (
                run_id, as_of_date, selected_symbols_json, proposed_weights_json,
                qubo_metadata_json, optimizer_result_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id) DO UPDATE SET
                selected_symbols_json = excluded.selected_symbols_json,
                proposed_weights_json = excluded.proposed_weights_json,
                qubo_metadata_json = excluded.qubo_metadata_json,
                optimizer_result_json = excluded.optimizer_result_json
            """,
            [
                run_id,
                as_of_date,
                json.dumps(result.get("selected_symbols") or [], sort_keys=True),
                json.dumps(proposed_weights, sort_keys=True),
                json.dumps(result.get("qubo_metadata") or {}, sort_keys=True, default=str),
                json.dumps(result.get("optimizer_result") or {}, sort_keys=True, default=str),
                now,
            ],
        )
