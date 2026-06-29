"""Pixel-test BQuant TradingView Lightweight Charts rendering with Playwright."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from playwright.sync_api import Page, sync_playwright


PIXEL_EVALUATOR = """
() => {
  function scoreRoot(root) {
    const canvases = Array.from(root.querySelectorAll('canvas'));
    let totalPixels = 0;
    let activePixels = 0;
    let readableCanvases = 0;
    for (const canvas of canvases) {
      const width = canvas.width || 0;
      const height = canvas.height || 0;
      if (!width || !height) continue;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      if (!ctx) continue;
      let data;
      try {
        data = ctx.getImageData(0, 0, width, height).data;
      } catch (error) {
        continue;
      }
      readableCanvases += 1;
      totalPixels += width * height;
      for (let i = 0; i < data.length; i += 4) {
        const alpha = data[i + 3];
        if (alpha < 8) continue;
        const r = data[i];
        const g = data[i + 1];
        const b = data[i + 2];
        const distanceFromBackground = Math.abs(r - 15) + Math.abs(g - 23) + Math.abs(b - 42);
        if (distanceFromBackground > 36) activePixels += 1;
      }
    }
    return {
      id: root.id,
      canvasCount: canvases.length,
      readableCanvases,
      totalPixels,
      activePixels,
      activeRatio: totalPixels ? activePixels / totalPixels : 0,
      legendButtons: root.querySelectorAll('.bq-lwc-legend button').length,
    };
  }
  return Array.from(document.querySelectorAll('.bq-lwc-root')).map(scoreRoot);
}
"""


def _assert_scores(label: str, scores: list[dict[str, Any]], min_roots: int) -> None:
    """Validate chart canvas scores with a conservative nonblank-pixel threshold."""
    if len(scores) < min_roots:
        raise AssertionError(f"{label}: expected at least {min_roots} chart roots, got {len(scores)}")
    failures = []
    for score in scores:
        if score["canvasCount"] < 2:
            failures.append(f"{score['id']}: canvasCount={score['canvasCount']}")
        if score["readableCanvases"] < 2:
            failures.append(f"{score['id']}: readableCanvases={score['readableCanvases']}")
        if score["activePixels"] < 800:
            failures.append(f"{score['id']}: activePixels={score['activePixels']}")
        if score["activeRatio"] < 0.001:
            failures.append(f"{score['id']}: activeRatio={score['activeRatio']:.6f}")
        if score["legendButtons"] < 6:
            failures.append(f"{score['id']}: legendButtons={score['legendButtons']}")
    if failures:
        raise AssertionError(f"{label}: nonblank chart validation failed: {failures}")


def _wait_for_charts(page: Page, expected_roots: int) -> list[dict[str, Any]]:
    """Wait for chart roots/canvases and return pixel scores."""
    page.wait_for_selector(".bq-lwc-root", timeout=30_000)
    page.wait_for_function(
        """
        expected => {
          const roots = Array.from(document.querySelectorAll('.bq-lwc-root'));
          return roots.length >= expected && roots.every(root => root.querySelectorAll('canvas').length >= 2);
        }
        """,
        arg=expected_roots,
        timeout=30_000,
    )
    page.wait_for_timeout(1_500)
    return page.evaluate(PIXEL_EVALUATOR)


def _test_dashboard(page: Page, base_url: str) -> list[dict[str, Any]]:
    """Pixel-test the home dashboard VNIndex/VN30 charts."""
    page.goto(base_url, wait_until="networkidle", timeout=60_000)
    scores = _wait_for_charts(page, expected_roots=2)
    _assert_scores("dashboard", scores[:2], min_roots=2)
    return scores


def _test_symbol(page: Page, base_url: str, *, mode: str) -> list[dict[str, Any]]:
    """Pixel-test Symbol Explorer for daily or intraday mode."""
    page.goto(f"{base_url}/symbol", wait_until="networkidle", timeout=60_000)
    if mode == "intraday":
        page.locator("label:has-text('daily')").click(timeout=10_000)
        page.get_by_role("option", name="intraday").click(timeout=10_000)
    scores = _wait_for_charts(page, expected_roots=1)
    _assert_scores(f"symbol_{mode}", scores[:1], min_roots=1)
    return scores


def main() -> None:
    """Run browser-based pixel tests against a running BQuant web app."""
    parser = argparse.ArgumentParser(description="Pixel-test Lightweight Charts in BQuant.")
    parser.add_argument("--base-url", default="http://127.0.0.1:6688")
    parser.add_argument("--screenshot-dir", default="logs/playwright")
    args = parser.parse_args()

    screenshot_dir = Path(args.screenshot_dir)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200}, device_scale_factor=1)
        page.on("console", lambda msg: print(f"browser-console[{msg.type}]: {msg.text}"))
        page.on("pageerror", lambda exc: print(f"browser-pageerror: {exc}"))
        try:
            results["dashboard"] = _test_dashboard(page, args.base_url)
            page.screenshot(path=screenshot_dir / "dashboard_lightweight_charts.png", full_page=True)
            results["symbol_daily"] = _test_symbol(page, args.base_url, mode="daily")
            page.screenshot(path=screenshot_dir / "symbol_daily_lightweight_charts.png", full_page=True)
            results["symbol_intraday"] = _test_symbol(page, args.base_url, mode="intraday")
            page.screenshot(path=screenshot_dir / "symbol_intraday_lightweight_charts.png", full_page=True)
        finally:
            browser.close()

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
