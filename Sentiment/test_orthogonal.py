#!/usr/bin/env python
"""Does the sentiment reading add anything to price and volatility alone?

The prior question before any optimisation. Much of what a sentiment aggregate
measures may be a restatement of what the market has just done, which you
already know without it. This asks two things:

  1. How much of the reading is explained by trailing returns and volatility.
  2. Whether the reading improves a forecast that already uses those.

Neither involves choosing a rule, so neither adds overfitting surface. Reads
from the cache; no vendor calls.

    python test_orthogonal.py
    python test_orthogonal.py --side buy --mode improved

Interpreting the output. A high explained R-squared with a near-zero
incremental t-statistic means the indicator is re-describing price and vol. A
low explained R-squared with a meaningful incremental t means it carries
something new, and that is the part worth building on.

The multiple-testing bar matters here. At roughly 89 independent observations,
trying fifty specifications produces an expected largest t-statistic near 2.2
under the null, with a 95th percentile above 3.1. Treat anything below about
3.2 as uninformative once you have searched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sentiment_engine as eng          # noqa: E402
import sentiment_stats as stats         # noqa: E402
from providers import SeriesSpec, SeriesStore   # noqa: E402
from sentiment_builder import SentimentBuilder, load_config   # noqa: E402

SEARCH_BAR = 3.2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="sell", choices=["sell", "buy"])
    ap.add_argument("--mode", default="improved", choices=["replica", "improved"])
    ap.add_argument("--horizons", default="21,63,126")
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
    prices = store.get(SeriesSpec(source=bm.get("source", "bloomberg"),
                                  code=bm.get("code", "SPX Index"),
                                  field=bm.get("field", "PX_LAST")), refresh=False)
    store.close()
    if prices.empty:
        print("benchmark series not in cache; run warm_cache.py first")
        return 1

    engine = eng.SentimentEngine(inputs)
    reading = engine.compute(args.side, args.mode).reading
    horizons = [int(h) for h in args.horizons.split(",")]

    print(f"{args.side} / {args.mode}, {len(report.built)} inputs, "
          f"{len(reading.dropna())} readings\n")

    # --- 1. how much of the reading is already in the price series ---------
    exp = stats.explained_by_state(reading, prices)
    r2 = exp.get("r_squared")
    print("How much of the reading is explained by trailing returns and vol")
    print(f"  R-squared {r2:.3f} on {exp['n']:.0f} observations")
    if r2 == r2:
        if r2 > 0.7:
            print("  Mostly a restatement of what the market has already done.")
        elif r2 > 0.4:
            print("  Substantially overlapping with price and vol, but not wholly.")
        else:
            print("  Largely independent of price and vol, which is the good case.")

    # --- 2. does it improve on price and vol -------------------------------
    print("\nIncremental content: forward outcome on trailing state, "
          "then on state plus reading")
    print(f"  {'target':<12}{'horizon':>8}{'R2 state':>10}{'R2 +read':>10}"
          f"{'delta':>9}{'t (HAC)':>9}{'n eff':>7}")

    best = 0.0
    for target in ("return", "drawdown", "volatility"):
        for h in horizons:
            try:
                r = stats.incremental_test(prices, reading, h, target)
            except Exception:
                continue
            if r.get("t_stat") != r.get("t_stat"):
                continue
            mark = ""
            if abs(r["t_stat"]) > SEARCH_BAR:
                mark = "  clears search bar"
            elif abs(r["t_stat"]) > 2.0:
                mark = "  nominal only"
            best = max(best, abs(r["t_stat"]))
            print(f"  {target:<12}{h:>7}d{r['r2_state_only']:>10.3f}"
                  f"{r['r2_with_reading']:>10.3f}{r['delta_r2']:>9.4f}"
                  f"{r['t_stat']:>9.2f}{r['n_effective']:>7.0f}{mark}")

    print(f"\nLargest incremental t-statistic: {best:.2f}")
    if best < 2.0:
        print("Nothing here beyond price and volatility. Reweighting or adding")
        print("variables will not change that; the aggregate as constructed is")
        print("not carrying independent information.")
    elif best < SEARCH_BAR:
        print(f"Nominally significant but below the {SEARCH_BAR} bar that any")
        print("search over specifications requires. Worth a look, not a conclusion.")
    else:
        print("Clears the search-adjusted bar. Confirm out of sample or on")
        print("another market before acting on it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
