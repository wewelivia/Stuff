"""Calibration and evaluation.

Signals are scored as lift over the unconditional base rate, never as a bare
hit rate. Three-month equity returns are positive around 70% of the time, so a
buy signal hitting 75% is close to uninformative while a sell signal hitting
45% is not.

Significance tests use Newey-West with the lag matched to the return overlap.
Overlapping h-period returns sampled every period share h-1 periods of data;
combined with a persistent signal this overstates naive t-statistics by a
factor of three to five.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


# --- forward returns -------------------------------------------------------
def forward_returns(prices: pd.Series, horizon: int) -> pd.Series:
    """Return from t to t+horizon, stamped at t."""
    return prices.shift(-horizon) / prices - 1.0


def effective_sample_size(n: int, horizon: int) -> float:
    """Independent-equivalent observation count."""
    return float(n) / float(max(horizon, 1))


# --- Newey-West ------------------------------------------------------------
def newey_west_ols(y: np.ndarray, X: np.ndarray, lags: int) -> Tuple[np.ndarray, np.ndarray]:
    """OLS with Bartlett-kernel HAC covariance."""
    XtX_inv = np.linalg.pinv(X.T @ X)
    beta = XtX_inv @ (X.T @ y)
    resid = y - X @ beta

    Xe = X * resid[:, None]
    S = Xe.T @ Xe
    for l in range(1, lags + 1):
        w = 1.0 - l / (lags + 1.0)
        A = Xe[l:].T @ Xe[:-l]
        S += w * (A + A.T)

    cov = XtX_inv @ S @ XtX_inv
    return beta, np.sqrt(np.maximum(np.diag(cov), 0.0))


def mean_difference_test(values: pd.Series, mask: pd.Series,
                         horizon: int, lags: Optional[int] = None) -> Dict[str, float]:
    """Difference in means, run as a regression on a constant and a dummy."""
    df = pd.concat([values.rename("y"), mask.rename("d")], axis=1).dropna()
    if len(df) < 30 or df["d"].sum() < 5 or (~df["d"].astype(bool)).sum() < 5:
        return {"difference": np.nan, "t_stat": np.nan, "se": np.nan,
                "n": float(len(df)), "n_signal": float(df["d"].sum()),
                "n_effective": np.nan, "lags": np.nan}

    y = df["y"].to_numpy(dtype=float)
    d = df["d"].astype(float).to_numpy()
    X = np.column_stack([np.ones(len(d)), d])

    L = int(lags) if lags is not None else max(int(horizon) - 1, 1)
    beta, se = newey_west_ols(y, X, L)

    diff, se_diff = float(beta[1]), float(se[1])
    return {"difference": diff, "se": se_diff,
            "t_stat": diff / se_diff if se_diff > 0 else np.nan,
            "n": float(len(df)), "n_signal": float(d.sum()),
            "n_effective": effective_sample_size(len(df), horizon), "lags": float(L)}


def non_overlapping_check(values: pd.Series, mask: pd.Series, horizon: int) -> Dict[str, float]:
    """Same comparison on non-overlapping subsamples, averaged over all offsets."""
    df = pd.concat([values.rename("y"), mask.rename("d")], axis=1).dropna()
    if len(df) < horizon * 4:
        return {"difference": np.nan, "n_blocks": 0.0, "offsets_used": 0.0}

    diffs, blocks = [], []
    for offset in range(horizon):
        sub = df.iloc[offset::horizon]
        sig = sub.loc[sub["d"].astype(bool), "y"]
        non = sub.loc[~sub["d"].astype(bool), "y"]
        if len(sig) >= 3 and len(non) >= 3:
            diffs.append(sig.mean() - non.mean())
            blocks.append(len(sub))

    if not diffs:
        return {"difference": np.nan, "n_blocks": 0.0, "offsets_used": 0.0}
    return {"difference": float(np.mean(diffs)),
            "dispersion_across_offsets": float(np.std(diffs)),
            "n_blocks": float(np.mean(blocks)),
            "offsets_used": float(len(diffs))}


# --- lift ------------------------------------------------------------------
@dataclass
class LiftResult:
    side: str
    horizon: int
    base_rate: float
    conditional_rate: float
    lift: float
    lift_ratio: float
    mean_return_all: float
    mean_return_signal: float
    return_difference: float
    t_stat: float
    n: int
    n_signal: int
    n_effective: float
    non_overlapping_difference: float
    lift_ci: Tuple[float, float] = (np.nan, np.nan)

    def summary(self) -> str:
        return (f"{self.side} @ {self.horizon}: base {self.base_rate:.1%} -> "
                f"{self.conditional_rate:.1%} (lift {self.lift:+.1%}), "
                f"return diff {self.return_difference:+.2%}, "
                f"t={self.t_stat:.2f}, n_eff={self.n_effective:.0f}")

    def to_dict(self) -> Dict[str, object]:
        return {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in self.__dict__.items()}


def evaluate_signal(prices: pd.Series, signal: pd.Series, side: str,
                    horizon: int, n_boot: int = 0,
                    mean_block: Optional[int] = None, seed: int = 0) -> LiftResult:
    """Sell signals are scored against a negative forward return, buy signals
    against a positive one."""
    fwd = forward_returns(prices, horizon)
    df = pd.concat([fwd.rename("fwd"), signal.rename("sig")], axis=1).dropna()
    if df.empty:
        raise ValueError("no overlapping observations between prices and signal")

    df["sig"] = df["sig"].astype(bool)
    outcome = df["fwd"] < 0 if side == "sell" else df["fwd"] > 0

    base = float(outcome.mean())
    sig_mask = df["sig"]
    cond = float(outcome[sig_mask].mean()) if sig_mask.sum() else np.nan

    test = mean_difference_test(df["fwd"], sig_mask, horizon)
    nonov = non_overlapping_check(df["fwd"], sig_mask, horizon)

    ci = (np.nan, np.nan)
    if n_boot > 0:
        ci = bootstrap_lift_ci(outcome, sig_mask, horizon, n_boot, mean_block, seed)

    return LiftResult(
        side=side, horizon=horizon, base_rate=base, conditional_rate=cond,
        lift=cond - base if not np.isnan(cond) else np.nan,
        lift_ratio=cond / base if base > 0 and not np.isnan(cond) else np.nan,
        mean_return_all=float(df["fwd"].mean()),
        mean_return_signal=float(df.loc[sig_mask, "fwd"].mean()) if sig_mask.sum() else np.nan,
        return_difference=test["difference"], t_stat=test["t_stat"],
        n=int(len(df)), n_signal=int(sig_mask.sum()),
        n_effective=test["n_effective"],
        non_overlapping_difference=nonov["difference"], lift_ci=ci)


def evaluate_by_band(prices: pd.Series, reading: pd.Series, bands: pd.Series,
                     side: str, horizon: int) -> pd.DataFrame:
    """Lift by band. Monotonic lift across bands is the property to look for;
    a single hot top band usually means a few episodes are doing the work."""
    fwd = forward_returns(prices, horizon)
    df = pd.concat([fwd.rename("fwd"), reading.rename("reading"),
                    bands.rename("band")], axis=1).dropna()
    if df.empty:
        return pd.DataFrame()

    outcome = df["fwd"] < 0 if side == "sell" else df["fwd"] > 0
    base = float(outcome.mean())
    base_ret = float(df["fwd"].mean())

    rows = []
    for band, grp in df.groupby("band", sort=False):
        hit = float(outcome.loc[grp.index].mean())
        rows.append({
            "band": band, "n": len(grp),
            "n_effective": effective_sample_size(len(grp), horizon),
            "share_of_time": len(grp) / len(df),
            "base_rate": base, "conditional_rate": hit, "lift": hit - base,
            "mean_return": float(grp["fwd"].mean()),
            "mean_return_all": base_ret,
            "return_difference": float(grp["fwd"].mean()) - base_ret,
        })
    out = pd.DataFrame(rows)
    out["thin"] = out["n_effective"] < 10
    return out


# --- bootstrap -------------------------------------------------------------
def stationary_bootstrap_indices(n: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap. Geometric block lengths preserve
    the persistence that makes the reading's tails fat."""
    p = 1.0 / max(mean_block, 1)
    idx = np.empty(n, dtype=int)
    i = int(rng.integers(0, n))
    for t in range(n):
        idx[t] = i
        i = int(rng.integers(0, n)) if rng.random() < p else (i + 1) % n
    return idx


