"""Validate the vnstock:VCI 15-minute intraday rebuild outputs."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warehouse.duckdb_connection import get_connection


def main() -> None:
    """Print and enforce core intraday table/manifest checks."""
    with get_connection(read_only=True) as conn:
        summary = conn.execute(
            """
            SELECT
                count(*) AS rows,
                count(DISTINCT symbol) AS symbols,
                min(bar_time) AS min_time,
                max(bar_time) AS max_time,
                string_agg(DISTINCT source, ',') AS sources,
                sum(CASE WHEN open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR volume < 0 THEN 1 ELSE 0 END) AS bad_rows,
                sum(CASE WHEN high < greatest(open, low, close) OR low > least(open, high, close) THEN 1 ELSE 0 END) AS bad_bounds
            FROM intraday_ohlcv_15m_base
            """
        ).fetchone()
        per_symbol = conn.execute(
            """
            SELECT symbol, count(*) AS rows, min(bar_time) AS min_time, max(bar_time) AS max_time
            FROM intraday_ohlcv_15m_base
            GROUP BY symbol
            ORDER BY symbol
            """
        ).fetchall()
        manifest = conn.execute(
            """
            SELECT count(*) AS files, coalesce(sum(row_count), 0) AS rows
            FROM data_file_manifest
            WHERE dataset_name = 'intraday_ohlcv_15m_60d'
            """
        ).fetchone()
        delta_rows = conn.execute("SELECT count(*) FROM intraday_ohlcv_15m_delta").fetchone()[0]

    print("intraday_ohlcv_15m_base:", summary)
    print("intraday manifest:", manifest)
    print("intraday delta rows:", delta_rows)
    print("per-symbol coverage:")
    for row in per_symbol:
        print("  ", row)

    rows, symbols, min_time, max_time, sources, bad_rows, bad_bounds = summary
    files, manifest_rows = manifest
    if rows <= 0:
        raise SystemExit("Expected intraday base rows after rebuild")
    if symbols != 30:
        raise SystemExit(f"Expected 30 VN30 symbols in intraday base, got {symbols}")
    if sources != "vnstock:vci":
        raise SystemExit(f"Unexpected intraday source set: {sources}")
    if bad_rows or bad_bounds:
        raise SystemExit(f"Invalid intraday OHLCV rows: bad_rows={bad_rows}, bad_bounds={bad_bounds}")
    if files != 30 or int(manifest_rows) != int(rows):
        raise SystemExit(f"Unexpected intraday manifest coverage: files={files}, rows={manifest_rows}, table_rows={rows}")
    if min_time is None or max_time is None:
        raise SystemExit("Intraday coverage bounds are missing")


if __name__ == "__main__":
    main()
