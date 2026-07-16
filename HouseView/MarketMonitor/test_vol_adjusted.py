"""
test_vol_adjusted.py — offline validation of the merged Market Monitor.

No Bloomberg, no network. Run:  python test_vol_adjusted.py
Covers the new vol-adjusted scoring plus a full synthetic pass through
analyse_series, correlation_shifts and regime_quadrant to prove the merge
did not disturb the retained logic.
"""

import math
import random

import market_monitor as mm

SETTINGS = {
    "vol_window": 63,
    "horizons": [{"key": "1d", "days": 1}, {"key": "1w", "days": 5},
                 {"key": "1m", "days": 21}, {"key": "3m", "days": 63}],
    "trend_window": 63,
    "corr_shift_flag": 0.40,
    "stale_after_bdays": 5,
}


def _walk(seed, n, start, vol, kind="price"):
    random.seed(seed)
    v, out = start, []
    for i in range(n):
        if kind == "price":
            v *= math.exp(random.gauss(0.0002, vol))
        else:
            v += random.gauss(0, vol)
        out.append((f"2025-{(i//21)%12+1:02d}-{i%21+1:02d}", v))
    # dates must be sortable & unique enough: rebuild as a simple counter
    return [(f"D{i:04d}", val) for i, (_, val) in enumerate(out)]


def test_flat_series_scores_none():
    s = mm.vol_adjusted_scores([100.0] * 200, "price",
                               SETTINGS["horizons"], 63)
    assert all(v is None for v in s.values()), "flat series must not score"
    print("ok  flat series -> None on every horizon")


def test_crash_day_flags():
    random.seed(7)
    vals = [100.0]
    for _ in range(200):
        vals.append(vals[-1] * math.exp(random.gauss(0, 0.01)))
    vals.append(vals[-1] * 0.90)
    s = mm.vol_adjusted_scores(vals, "price", SETTINGS["horizons"], 63)
    assert s["1d"]["z"] < -5, f"10% crash on 1% vol must flag: {s['1d']}"
    print(f"ok  crash day -> 1d z={s['1d']['z']}")


def test_yield_moves_in_bp():
    random.seed(3)
    vals = [4.00]
    for _ in range(200):
        vals.append(vals[-1] + random.gauss(0, 0.04))
    vals.append(vals[-1] + 0.20)
    s = mm.vol_adjusted_scores(vals, "yield", SETTINGS["horizons"], 63)
    assert abs(s["1d"]["move"] - 20.0) < 1e-6, "yield move must be in bp"
    assert s["1d"]["z"] > 3
    print(f"ok  +20bp day -> z={s['1d']['z']}, move={s['1d']['move']}bp")


def test_vol_window_excludes_the_move():
    random.seed(11)
    vals = [100.0]
    for _ in range(120):
        vals.append(vals[-1] * math.exp(random.gauss(0, 0.002)))
    for _ in range(5):
        vals.append(vals[-1] * math.exp(0.03))
    s = mm.vol_adjusted_scores(vals, "price", SETTINGS["horizons"], 63)
    assert s["1w"]["z"] > 10, "a move must not inflate its own yardstick"
    print(f"ok  vol window excludes the move -> 1w z={s['1w']['z']}")


def test_analyse_series_payload_shape():
    data = _walk(42, 260, 5000, 0.011)
    cfg = {"name": "S&P 500", "kind": "price", "asset_class": "equities",
           "region": "US", "unit": "idx"}
    s = mm.analyse_series(cfg, data, SETTINGS)
    assert s is not None
    for k in ("1d", "1w", "1m", "3m"):
        assert s["horizons"][k] is not None, f"horizon {k} missing"
    assert s["z_1d"] is not None and s["chg_1d"] is not None
    assert len(s["spark"]) == 60
    print("ok  analyse_series carries horizons + back-compat z_1d/z_5d")


def test_regime_and_correlations_still_work():
    settings = dict(SETTINGS)
    series = {
        "S&P 500":   ("price", _walk(1, 400, 5000, 0.011)),
        "US 10y":    ("yield", _walk(2, 400, 4.2, 0.045)),
        "US HY OAS": ("oas",   _walk(3, 400, 3.1, 0.05)),
        "Copper":    ("price", _walk(4, 400, 4.3, 0.012)),
        "US 2s10s":  ("yield", _walk(5, 400, 0.4, 0.03)),
        "US 10y breakeven": ("yield", _walk(6, 400, 2.3, 0.02)),
        "US 5y breakeven":  ("yield", _walk(7, 400, 2.4, 0.02)),
        "Brent":     ("price", _walk(8, 400, 80, 0.018)),
        "Gold":      ("price", _walk(9, 400, 2400, 0.009)),
        "Dollar index": ("price", _walk(10, 400, 104, 0.004)),
    }
    by_name = {}
    for name, (kind, data) in series.items():
        s = mm.analyse_series({"name": name, "kind": kind}, data, settings)
        assert s is not None, name
        by_name[name] = s

    regime_cfg = {
        "growth": [{"name": "S&P 500", "weight": 1.0},
                   {"name": "US HY OAS", "weight": -1.0},
                   {"name": "Copper", "weight": 1.0},
                   {"name": "US 2s10s", "weight": 0.5}],
        "inflation": [{"name": "US 10y breakeven", "weight": 1.0},
                      {"name": "US 5y breakeven", "weight": 1.0},
                      {"name": "Brent", "weight": 0.75},
                      {"name": "Gold", "weight": 0.25}],
        "threshold": 0.25,
    }
    reg = mm.regime_quadrant(regime_cfg, by_name, settings)
    assert reg["quadrant"] != "indeterminate"
    assert reg["growth_score"] is not None and reg["inflation_score"] is not None

    pairs = [{"a": "S&P 500", "b": "US 10y", "label": "Equity-bond"},
             {"a": "Dollar index", "b": "Gold", "label": "Dollar-gold"}]
    corr = mm.correlation_shifts(pairs, by_name, settings)
    assert len(corr) == 2
    assert all(-1 <= c["current"] <= 1 for c in corr)
    print(f"ok  regime={reg['quadrant']} (g={reg['growth_score']}, "
          f"i={reg['inflation_score']}); {len(corr)} correlation pairs")


if __name__ == "__main__":
    test_flat_series_scores_none()
    test_crash_day_flags()
    test_yield_moves_in_bp()
    test_vol_window_excludes_the_move()
    test_analyse_series_payload_shape()
    test_regime_and_correlations_still_work()
    print("\nall merged-monitor tests passed")
