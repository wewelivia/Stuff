#!/usr/bin/env python
"""Verify every file is present and at the expected version.

Files have been copied across in batches, so a partial update is easy to end up
with and hard to spot: the symptom is an ImportError somewhere unrelated, or a
page that renders stale. Each check looks for a marker that only exists in the
current version of that file.

    python check_install.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# path -> (marker that must be present, what it indicates)
CHECKS = {
    "sentiment_engine.py": ("def ranks_on", "native-frequency ranks with bounded fill"),
    "sentiment_stats.py": ("def redundancy_ratio", "calibration and lift machinery"),
    "sentiment_builder.py": ("FILL_LIMIT_BDAYS", "mixed-frequency alignment"),
    "api_sentiment.py": ("def get_status", "background build and status endpoint"),
    "sentiment.html": ('id="build"', "version marker and build polling"),
    "warm_cache.py": ("def specs_from_config", "cache warming"),
    "providers/__init__.py": ("class SeriesStore", "provider routing"),
    "providers/base.py": ("def apply_release_lag", "release-lag stamping"),
    "providers/cache.py": ("BACKEND", "parquet/CSV fallback"),
    "providers/bloomberg_provider.py": ("class BloombergProvider", "Bloomberg access"),
    "providers/macrobond_provider.py": ("class MacrobondProvider", "Macrobond access"),
    "config/sentiment_tickers.yaml": ("cftc_cme13874a_8o", "verified CFTC codes"),
    "config/sentiment_config.yaml": ("8030", "port not colliding with market-monitor"),
    "tests/test_sentiment.py": ("def test_end_to_end", "integration test"),
}

OPTIONAL = {"requirements.txt", "README.md", "run_sentiment.bat", ".gitignore"}


def main() -> int:
    missing, stale, ok = [], [], []

    for rel, (marker, what) in CHECKS.items():
        path = os.path.join(HERE, rel)
        if not os.path.exists(path):
            missing.append((rel, what))
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as exc:
            stale.append((rel, f"unreadable: {exc}"))
            continue
        (ok if marker in text else stale).append((rel, what))

    for rel, what in ok:
        print(f"  ok       {rel:<38} {what}")
    for rel, what in stale:
        print(f"  OLD      {rel:<38} missing: {what}")
    for rel, what in missing:
        print(f"  MISSING  {rel:<38} {what}")

    for rel in sorted(OPTIONAL):
        if not os.path.exists(os.path.join(HERE, rel)):
            print(f"  note     {rel:<38} not present (optional)")

    print()
    if not stale and not missing:
        print(f"All {len(ok)} files current.")
        print("Next: python warm_cache.py")
        return 0

    print(f"{len(ok)} current, {len(stale)} out of date, {len(missing)} missing.")
    print("Copy the listed files from the Sentiment folder and re-run this check.")
    if any(r == "sentiment.html" for r, _ in stale + missing):
        print("Also hard-refresh the browser (Ctrl+F5) after replacing sentiment.html.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