def bootstrap_lift_ci(outcome: pd.Series, signal: pd.Series, horizon: int,
                      n_boot: int = 500, mean_block: Optional[int] = None,
                      seed: int = 0, alpha: float = 0.05) -> Tuple[float, float]:
    rng = np.random.default_rng(seed)
    o = outcome.to_numpy(dtype=float)
    s = signal.to_numpy(dtype=bool)
    block = mean_block if mean_block is not None else max(horizon, 2)

    lifts = []
    for _ in range(n_boot):
        idx = stationary_bootstrap_indices(len(o), block, rng)
        ob, sb = o[idx], s[idx]
        if sb.sum() < 5 or (~sb).sum() < 5:
            continue
        lifts.append(ob[sb].mean() - ob.mean())
    if len(lifts) < 20:
        return (np.nan, np.nan)
    return (float(np.quantile(lifts, alpha / 2)), float(np.quantile(lifts, 1 - alpha / 2)))


# --- band calibration ------------------------------------------------------
DEFAULT_BAND_QUANTILES: Dict[str, float] = {
    "Mild": 0.70, "Moderate": 0.85, "Strong": 0.95, "Extreme": 0.99}


def calibrate_bands(reading: pd.Series,
                    quantiles: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Thresholds from the reading's own distribution, replacing round numbers."""
    q = quantiles or DEFAULT_BAND_QUANTILES
    clean = reading.dropna()
    if clean.empty:
        return {k: np.nan for k in q}
    return {name: float(clean.quantile(p)) for name, p in q.items()}


def calibrate_bands_ci(reading: pd.Series, quantiles: Optional[Dict[str, float]] = None,
                       n_boot: int = 500, mean_block: int = 21,
                       seed: int = 0, alpha: float = 0.05) -> pd.DataFrame:
    """Intervals on the thresholds. Overlapping intervals between adjacent
    bands mean the distinction is not supported by the sample.

    The replica reading is discrete on a 1/k grid, so zero-width intervals are
    expected there and are not an error.
    """
    q = quantiles or DEFAULT_BAND_QUANTILES
    rng = np.random.default_rng(seed)
    vals = reading.dropna().to_numpy(dtype=float)
    if len(vals) < 100:
        return pd.DataFrame()

    draws: Dict[str, List[float]] = {k: [] for k in q}
    for _ in range(n_boot):
        sample = vals[stationary_bootstrap_indices(len(vals), mean_block, rng)]
        for name, p in q.items():
            draws[name].append(float(np.quantile(sample, p)))

    rows = []
    for name, p in q.items():
        arr = np.array(draws[name])
        lo, hi = float(np.quantile(arr, alpha / 2)), float(np.quantile(arr, 1 - alpha / 2))
        rows.append({"band": name, "quantile": p,
                     "threshold": float(np.quantile(vals, p)),
                     "lo": lo, "hi": hi, "width": hi - lo})
    return pd.DataFrame(rows)


def walk_forward_bands(reading: pd.Series, quantiles: Optional[Dict[str, float]] = None,
                       min_periods: int = 756) -> pd.DataFrame:
    """Thresholds using only data available at each date."""
    q = quantiles or DEFAULT_BAND_QUANTILES
    return pd.DataFrame(
        {name: reading.expanding(min_periods=min_periods).quantile(p)
         for name, p in q.items()},
        index=reading.index)


def binomial_reference(k: int, fire_prob: float,
                       quantiles: Sequence[float] = (0.7, 0.85, 0.95, 0.99),
                       n_sim: int = 20000, seed: int = 0) -> Dict[float, float]:
    """Firing share distribution if the k inputs were independent."""
    rng = np.random.default_rng(seed)
    counts = rng.binomial(k, fire_prob, size=n_sim) / float(k)
    return {q: float(np.quantile(counts, q)) for q in quantiles}


def redundancy_ratio(reading: pd.Series, k: int, fire_prob: float,
                     quantile: float = 0.95, seed: int = 0) -> Dict[str, float]:
    """Effective number of independent inputs implied by the observed tail."""
    observed = float(reading.dropna().quantile(quantile))
    best_k, best_gap = k, np.inf
    for k_try in range(2, k + 1):
        ref = binomial_reference(k_try, fire_prob, (quantile,), n_sim=8000, seed=seed)[quantile]
        gap = abs(ref - observed)
        if gap < best_gap:
            best_gap, best_k = gap, k_try
    return {"observed_quantile": observed, "nominal_k": float(k),
            "effective_k": float(best_k), "ratio": best_k / float(k)}


def full_evaluation(prices: pd.Series, reading: pd.Series, signal: pd.Series,
                    side: str, horizons: Sequence[int] = (21, 63, 126),
                    n_boot: int = 300) -> Dict[str, object]:
    """Multiple horizons: a signal that works at exactly one horizon and not
    near it is usually an artefact of the search that found it."""
    results: Dict[str, object] = {"side": side, "by_horizon": {}, "bands": {}}
    for h in horizons:
        try:
            results["by_horizon"][h] = evaluate_signal(prices, signal, side, h, n_boot=n_boot)
        except ValueError:
            continue
    results["bands"] = {
        "calibrated": calibrate_bands(reading),
        "ci": calibrate_bands_ci(reading, n_boot=min(n_boot, 300)),
        "published": {"Mild": 0.20, "Moderate": 0.30, "Strong": 0.40, "Extreme": 0.50},
    }
    return results
