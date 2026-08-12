#!/usr/bin/env python
"""Selection-corrected placebo. Supersedes test 3 in test_out_of_sample.py.

That test compared the observed statistic for volatility at 21 days against a
null built from the same single cell. But that cell was chosen as the best of
nine, so its p-value of 0.025 is optimistic: the null should be the
distribution of the largest statistic across everything tried.

This recomputes all nine cells for every circular shift and keeps the maximum,
so like is compared with like. It takes a few minutes, since it is nine
regressions per shift.

    python test_placebo_corrected.py
    python test_placebo_corrected.py --shifts 200
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sentiment_engine as eng          # noqa: E402
import sentiment_stats as stats         # noqa: E402
from providers import SeriesSpec, SeriesStore   # noqa: E402
from sentiment_builder import SentimentBuilder, load_config   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="sell", choices=["sell", "buy"])
    ap.add_argument("--mode", default="improved", choices=["replica", "improved"])
    ap.add_argument("--shifts", type=int, default=300)
    args = ap.parse_args()

    with open(os.path.join(HERE, "config", "sentiment_config.yaml"), encoding="utf-8") as fh:
        app_cfg = yaml.safe_load(fh)
    cfg = load_config(os.path.join(HERE, "config", "sentiment_tickers.yaml"))

    store = SeriesStore(
        cache_dir=app_cfg.get("cache_dir", os.path.join(HERE, "cache")),
        history_start=dt.date.fromisoformat(
            str(cfg.get("defaults", {}).get("history_start", "2004-01-01"))),
        enable_bloomberg=False, enable_macrobond=False)

    inputs, report = SentimentBuilder(cfg, store).build(refresh=False)
    if not inputs:
        print("no inputs built; run warm_cache.py first")
        return 1

    bm = app_cfg.get("benchmark", {})
    prices = store.get(SeriesSpec(source="bloomberg", code=bm.get("code", "SPX Index"),
                                  field=bm.get("field", "PX_LAST")), refresh=False)
    store.close()
    if prices.empty:
        print("benchmark not in cache")
        return 1

    reading = eng.SentimentEngine(inputs).compute(args.side, args.mode).reading

    print(f"{args.side} / {args.mode}, {len(report.built)} inputs")
    print(f"{args.shifts} shifts x 9 cells. This takes a few minutes.\n")

    t0 = time.time()
    res = stats.placebo_max_incremental(prices, reading, n_shifts=args.shifts)
    if res.get("n_shifts", 0) < 30:
        print("not enough data")
        return 1

    print(f"cells searched per shift : {res['n_cells']}")
    print(f"observed max |t|         : {res['observed_max']:.2f}")
    print(f"null mean                : {res['null_mean']:.2f}")
    print(f"null 95th percentile     : {res['null_p95']:.2f}")
    print(f"null maximum             : {res['null_max']:.2f}")
    print(f"selection-corrected p    : {res['p_value']:.3f}")
    print(f"({time.time() - t0:.0f}s)\n")

    p = res["p_value"]
    if p > 0.20:
        print("The result is ordinary once selection is accounted for. A")
        print("persistent series unrelated to volatility produces this")
        print("routinely. Nothing here to build on.")
    elif p > 0.10:
        print("Weak. Selection accounts for most of the apparent significance.")
        print("Not a basis for adding inputs or reweighting.")
    elif p > 0.05:
        print("Borderline after correction. Worth one more independent test,")
        print("not worth building on yet.")
    else:
        print("Survives selection correction. The short-horizon volatility")
        print("channel is real enough to pursue; credit spreads and implied")
        print("correlation are the inputs most likely to strengthen it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
