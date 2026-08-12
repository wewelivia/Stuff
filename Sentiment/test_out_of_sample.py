#!/usr/bin/env python
"""Test the one live result out of sample: does the reading add to a
short-horizon volatility forecast, beyond price and vol?

The full-sample run gave t = 2.76 at 21 days, just above the search-adjusted
bar for nine correlated specifications. Marginal, and in-sample. Three checks,
in ascending order of how much they would change my mind:

  1. Temporal split. Fit nothing, but evaluate separately before and after
     2018. The reading uses expanding percentiles, so the early sample is
     unstable; the later period is the honest test.

  2. Cross-market. The reading is built from US inputs. Does it inform
     volatility in Europe, Japan, the UK and emerging markets? Global equity
     volatility is highly correlated, so this is weaker evidence than it looks.
     The script reports each market's volatility correlation with the S&P so
     the degree of independence is visible rather than assumed.

  3. Placebo by circular shift. Shifting the reading in time preserves its
     autocorrelation exactly and destroys its alignment with the outcome. If
     the observed statistic sits inside that null distribution, persistence
     alone explains it. This is the strictest of the three and needs no new
     data.

Requires Bloomberg for the non-US indices. Everything else reads from cache.

    python test_out_of_sample.py
    python test_out_of_sample.py --horizon 21 --no-fetch
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sentiment_engine as eng          # noqa: E402
import sentiment_stats as stats         # noqa: E402
from providers import SeriesSpec, SeriesStore   # noqa: E402
from sentiment_builder import SentimentBuilder, load_config   # noqa: E402

MARKETS = {
    "S&P 500": "SPX Index",
    "EuroStoxx 50": "SX5E Index",
    "Nikkei 225": "NKY Index",
    "FTSE 100": "UKX Index",
    "EM": "MXEF Index",
}

SPLIT = "2018-01-01"


def realised(prices: pd.Series, window: int = 21) -> pd.Series:
    return np.log(prices / prices.shift(1)).rolling(window).std() * np.sqrt(252)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="sell", choices=["sell", "buy"])
    ap.add_argument("--mode", default="improved", choices=["replica", "improved"])
    ap.add_argument("--horizon", type=int, default=21)
    ap.add_argument("--target", default="volatility",
                    choices=["volatility", "drawdown", "return"])
    ap.add_argument("--no-fetch", action="store_true",
                    help="cache only; skips the non-US markets if absent")
    ap.add_argument("--shifts", type=int, default=400)
    args = ap.parse_args()

    with open(os.path.join(HERE, "config", "sentiment_config.yaml"), encoding="utf-8") as fh:
        app_cfg = yaml.safe_load(fh)
    cfg = load_config(os.path.join(HERE, "config", "sentiment_tickers.yaml"))

    store = SeriesStore(
        cache_dir=app_cfg.get("cache_dir", os.path.join(HERE, "cache")),
        history_start=dt.date.fromisoformat(
            str(cfg.get("defaults", {}).get("history_start", "2004-01-01"))),
        enable_bloomberg=not args.no_fetch,
        enable_macrobond=False)

    inputs, report = SentimentBuilder(cfg, store).build(refresh=False)
    if not inputs:
        print("no inputs built; run warm_cache.py first")
        return 1

    reading = eng.SentimentEngine(inputs).compute(args.side, args.mode).reading
    h, target = args.horizon, args.target

    print(f"{args.side} / {args.mode}, {len(report.built)} inputs, "
          f"target {target}, horizon {h}d\n")

    prices = {}
    for label, code in MARKETS.items():
        s = store.get(SeriesSpec(source="bloomberg", code=code), refresh=not args.no_fetch)
        if not s.empty:
            prices[label] = s
        else:
            print(f"  {label} ({code}) unavailable")
    store.close()

    if "S&P 500" not in prices:
        print("benchmark missing; run warm_cache.py first")
        return 1
    spx = prices["S&P 500"]

    # --- 1. temporal split -------------------------------------------------
    print("1. Temporal split")
    base = stats.incremental_test(spx, reading, h, target)
    print(f"   {'full sample':<22} t {base['t_stat']:+6.2f}   "
          f"delta R2 {base['delta_r2']:+.4f}   n eff {base['n_effective']:.0f}")
    for label, sl in (("to " + SPLIT, slice(None, SPLIT)),
                      ("from " + SPLIT, slice(SPLIT, None))):
        r = stats.incremental_test(spx.loc[sl], reading.loc[sl], h, target)
        if r.get("t_stat") == r.get("t_stat"):
            print(f"   {label:<22} t {r['t_stat']:+6.2f}   "
                  f"delta R2 {r['delta_r2']:+.4f}   n eff {r['n_effective']:.0f}")
        else:
            print(f"   {label:<22} insufficient data")

    # --- 2. cross-market ---------------------------------------------------
    print("\n2. Cross-market, same US-built reading")
    print(f"   {'market':<15}{'t':>8}{'delta R2':>11}{'n eff':>8}"
          f"{'vol corr vs SPX':>18}")
    spx_vol = realised(spx, h)
    for label, s in prices.items():
        r = stats.incremental_test(s, reading, h, target)
        if r.get("t_stat") != r.get("t_stat"):
            continue
        corr = realised(s, h).corr(spx_vol) if label != "S&P 500" else 1.0
        print(f"   {label:<15}{r['t_stat']:>8.2f}{r['delta_r2']:>11.4f}"
              f"{r['n_effective']:>8.0f}{corr:>18.2f}")
    if len(prices) > 1:
        print("   High correlations mean these are not independent samples.")

    # --- 3. placebo --------------------------------------------------------
    print(f"\n3. Placebo: {args.shifts} circular shifts of the reading")
    pl = stats.placebo_incremental(spx, reading, h, target,
                                   n_shifts=args.shifts)
    if pl.get("n_shifts", 0) < 30:
        print("   not enough data for the placebo")
    else:
        print(f"   observed |t| {abs(pl['observed']):.2f}")
        print(f"   null mean {pl['null_mean']:.2f}, 95th pct {pl['null_p95']:.2f}, "
              f"max {pl['null_max']:.2f}")
        print(f"   p-value {pl['p_value']:.3f}  "
              f"({int(pl['p_value'] * pl['n_shifts'])} of {pl['n_shifts']} shifts "
              f"matched or beat it)")
        print()
        if pl["p_value"] > 0.10:
            print("   The observed statistic is ordinary for a persistent series")
            print("   unrelated to the outcome. This is the result that matters,")
            print("   and it does not support building further.")
        elif pl["p_value"] > 0.05:
            print("   Borderline. Not enough to build on, not enough to dismiss.")
        else:
            print("   Survives the strictest of the three tests. Worth pursuing,")
            print("   with credit spreads and implied correlation as the next inputs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
