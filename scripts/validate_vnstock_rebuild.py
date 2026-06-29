"""Validate the vnstock:VCI daily rebuild outputs.

This script is intentionally small and dependency-light because it is called at
the end of the tmux rebuild command. A non-zero exit tells the operator to check
the preceding pipeline logs before restarting the web app.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warehouse.duckdb_connection import get_connection


def _fetchone(conn, query: str, params: list[object] | None = None) -> tuple:
    """Run a scalar-style validation query and return its first row."""
    return conn.execute(query, params or []).fetchone()


def _fetchall(conn, query: str, params: list[object] | None = None) -> list[tuple]:
    """Run a validation query and return all rows."""
    return conn.execute(query, params or []).fetchall()


def main() -> None:
    """Print core rebuild checks and raise if the canonical data is invalid."""
    with get_connection(read_only=True) as conn:
        daily_summary = _fetchone(
            conn,
            """
            SELECT
                count(*) AS rows,
                count(DISTINCT symbol) AS symbols,
                min(trading_date) AS min_date,
                max(trading_date) AS max_date,
                string_agg(DISTINCT source, ',') AS sources
            FROM daily_ohlcv_base
            """,
        )
        index_summary = _fetchall(
            conn,
            """
            WITH latest AS (
                SELECT
                    symbol,
                    trading_date,
                    close,
                    row_number() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                FROM market_index_daily_base
            )
            SELECT
                b.symbol,
                count(*) AS rows,
                min(b.trading_date) AS min_date,
                max(b.trading_date) AS max_date,
                max(CASE WHEN l.rn = 1 THEN l.close END) AS latest_close,
                string_agg(DISTINCT b.source, ',') AS sources
            FROM market_index_daily_base b
            LEFT JOIN latest l
                ON b.symbol = l.symbol
               AND b.trading_date = l.trading_date
            GROUP BY b.symbol
            ORDER BY b.symbol
            """,
        )
        bad_daily_sources = _fetchone(
            conn,
            "SELECT count(*) FROM daily_ohlcv_base WHERE source <> 'vnstock:vci'",
        )[0]
        bad_index_sources = _fetchone(
            conn,
            "SELECT count(*) FROM market_index_daily_base WHERE source <> 'vnstock:vci'",
        )[0]
        index_symbols = {
            row[0]
            for row in _fetchall(
                conn,
                "SELECT DISTINCT symbol FROM market_index_daily_base ORDER BY symbol",
            )
        }
        intraday_base_rows = _fetchone(conn, "SELECT count(*) FROM intraday_ohlcv_15m_base")[0]
        intraday_delta_rows = _fetchone(conn, "SELECT count(*) FROM intraday_ohlcv_15m_delta")[0]
        manifest_summary = _fetchall(
            conn,
            """
            SELECT dataset_name, count(*) AS files, coalesce(sum(row_count), 0) AS rows
            FROM data_file_manifest
            GROUP BY dataset_name
            ORDER BY dataset_name
            """,
        )

    print("daily_ohlcv_base:", daily_summary)
    print("market_index_daily_base:")
    for row in index_summary:
        print("  ", row)
    print("bad_daily_sources:", bad_daily_sources)
    print("bad_index_sources:", bad_index_sources)
    print("intraday rows:", {"base": intraday_base_rows, "delta": intraday_delta_rows})
    print("manifest:")
    for row in manifest_summary:
        print("  ", row)

    daily_rows, daily_symbols, _, _, daily_sources = daily_summary
    if daily_rows <= 0 or daily_symbols != 30:
        raise SystemExit(f"Expected 30 daily VN30 symbols with rows, got symbols={daily_symbols}, rows={daily_rows}")
    if daily_sources != "vnstock:vci" or bad_daily_sources:
        raise SystemExit(f"Unexpected daily sources: sources={daily_sources}, bad_rows={bad_daily_sources}")
    if index_symbols != {"VN30", "VNINDEX"}:
        raise SystemExit(f"Expected VN30/VNINDEX market index rows, got {sorted(index_symbols)}")
    if bad_index_sources:
        raise SystemExit(f"Unexpected market index source rows: {bad_index_sources}")
    latest_index_values = {row[0]: float(row[4]) for row in index_summary if row[4] is not None}
    if latest_index_values.get("VNINDEX", 0.0) < 1000.0:
        raise SystemExit(f"VNINDEX latest close does not look like raw index points: {latest_index_values}")
    if intraday_base_rows:
        with get_connection(read_only=True) as conn:
            bad_intraday_sources = _fetchone(
                conn,
                "SELECT count(*) FROM intraday_ohlcv_15m_base WHERE source <> 'vnstock:vci'",
            )[0]
        if bad_intraday_sources:
            raise SystemExit(f"Unexpected intraday source rows: {bad_intraday_sources}")


if __name__ == "__main__":
    main()
