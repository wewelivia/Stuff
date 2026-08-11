#!/usr/bin/env python
"""Populate the series cache and report what each vendor actually returned.

Run this before starting the API. A cold cache means the first web request
fetches every series from scratch, which takes minutes and leaves the tab
showing placeholders with no explanation.

It also doubles as the vendor diagnostic: every series is reported with its
observation count, date range and staleness, so a code that resolves but
returns nothing useful is visible here rather than silently dropping an input
later.

    python warm_cache.py                # fetch everything
    python warm_cache.py --check        # report the cache, fetch nothing
    python warm_cache.py --only cftc_equities,move
    python warm_cache.py --source macrobond
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from typing import Dict, List, Optional

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from providers import SeriesSpec, SeriesStore  # noqa: E402
from providers.base import STALENESS_LIMIT_DAYS  # noqa: E402
from sentiment_builder import load_config  # noqa: E402


def specs_from_config(cfg: dict, only: Optional[List[str]] = None,
                      source_filter: Optional[str] = None) -> List[tuple]:
    """Flatten the config into (input_id, SeriesSpec) pairs."""
    out: List[tuple] = []
    default_field = cfg.get("defaults", {}).get("field", "PX_LAST")

    for entry in cfg.get("inputs", []):
        iid = entry["id"]
        if only and iid not in only:
            continue
        freq = entry.get("frequency", "daily")

        mb = entry.get("macrobond") or {}
        lag = int(mb.get("release_lag_days", 0))
        for role in ("series", "long", "short", "open_interest"):
            if mb.get(role):
                out.append((iid, SeriesSpec(source="macrobond", code=mb[role],
                                            frequency=freq, release_lag_days=lag)))
        for leg in mb.get("basket", []) or []:
            for role in ("long", "short", "oi"):
                if leg.get(role):
                    out.append((iid, SeriesSpec(source="macrobond", code=leg[role],
                                                frequency=freq, release_lag_days=lag)))

        for series_entry in entry.get("series", []) or []:
            field = series_entry.get("field", default_field)
            for code in series_entry.get("candidates", []) or []:
                out.append((iid, SeriesSpec(source="bloomberg", code=code,
                                            field=field, frequency=freq)))

    if source_filter:
        out = [(i, s) for i, s in out if s.source == source_filter]

    seen, unique = set(), []
    for iid, spec in out:
        if spec.key in seen:
            continue
        seen.add(spec.key)
        unique.append((iid, spec))
    return unique


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="Comma-separated input ids.")
    ap.add_argument("--source", default=None, choices=["bloomberg", "macrobond"])
    ap.add_argument("--check", action="store_true", help="Report cache only, no fetching.")
    ap.add_argument("--config", default=os.path.join(HERE, "config", "sentiment_config.yaml"))
    ap.add_argument("--tickers", default=os.path.join(HERE, "config", "sentiment_tickers.yaml"))
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as fh:
        app_cfg = yaml.safe_load(fh)
    tickers = load_config(args.tickers)

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    pairs = specs_from_config(tickers, only, args.source)

    start = dt.date.fromisoformat(
        str(tickers.get("defaults", {}).get("history_start", "2004-01-01")))
    cache_dir = app_cfg.get("cache_dir", os.path.join(HERE, "cache"))

    store = SeriesStore(cache_dir=cache_dir, history_start=start,
                        enable_bloomberg=app_cfg.get("enable_bloomberg", True),
                        enable_macrobond=app_cfg.get("enable_macrobond", True))

    from providers.cache import BACKEND
    print(f"cache: {os.path.abspath(cache_dir)}  ({BACKEND})")
    print(f"{len(pairs)} series across {len({i for i, _ in pairs})} inputs, "
          f"history from {start}\n")

    # Also fetch the benchmark, or every lift figure comes back empty.
    bm = app_cfg.get("benchmark", {})
    pairs.append(("__benchmark__", SeriesSpec(source=bm.get("source", "bloomberg"),
                                              code=bm.get("code", "SPX Index"),
                                              field=bm.get("field", "PX_LAST"))))

    rows: List[Dict[str, object]] = []
    t0 = time.time()
    width = max(len(s.code) for _, s in pairs) + 2

    for n, (iid, spec) in enumerate(pairs, start=1):
        t1 = time.time()
        try:
            series = store.get(spec, refresh=not args.check)
        except Exception as exc:
            series = pd.Series(dtype=float)
            print(f"  [{n:>3}/{len(pairs)}] {spec.code:<{width}} ERROR {type(exc).__name__}: {exc}")
            rows.append({"input": iid, "code": spec.code, "source": spec.source,
                         "ok": False, "error": str(exc)})
            continue

        elapsed = time.time() - t1
        if series.empty:
            print(f"  [{n:>3}/{len(pairs)}] {spec.code:<{width}} EMPTY  ({elapsed:.1f}s)  [{iid}]")
            rows.append({"input": iid, "code": spec.code, "source": spec.source,
                         "ok": False, "error": "no observations"})
            continue

        last = series.index[-1].date()
        stale_days = (dt.date.today() - last).days
        limit = STALENESS_LIMIT_DAYS.get(spec.frequency, 60)
        flag = "  STALE" if stale_days > limit else ""
        print(f"  [{n:>3}/{len(pairs)}] {spec.code:<{width}} {len(series):>6} obs  "
              f"{series.index[0].date()} to {last}  {stale_days:>3}d  ({elapsed:.1f}s){flag}")
        rows.append({"input": iid, "code": spec.code, "source": spec.source, "ok": True,
                     "n": int(len(series)), "first": str(series.index[0].date()),
                     "last": str(last), "staleness_days": stale_days,
                     "stale": stale_days > limit})

    store.close()

    ok = [r for r in rows if r.get("ok")]
    bad = [r for r in rows if not r.get("ok")]
    stale = [r for r in ok if r.get("stale")]

    print(f"\n{'-' * 60}")
    print(f"{len(ok)} of {len(rows)} series retrieved in {time.time() - t0:.0f}s")
    if stale:
        print(f"{len(stale)} stale: {', '.join(r['code'] for r in stale)}")
    if bad:
        print(f"{len(bad)} failed:")
        for r in bad:
            print(f"  {r['code']:<28} [{r['input']}] {r.get('error')}")

    affected = sorted({r["input"] for r in bad if r["input"] != "__benchmark__"})
    if affected:
        print(f"\nInputs with at least one missing series: {', '.join(affected)}")
        print("Some carry alternates and will still build; the tab's dropped-inputs")
        print("list shows what actually made it in.")

    path = os.path.join(cache_dir, "_warm_report.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"run_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "series": rows}, fh, indent=2, default=str)
    print(f"\nWrote {path}")
    print("Now start the API: uvicorn api_sentiment:app --host 0.0.0.0 --port 8030")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
