"""
Offline test for the Market Monitor engine — no network required.

    python test_market_monitor.py

Builds synthetic series with known properties and asserts that z-scores,
outsized-move flags, correlation shifts, and the regime quadrant come out
as expected. All fetching is bypassed.
"""

import math
import random
from datetime import date, timedelta

import market_monitor as mm


def synth_dates(n):
    d0, out = date(2023, 1, 2), []
    d = d0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def make_series(n=900, drift=0.0, vol=1.0, start=100.0, seed=1, shock=None):
    random.seed(seed)
    vals, v = [], start
    for i in range(n):
        step = random.gauss(drift, vol)
        if shock and i == n - 1:
            step = shock
        v = max(v + step, 1.0)
        vals.append(v)
    return list(zip(synth_dates(n), vals))


def approx(a, b, tol):
    return a is not None and abs(a - b) <= tol


def main():
    settings = {"zscore_lookback": 252, "trend_window": 63,
                "outsized_z": 2.0, "corr_shift_flag": 0.40, "stale_after_bdays": 5000}
    failures = []

    # ---- 1. z-score flags a known 4-sigma shock -----------------------------
    calm = make_series(n=800, vol=0.5, seed=7)
    shocked = make_series(n=800, vol=0.5, seed=7, shock=0.5 * 4.2)
    s_calm = mm.analyse_series({"name": "calm", "kind": "level"}, calm, settings)
    s_shock = mm.analyse_series({"name": "shock", "kind": "level"}, shocked, settings)
    if not (abs(s_calm["z_1d"]) < 2.5):
        failures.append(f'calm series z too large: {s_calm["z_1d"]}')
    if not (s_shock["z_1d"] and s_shock["z_1d"] >= 3.0):
        failures.append(f'shock not flagged: z={s_shock["z_1d"]}')

    # ---- 2. yield kind converts to bp ---------------------------------------
    yld = [(d, 4.00 + 0.001 * i) for i, d in enumerate(synth_dates(400))]
    yld[-1] = (yld[-1][0], yld[-2][1] + 0.10)   # +10bp on the last day
    s_y = mm.analyse_series({"name": "y", "kind": "yield"}, yld, settings)
    if not approx(s_y["chg_1d"], 10.0, 0.01):
        failures.append(f'bp conversion wrong: {s_y["chg_1d"]}')

    # ---- 3. correlation shift / sign flip detection -------------------------
    n = 400
    random.seed(3)
    base = [random.gauss(0, 1) for _ in range(n)]
    a_vals, b_vals, av, bv = [], [], 100.0, 100.0
    for i, e in enumerate(base):
        av += e
        # b follows a for the first half (corr +), mirrors it in the second (corr -)
        bv += e if i < n - 63 else -e
        a_vals.append(av); b_vals.append(bv)
    ds = synth_dates(n)
    A = mm.analyse_series({"name": "A", "kind": "level"}, list(zip(ds, a_vals)), settings)
    B = mm.analyse_series({"name": "B", "kind": "level"}, list(zip(ds, b_vals)), settings)
    corr = mm.correlation_shifts(
        [{"a": "A", "b": "B", "label": "test pair"}], {"A": A, "B": B}, settings)
    if not corr or not corr[0]["sign_flip"] or not corr[0]["flag"]:
        failures.append(f"sign flip not detected: {corr}")

    # ---- 4. regime quadrant: engineered reflation ---------------------------
    # growth proxies trending up strongly in the last 3m, inflation proxies too
    def trending(seed, late_drift):
        random.seed(seed)
        vals, v = [], 100.0
        for i in range(800):
            v += random.gauss(late_drift if i > 800 - 63 else 0.0, 0.6)
            v = max(v, 1.0)
            vals.append(v)
        return list(zip(synth_dates(800), vals))

    names = {
        "S&P 500": trending(11, +0.8),
        "US HY OAS": trending(12, -0.8),
        "Copper": trending(13, +0.8),
        "US 10y breakeven": trending(14, +0.8),
        "Brent": trending(15, +0.8),
    }
    by_name = {k: mm.analyse_series({"name": k, "kind": "level"}, v, settings)
               for k, v in names.items()}
    regime_cfg = {
        "growth": [{"name": "S&P 500", "weight": 1.0},
                   {"name": "US HY OAS", "weight": -1.0},
                   {"name": "Copper", "weight": 1.0}],
        "inflation": [{"name": "US 10y breakeven", "weight": 1.0},
                      {"name": "Brent", "weight": 1.0}],
        "threshold": 0.25,
    }
    r = mm.regime_quadrant(regime_cfg, by_name, settings)
    if r["quadrant"] != "reflation":
        failures.append(f'expected reflation, got {r["quadrant"]} '
                        f'(g={r["growth_score"]}, i={r["inflation_score"]})')

    # ---- 5. missing members degrade gracefully ------------------------------
    r2 = mm.regime_quadrant(regime_cfg, {"S&P 500": by_name["S&P 500"]}, settings)
    if r2["quadrant"] == "error":
        failures.append("regime crashed on missing members")

    # ---- 6. percentile sanity ------------------------------------------------
    if not (s_shock["pctile_3y"] is not None and 0 <= s_shock["pctile_3y"] <= 100):
        failures.append("percentile out of range")

    print("=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  ✗", f)
        raise SystemExit(1)
    print("ALL TESTS PASSED")
    print(f'  calm z_1d={s_calm["z_1d"]}, shock z_1d={s_shock["z_1d"]}')
    print(f'  bp move={s_y["chg_1d"]}bp')
    print(f'  corr {corr[0]["previous"]} -> {corr[0]["current"]} (flip={corr[0]["sign_flip"]})')
    print(f'  regime={r["quadrant"]} g={r["growth_score"]} i={r["inflation_score"]}')


if __name__ == "__main__":
    main()
