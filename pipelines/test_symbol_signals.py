"""Test script to verify symbol-level signals are included in LLM context."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warehouse.duckdb_connection import get_connection
from agents.system_analysis import build_live_analysis_sections, _compact_sections_for_llm, _compact_symbol_signals


def main():
    """Test that symbol-level signals are properly included in compacted context."""
    print("Building live analysis sections...")
    sections = build_live_analysis_sections(refresh_recommendations=False, trigger_type="manual")
    
    print("\n=== Direct test of _compact_symbol_signals ===")
    portfolio = sections.get("portfolio", {})
    signals = _compact_symbol_signals(portfolio, limit=10)
    print(f"Direct call returned {len(signals)} signals")
    if signals:
        print("First signal from direct call:")
        print(signals[0])
    
    print("\nCompacting sections for LLM...")
    compacted = _compact_sections_for_llm(sections)
    
    print("\n=== Compacted Context Structure ===")
    for key in compacted.keys():
        print(f"- {key}")
    
    print("\n=== Portfolio Summary ===")
    print(f"Status: {portfolio.get('status')}")
    print(f"Watchlist count: {len(portfolio.get('watchlist', []))}")
    print(f"Top weights count: {len(portfolio.get('top_weights', []))}")
    
    print("\nWatchlist symbols:")
    for row in portfolio.get('watchlist', []):
        print(f"  - {row.get('symbol')}")
    
    print("\nTop weights symbols:")
    for row in portfolio.get('top_weights', []):
        print(f"  - {row.get('symbol')}")
    
    print("\n=== Symbol Signals from Compacted ===")
    symbol_signals = compacted.get("symbol_signals", [])
    print(f"Number of symbols with signals: {len(symbol_signals)}")
    
    if symbol_signals:
        print("\nFirst symbol signal example from compacted:")
        print(symbol_signals[0])
    else:
        print("No symbol signals found in compacted context")
    
    print("\n=== Direct Query Test ===")
    conn = get_connection()
    result = conn.execute("""
        SELECT symbol, trading_date, close, volume, trend_state, liquidity_state
        FROM analytics_marts.mart_agent_context_daily
        ORDER BY trading_date DESC
        LIMIT 5
    """).fetchall()
    print(f"Direct query result count: {len(result)}")
    if result:
        print("Sample rows from direct query:")
        for row in result:
            print(f"  {row}")
    
    print("\n=== Check if portfolio symbols exist in mart ===")
    symbols_to_check = [row.get('symbol') for row in portfolio.get('watchlist', [])]
    for symbol in symbols_to_check:
        result = conn.execute("""
            SELECT symbol, trading_date, close, volume, trend_state, liquidity_state
            FROM analytics_marts.mart_agent_context_daily
            WHERE symbol = ?
            ORDER BY trading_date DESC
            LIMIT 1
        """, [symbol]).fetchone()
        if result:
            print(f"  {symbol}: FOUND - {result}")
        else:
            print(f"  {symbol}: NOT FOUND")
    
    print("\n=== Debug: Test the exact query used by _compact_symbol_signals ===")
    test_symbols = [row.get('symbol') for row in portfolio.get('watchlist', [])][:3]
    test_symbol_list = ", ".join(f"'{s}'" for s in test_symbols)
    test_query = f"""
    WITH ranked AS (
        SELECT
            symbol,
            trading_date,
            close,
            volume,
            return_1d,
            return_5d,
            return_20d,
            return_60d,
            volatility_20d,
            volatility_60d,
            drawdown_from_peak,
            relative_strength_20d,
            relative_strength_60d,
            traded_value_proxy,
            volume_to_avg_20d,
            traded_value_cross_section_percentile,
            trend_state,
            liquidity_state,
            market_regime,
            volatility_regime,
            regime_score,
            breadth_score,
            advancer_ratio,
            data_quality_status,
            stale_days,
            agent_candidate_state,
            ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trading_date DESC) as rn
        FROM analytics_marts.mart_agent_context_daily
        WHERE symbol IN ({test_symbol_list})
    )
    SELECT * FROM ranked WHERE rn = 1
    """
    test_frame = conn.execute(test_query).df()
    print(f"Test query returned {len(test_frame)} rows")
    if not test_frame.empty:
        print("Columns:", test_frame.columns.tolist())
        print("First row:")
        print(test_frame.iloc[0].to_dict())
    
    print("\n=== Test Complete ===")


if __name__ == "__main__":
    main()
