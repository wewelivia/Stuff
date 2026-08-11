"""Test suite. Runs without Bloomberg, Macrobond or a network connection.

    python tests/test_sentiment.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sentiment_engine as eng
import sentiment_stats as stats
from providers.base import SeriesSpec, apply_release_lag, is_stale, to_weekly
from providers.cache import SeriesCache
from sentiment_builder import OBS_PER_YEAR, _months, _rule

RNG = np.random.default_rng(42)
PASSED, FAILED = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def bidx(n: int, start: str = "2004-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def walk(n: int, seed: int = 0, drift: float = 0.0003, vol: float = 0.01) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, vol, n))), index=bidx(n))


# --- engine ----------------------------------------------------------------
def test_causality():
    print("\n[causality]")
    s = walk(1000, seed=1)
    full = eng.causal_percentile_rank(s, "expanding", 100)
    s2 = s.copy()
    s2.iloc[-200:] *= 10
    mod = eng.causal_percentile_rank(s2, "expanding", 100)
    check("ranks before t unaffected by data after t",
          np.allclose(full.iloc[:-200].dropna(), mod.iloc[:-200].dropna()))
    check("the modified tail does change its own ranks",
          not np.allclose(full.iloc[-200:].dropna(), mod.iloc[-200:].dropna()))

    rising = pd.Series(np.arange(500, dtype=float), index=bidx(500))
    check("strictly rising series ranks at 1.0",
          np.allclose(eng.causal_percentile_rank(rising, "expanding", 10).dropna(), 1.0))


def test_hinge():
    print("\n[hinge]")
    r = pd.Series([0.0, 0.5, 0.90, 0.95, 1.0])
    h = eng.hinge(r, eng.TriggerRule("gt", 90))
    check("zero at and below threshold", h.iloc[1] == 0 and abs(h.iloc[2]) < 1e-12)
    check("one at the maximum", abs(h.iloc[4] - 1.0) < 1e-12)
    check("half at the midpoint", abs(h.iloc[3] - 0.5) < 1e-12)
    check("monotone in rank", bool((h.diff().dropna() >= 0).all()))
    hl = eng.hinge(pd.Series([0.0, 0.10, 0.5]), eng.TriggerRule("lt", 10))
    check("lt hinge one at zero, zero at threshold",
          abs(hl.iloc[0] - 1.0) < 1e-12 and abs(hl.iloc[1]) < 1e-12)
    for rule, pct in (("xx", 90), ("gt", 0), ("gt", 100)):
        try:
            eng.TriggerRule(rule, pct)
            check(f"invalid rule ({rule},{pct}) rejected", False)
        except ValueError:
            check(f"invalid rule ({rule},{pct}) rejected", True)


def test_transforms():
    print("\n[transforms]")
    n = 800
    base = pd.Series(RNG.normal(0, 1, n), index=bidx(n))
    spiked = base.copy()
    spiked.iloc[400] = 60.0
    rob = float((eng.robust_z(base).iloc[500:] - eng.robust_z(spiked).iloc[500:]).abs().mean())
    check("60-sigma outlier barely moves robust z", rob < 0.15, f"{rob:.4f}")
    check("robust z winsorised at 3", float(eng.robust_z(spiked).abs().max()) <= 3.0 + 1e-9)

    p = walk(n, seed=3)
    check("beta on itself is 1", np.allclose(eng.rolling_beta(p, p, 60).dropna(), 1.0, atol=1e-8))
    lev = pd.Series(100 * np.exp(np.cumsum(np.log(p / p.shift(1)).fillna(0) * 2)), index=p.index)
    check("beta of 2x series is 2", abs(eng.rolling_beta(lev, p, 60).dropna().mean() - 2.0) < 0.05)

    mono = pd.Series(np.arange(1, 100, dtype=float), index=bidx(99))
    check("RSI of rising series is 100 with no divide-by-zero",
          np.allclose(eng.rsi(mono, 14).dropna(), 100.0))
    lin = pd.Series(np.arange(300, dtype=float), index=bidx(300))
    check("3M difference on unit slope is 63", np.allclose(eng.diff_horizon(lin, 3).dropna(), 63.0))

    a = pd.Series(RNG.normal(0, 1, n), index=bidx(n))
    check("inverting one of two identical legs cancels",
          float(eng.mean_of_z({"x": a, "y": a}, invert=("y",)).dropna().abs().max()) < 1e-9)
    check("without inversion they reinforce",
          float(eng.mean_of_z({"x": a, "y": a}).dropna().abs().max()) > 0.5)


def _inputs(n=1500, k=8, correlated=False):
    idx = bidx(n)
    common = pd.Series(RNG.normal(0, 1, n), index=idx).rolling(20).mean()
    out = []
    for i in range(k):
        s = (common + pd.Series(RNG.normal(0, 0.05, n), index=idx)) if correlated \
            else pd.Series(RNG.normal(0, 1, n), index=idx).rolling(20).mean()
        out.append(eng.SentimentInput(id=f"in{i}", series=s, cluster=f"c{i % 3}",
                                      sell=eng.TriggerRule("gt", 90),
                                      buy=eng.TriggerRule("lt", 10), min_periods=250))
    return out


def test_aggregation():
    print("\n[aggregation]")
    inputs = _inputs()
    e = eng.SentimentEngine(inputs)
    res = e.compute("sell", "replica")
    check("reading within [0,1]", bool(((res.reading.dropna() >= 0) &
                                        (res.reading.dropna() <= 1)).all()))
    check("denominator counts available inputs", int(res.denominator.iloc[-1]) == 8)
    check("90th percentile rule fires near 10%",
          0.05 < float(res.fired_count.dropna().mean() / 8) < 0.16,
          f"{float(res.fired_count.dropna().mean()/8):.3f}")

    reduced = list(inputs)
    reduced[0] = eng.SentimentInput(id="in0", series=inputs[0].series * np.nan,
                                    cluster="c0", sell=eng.TriggerRule("gt", 90),
                                    min_periods=250)
    r2 = eng.SentimentEngine(reduced).compute("sell", "replica")
    check("missing input leaves the denominator", int(r2.denominator.iloc[-1]) == 7)
    check("dropped inputs named", "in0" in r2.dropped.iloc[-1])

    try:
        eng.SentimentEngine([inputs[0], inputs[0]])
        check("duplicate ids rejected", False)
    except ValueError:
        check("duplicate ids rejected", True)

    idx = bidx(1500)
    shared = pd.Series(RNG.normal(0, 1, 1500), index=idx).rolling(20).mean()
    lop = [eng.SentimentInput(id=f"d{i}", series=shared.copy(), cluster="crowd",
                              sell=eng.TriggerRule("gt", 90), min_periods=250)
           for i in range(6)]
    lop.append(eng.SentimentInput(
        id="lonely", series=pd.Series(RNG.normal(0, 1, 1500), index=idx).rolling(20).mean(),
        cluster="solo", sell=eng.TriggerRule("gt", 90), min_periods=250))
    e2 = eng.SentimentEngine(lop)
    corr = e2.compute("sell", "improved").reading.dropna().corr(
        e2.compute("sell", "replica").reading.dropna())
    check("cluster weighting differs from a flat vote", corr < 0.995, f"corr {corr:.4f}")


# --- statistics ------------------------------------------------------------
def test_newey_west():
    print("\n[newey-west]")
    n = 2000
    x = RNG.normal(0, 1, n)
    y = 0.5 * x + RNG.normal(0, 1, n)
    X = np.column_stack([np.ones(n), x])
    b, se = stats.newey_west_ols(y, X, 0)
    ols_se = np.sqrt(np.sum((y - X @ b) ** 2) / (n - 2) / np.sum((x - x.mean()) ** 2))
    check("zero-lag slope matches OLS", abs(b[1] - 0.5) < 0.1)
    check("zero-lag SE matches OLS", abs(se[1] - ols_se) / ols_se < 0.10)

    horizon = 63
    fwd = stats.forward_returns(walk(3000, seed=7), horizon).dropna()

    iid = pd.Series(RNG.random(len(fwd)) < 0.2, index=fwd.index)
    a = stats.mean_difference_test(fwd, iid, horizon, lags=0)
    b2 = stats.mean_difference_test(fwd, iid, horizon)
    check("iid signal: overlap alone barely changes the t-stat",
          abs(abs(b2["t_stat"]) - abs(a["t_stat"])) < 0.5,
          f"{a['t_stat']:.2f} -> {b2['t_stat']:.2f}")

    slow = pd.Series(RNG.normal(0, 1, len(fwd)), index=fwd.index).rolling(120).mean()
    persistent = (slow > slow.quantile(0.80)).fillna(False)
    naive = stats.mean_difference_test(fwd, persistent, horizon, lags=0)
    corr = stats.mean_difference_test(fwd, persistent, horizon)
    check("persistent signal: Newey-West materially shrinks the t-stat",
          abs(corr["t_stat"]) < abs(naive["t_stat"]) * 0.75,
          f"{naive['t_stat']:.2f} -> {corr['t_stat']:.2f} "
          f"({abs(naive['t_stat'])/max(abs(corr['t_stat']),1e-9):.1f}x)")
    check("lag matched to overlap", corr["lags"] == horizon - 1)
    check("effective sample is n/horizon",
          abs(stats.effective_sample_size(len(fwd), horizon) - len(fwd) / horizon) < 1e-9,
          f"{len(fwd)} -> {stats.effective_sample_size(len(fwd), horizon):.0f}")


def test_lift():
    print("\n[lift over base rate]")
    n, h = 4000, 63
    prices = walk(n, seed=11, drift=0.0004)
    fwd = stats.forward_returns(prices, h)
    check("drifting market base rate above 0.6",
          float((fwd.dropna() > 0).mean()) > 0.6,
          f"P(up)={float((fwd.dropna()>0).mean()):.1%}")

    planted = pd.Series(False, index=prices.index)
    planted[fwd < fwd.quantile(0.2)] = True
    good = stats.evaluate_signal(prices, planted, "sell", h)
    check("planted sell signal beats base rate", good.lift > 0.15,
          f"{good.base_rate:.1%} -> {good.conditional_rate:.1%}")

    rand = pd.Series(RNG.random(n) < 0.2, index=prices.index)
    null = stats.evaluate_signal(prices, rand, "sell", h)
    check("random signal has negligible lift", abs(null.lift) < 0.10, f"{null.lift:+.1%}")

    always = pd.Series(True, index=prices.index)
    ba = stats.evaluate_signal(prices, always, "buy", h)
    check("always-on buy signal: high hit rate, zero lift",
          ba.conditional_rate > 0.6 and abs(ba.lift) < 1e-9,
          f"hit {ba.conditional_rate:.1%}, lift {ba.lift:+.2%}")

    check("effective sample reported", null.n_effective < null.n / 10)
    check("non-overlapping cross-check computed",
          not np.isnan(null.non_overlapping_difference))
    ci = stats.bootstrap_lift_ci(fwd.dropna() < 0, rand.reindex(fwd.dropna().index),
                                 h, n_boot=200, seed=3)
    check("null signal CI contains zero", ci[0] <= 0 <= ci[1],
          f"[{ci[0]:+.3f}, {ci[1]:+.3f}]")


def test_calibration():
    print("\n[band calibration]")
    e = eng.SentimentEngine(_inputs(2500, 10))
    reading = e.compute("sell", "replica").reading
    bands = stats.calibrate_bands(reading)
    check("thresholds increase with quantile",
          bands["Mild"] <= bands["Moderate"] <= bands["Strong"] <= bands["Extreme"],
          ", ".join(f"{k} {v:.2f}" for k, v in bands.items()))

    distinct = sorted(reading.dropna().unique())
    step = min(np.diff(distinct)) if len(distinct) > 1 else np.nan
    check("replica reading is discrete on a 1/k grid", abs(step - 0.1) < 1e-9,
          f"{len(distinct)} values, step {step:.3f}")

    ci = stats.calibrate_bands_ci(reading, n_boot=150, seed=5)
    check("discrete intervals non-negative", len(ci) == 4 and bool((ci["width"] >= 0).all()))
    cont = stats.calibrate_bands_ci(e.compute("sell", "improved").reading, n_boot=150, seed=5)
    check("continuous intervals have width", len(cont) == 4 and bool((cont["width"] > 0).all()),
          ", ".join(f"{r.band}[{r.lo:.3f},{r.hi:.3f}]" for r in cont.itertuples()))

    wf = stats.walk_forward_bands(reading, min_periods=500)
    check("walk-forward undefined during burn-in", bool(wf["Extreme"].iloc[:499].isna().all()))

    corr_reading = eng.SentimentEngine(_inputs(2500, 10, True)).compute("sell", "replica").reading
    ref = stats.binomial_reference(10, 0.10, (0.95,))[0.95]
    check("correlated inputs have fatter tails than independent",
          float(corr_reading.dropna().quantile(0.95)) > ref,
          f"{float(corr_reading.dropna().quantile(0.95)):.2f} vs {ref:.2f}")
    red = stats.redundancy_ratio(corr_reading, 10, 0.10)
    check("effective input count below nominal", red["effective_k"] < red["nominal_k"],
          f"{red['nominal_k']:.0f} -> {red['effective_k']:.0f}")


def test_band_table():
    print("\n[band table]")
    prices = walk(3000, seed=13, drift=0.0004)
    reading = eng.SentimentEngine(_inputs(3000, 8)).compute("sell", "replica").reading
    table = stats.evaluate_by_band(prices, reading, eng.label_bands(reading), "sell", 63)
    check("table produced", not table.empty, f"{len(table)} bands")
    if not table.empty:
        check("base rate reported alongside conditional",
              {"base_rate", "conditional_rate", "lift"} <= set(table.columns))
        check("shares of time sum to one", abs(table["share_of_time"].sum() - 1.0) < 1e-9)
        check("thin bands flagged", "thin" in table.columns)
        check("base rate constant across bands",
              bool((table["base_rate"] - table["base_rate"].iloc[0]).abs().max() < 1e-12))


# --- providers and config --------------------------------------------------
def test_providers():
    print("\n[providers]")
    spec = SeriesSpec(source="macrobond", code="cftc_cme13874a_8o",
                      frequency="weekly", release_lag_days=3)
    check("spec key is filesystem safe", " " not in spec.key and "/" not in spec.key)
    try:
        SeriesSpec(source="reuters", code="x")
        check("unknown source rejected", False)
    except ValueError:
        check("unknown source rejected", True)

    tue = pd.Series([289619.0], index=pd.to_datetime(["2026-08-04"]))
    fri = apply_release_lag(tue, 3)
    check("Tuesday reading stamped at Friday publication",
          fri.index[0].date() == dt.date(2026, 8, 7) and fri.index[0].weekday() == 4)
    check("zero lag leaves the index alone",
          apply_release_lag(tue, 0).index[0] == tue.index[0])

    daily = pd.Series(range(20), index=pd.bdate_range("2026-07-06", periods=20))
    check("daily forward-fill collapses to weekly", len(to_weekly(daily)) == 4,
          f"{len(daily)} -> {len(to_weekly(daily))}")

    asof = dt.date(2026, 8, 11)
    check("8-day-old weekly series is fresh",
          not is_stale(pd.Series([1.0], index=pd.to_datetime(["2026-08-03"])), "weekly", asof))
    check("71-day-old monthly series is stale",
          is_stale(pd.Series([1.0], index=pd.to_datetime(["2026-06-01"])), "monthly", asof))

    import tempfile
    from providers.cache import BACKEND

    s1 = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    s2 = pd.Series([9.0, 3.0], index=pd.to_datetime(["2026-01-02", "2026-01-03"]))

    # Both backends are exercised, so the suite passes whether or not a parquet
    # engine is installed.
    for backend in ("csv", "parquet"):
        if backend == "parquet" and BACKEND != "parquet":
            check("parquet backend skipped, no engine installed", True, "csv in use")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            cache = SeriesCache(tmp, backend=backend)
            cache.write(spec, s1)
            check(f"{backend} cache round-trips", cache.read(spec).equals(s1.sort_index()))
            merged = cache.merge(spec, s2)
            check(f"{backend} merge keeps newest on overlap and extends",
                  len(merged) == 3 and merged.iloc[1] == 9.0)

    with tempfile.TemporaryDirectory() as tmp:
        SeriesCache(tmp, backend="csv").write(spec, s1)
        check("a cache written by the other backend is still readable",
              SeriesCache(tmp, backend="parquet").read(spec) is not None)


def test_config():
    print("\n[config]")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "config", "sentiment_tickers.yaml")
    cfg = yaml.safe_load(open(path, encoding="utf-8"))
    inputs = cfg["inputs"]

    check("21 inputs", len(inputs) == 21, str(len(inputs)))
    check("ids unique", len({i["id"] for i in inputs}) == 21)
    check("hsbc refs are 1-21 contiguous",
          sorted(i["hsbc_ref"] for i in inputs) == list(range(1, 22)))

    n_sell = sum(1 for i in inputs if i.get("sell"))
    n_buy = sum(1 for i in inputs if i.get("buy"))
    check("20 sell inputs as published", n_sell == 20, str(n_sell))
    check("13 buy inputs as published", n_buy == 13, str(n_buy))

    flat = [m for members in cfg["clusters"].values() for m in members]
    check("every input in exactly one cluster",
          sorted(flat) == sorted(i["id"] for i in inputs))

    for i in inputs:
        if not (i.get("series") or i.get("macrobond")):
            check(f"{i['id']} has a data source", False)
            break
    else:
        check("every input has a data source", True)

    subs = [i["id"] for i in inputs if i.get("is_substitute")]
    check("substitutes carry a UI label",
          all(inputs[[x["id"] for x in inputs].index(s)].get("ui_label") for s in subs),
          f"{len(subs)} substitutes")

    check("5y weekly window is 260 obs, not 1260",
          _rule({"rule": "lt", "pct": 10, "window": {"rolling_years": 5}}, "weekly").window == 260)
    check("5y daily window is 1260 obs",
          _rule({"rule": "lt", "pct": 10, "window": {"rolling_years": 5}}, "daily").window == 1260)
    check("expanding window passes through",
          _rule({"rule": "gt", "pct": 90, "window": "expanding"}).window == "expanding")
    check("horizon parsing handles 3M and 12M",
          _months("3M") == 3.0 and _months("12M") == 12.0 and _months(6) == 6.0)


class StubStore:
    """Stands in for the vendors so the full config path can be exercised."""

    INDEX = pd.bdate_range("2004-01-01", "2026-08-11")

    def get(self, spec, refresh=True, apply_lag=True):
        rng = np.random.default_rng(abs(hash(spec.code)) % 2 ** 31)
        n = len(self.INDEX)
        if spec.field in ("SHORT_INT", "OPEN_INT") or "cftc" in spec.code or "usfund" in spec.code:
            s = pd.Series(np.abs(np.cumsum(rng.normal(0, 1, n))) + 1000, index=self.INDEX)
        else:
            s = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n))), index=self.INDEX)
        return s.resample("W-FRI").last().dropna() if spec.frequency == "weekly" else s

    def provider_status(self):
        return {}

    def close(self):
        pass


def test_mixed_frequency():
    print("\n[mixed frequency]")
    wk = pd.Series(np.arange(300.0), index=pd.bdate_range("2020-01-03", periods=300, freq="W-FRI"))
    si = eng.SentimentInput(id="w", series=wk, cluster="c", sell=eng.TriggerRule("gt", 90),
                            min_periods=104, fill_limit=10)
    idx = pd.bdate_range(wk.index[0], wk.index[-1])
    r = si.ranks_on("sell", idx)
    live = r.loc[r.first_valid_index():]
    check("weekly ranks cover every business day after burn-in",
          live.notna().sum() == len(live), f"{live.notna().sum()}/{len(live)}")

    dead = eng.SentimentInput(
        id="dead", series=pd.Series([1.0, 2.0], index=pd.to_datetime(["2020-01-03", "2020-01-10"])),
        cluster="x", sell=eng.TriggerRule("gt", 90), min_periods=1, fill_limit=10)
    r2 = dead.ranks_on("sell", pd.bdate_range("2020-01-03", "2020-06-01"))
    check("a dead input expires rather than repeating for ever",
          r2.notna().sum() < 25, f"{r2.notna().sum()} of {len(r2)} bdays live")

    check("percentile windows are set on the input's own frequency",
          _rule({"rule": "lt", "pct": 10, "window": {"rolling_years": 5}}, "weekly").window == 260)


def test_end_to_end():
    print("\n[end to end]")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from sentiment_builder import SentimentBuilder
    cfg = yaml.safe_load(open(os.path.join(here, "config", "sentiment_tickers.yaml"),
                              encoding="utf-8"))
    inputs, report = SentimentBuilder(cfg, StubStore()).build(refresh=False)

    check("all 21 inputs build", len(report.built) == 21,
          f"{len(report.built)}/21, skipped {list(report.skipped)}")
    e = eng.SentimentEngine(inputs)
    check("denominators match the published 20 and 13",
          e.published_denominator("sell") == 20 and e.published_denominator("buy") == 13)
    check("all nine clusters present", len({i.cluster for i in inputs}) == 9)

    res = e.compute_all()
    check("both sides in both modes computed", len(res) == 4)
    for key, r in res.items():
        v = r.reading.dropna()
        last = v.index[-1]
        want = 18 if key.startswith("sell") else 12
        check(f"{key}: reading in [0,1] and denominator holds at the latest date",
              bool(v.between(0, 1).all()) and int(r.denominator.loc[last]) >= want,
              f"last {v.iloc[-1]:.3f}, denom {int(r.denominator.loc[last])}")

    prices = pd.Series(100 * np.exp(np.cumsum(RNG.normal(0.0004, 0.01, len(StubStore.INDEX)))),
                       index=StubStore.INDEX)
    sell = res["sell_replica"]
    reading = sell.reading.dropna()
    cal = stats.calibrate_bands(reading)
    lift = stats.evaluate_signal(prices, (sell.reading >= cal["Strong"]).fillna(False),
                                 "sell", 63, n_boot=50)
    check("evaluation runs end to end and reports base rate with lift",
          not np.isnan(lift.base_rate) and not np.isnan(lift.lift), lift.summary())


def main() -> int:
    print("=" * 70)
    print("sentiment tests")
    print("=" * 70)
    for fn in (test_causality, test_hinge, test_transforms, test_aggregation,
               test_newey_west, test_lift, test_calibration, test_band_table,
               test_providers, test_config, test_mixed_frequency, test_end_to_end):
        fn()
    print("\n" + "=" * 70)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print(f"  FAILED: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
