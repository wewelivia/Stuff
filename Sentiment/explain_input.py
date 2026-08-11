#!/usr/bin/env python
"""Trace one input end to end and show where it fails.

The tab can only say "unavailable". This prints every stage: which files are
loaded, what the vendor returned, what each transform did to the observation
count, how the rule's window converts at that frequency, and whether a
percentile can be produced at all.

    python explain_input.py short_interest_equity_etfs
    python explain_input.py --all
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sentiment_engine as eng          # noqa: E402
import sentiment_builder as sb          # noqa: E402
from providers import SeriesSpec, SeriesStore   # noqa: E402


def module_report() -> bool:
    """Confirm the loaded modules are the versions this script expects."""
    print("loaded modules")
    ok = True
    checks = [
        ("sentiment_builder", sb, "OBS_PER_YEAR", "semimonthly"),
        ("sentiment_builder", sb, "MIN_PERIODS", "semimonthly"),
        ("sentiment_engine", eng, "OBS_PER_MONTH", "semimonthly"),
    ]
    print(f"  {'sentiment_builder':<20} {sb.__file__}")
    print(f"  {'sentiment_engine':<20} {eng.__file__}")

    for label, module, attr, key in checks:
        value = getattr(module, attr, None)
        if value is None:
            print(f"  MISSING  {label}.{attr} does not exist — file is out of date")
            ok = False
        elif key not in value:
            print(f"  OLD      {label}.{attr} has no '{key}' entry: {value}")
            ok = False
        else:
            print(f"  ok       {label}.{attr} = {value}")

    if not ok:
        print("\n  Python is not running the files you think it is. Check for a stale")
        print("  __pycache__ and confirm the copy landed, then re-run.")
    return ok


def explain(iid: str, cfg: dict, store: SeriesStore) -> None:
    entry = next((e for e in cfg["inputs"] if e["id"] == iid), None)
    if entry is None:
        print(f"\n{iid}: not in the config")
        return

    freq = entry.get("frequency", "daily")
    print(f"\n{'=' * 70}\n{iid}   (frequency: {freq})")

    builder = sb.SentimentBuilder(cfg, store)
    raw = builder._resolve_inputs(entry, refresh=False)
    if not raw:
        print("  no series resolved — nothing came back from the vendor or cache")
        return

    print("  series retrieved:")
    for role, s in raw.items():
        if s is None or s.empty:
            print(f"    {role:<24} EMPTY")
            continue
        gap = (s.index[-1] - s.index[0]).days / max(len(s) - 1, 1)
        print(f"    {role:<24} {len(s):>6} obs  {s.index[0].date()} to {s.index[-1].date()}"
              f"  median gap {gap:.1f}d")

    try:
        series = builder._build_series(entry, refresh=False)
    except Exception as exc:
        print(f"  transform FAILED: {type(exc).__name__}: {exc}")
        return

    if series is None or series.dropna().empty:
        print("  transform produced no usable observations")
        return

    n = len(series.dropna())
    print(f"  after transforms: {n} observations "
          f"({series.dropna().index[0].date()} to {series.dropna().index[-1].date()})")

    per_month = eng.OBS_PER_MONTH.get(freq, eng.WEEKS_PER_MONTH)
    print(f"  1 month = {per_month} observations at this frequency")

    min_periods = sb.MIN_PERIODS.get(freq, 252)
    print(f"  min_periods = {min_periods}")

    for side in ("sell", "buy"):
        rule = sb._rule(entry.get(side), freq)
        if rule is None:
            print(f"  {side:<5} no rule")
            continue
        window = rule.window
        if window == "expanding":
            verdict = "ok, expanding"
        elif window > n:
            verdict = f"IMPOSSIBLE — needs {window}, only {n} exist"
        else:
            verdict = f"ok, needs {window} of {n}"
        print(f"  {side:<5} {rule.rule} {rule.pct:.0f}th, window {window}  ->  {verdict}")

        ranks = eng.causal_percentile_rank(series, window, min_periods)
        live = ranks.dropna()
        if live.empty:
            print(f"        no percentile produced, so this input can never fire")
        else:
            print(f"        {len(live)} percentiles, latest {live.iloc[-1]:.2f} "
                  f"on {live.index[-1].date()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="*", help="input ids; default is the short-interest pair")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not module_report():
        return 1

    import yaml
    with open(os.path.join(HERE, "config", "sentiment_config.yaml"), encoding="utf-8") as fh:
        app_cfg = yaml.safe_load(fh)
    cfg = sb.load_config(os.path.join(HERE, "config", "sentiment_tickers.yaml"))

    import datetime as dt
    store = SeriesStore(
        cache_dir=app_cfg.get("cache_dir", os.path.join(HERE, "cache")),
        history_start=dt.date.fromisoformat(
            str(cfg.get("defaults", {}).get("history_start", "2004-01-01"))),
        enable_bloomberg=False, enable_macrobond=False)   # cache only, no vendor calls

    ids = ([e["id"] for e in cfg["inputs"]] if args.all
           else args.inputs or ["short_interest_equity_etfs",
                                "short_interest_equity_bond_etfs"])
    for iid in ids:
        explain(iid, cfg, store)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
