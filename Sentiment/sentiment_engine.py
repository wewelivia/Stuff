"""Transforms, percentile triggers and aggregation for the sentiment indicator.

No vendor dependencies. Takes prepared pandas Series and returns signal frames.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Union

import numpy as np
import pandas as pd

TRADING_DAYS_PER_MONTH = 21
WEEKS_PER_MONTH = 4.348

# Observations per month by frequency. Horizons in the config are expressed in
# months and must convert on the series' own frequency: at semi-monthly, three
# months is six observations, not thirteen.
OBS_PER_MONTH = {"daily": TRADING_DAYS_PER_MONTH, "weekly": WEEKS_PER_MONTH,
                 "semimonthly": 2.0, "monthly": 1.0}


# --- transforms ------------------------------------------------------------
def diff_horizon(s: pd.Series, months: float, freq: str = "daily") -> pd.Series:
    lag = max(int(round(months * OBS_PER_MONTH.get(freq, WEEKS_PER_MONTH))), 1)
    return s - s.shift(lag)


def smooth(s: pd.Series, months: float, freq: str = "daily") -> pd.Series:
    window = max(int(round(months * OBS_PER_MONTH.get(freq, WEEKS_PER_MONTH))), 1)
    return s.rolling(window, min_periods=max(window // 2, 1)).mean()


def robust_z(s: pd.Series, window: Optional[int] = None, winsorise: float = 3.0) -> pd.Series:
    """Median/MAD z-score. 1.4826 rescales MAD to a standard deviation
    equivalent under normality."""
    if window is None:
        med = s.expanding(min_periods=30).median()
        mad = (s - med).abs().expanding(min_periods=30).median()
    else:
        min_p = max(window // 4, 30)
        med = s.rolling(window, min_periods=min_p).median()
        mad = (s - med).abs().rolling(window, min_periods=min_p).median()

    scale = (1.4826 * mad).replace(0.0, np.nan)
    return ((s - med) / scale).clip(-winsorise, winsorise)


def rolling_beta(y: pd.Series, x: pd.Series, window: int) -> pd.Series:
    ry, rx = y.pct_change(), x.pct_change()
    df = pd.concat([ry.rename("y"), rx.rename("x")], axis=1).dropna()
    cov = df["y"].rolling(window).cov(df["x"])
    var = df["x"].rolling(window).var().replace(0.0, np.nan)
    return (cov / var).reindex(y.index)


def rsi(s: pd.Series, window: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).where(loss != 0, 100.0)


def realised_vol(s: pd.Series, window: int = 60, annualise: int = 252) -> pd.Series:
    return s.pct_change().rolling(window).std() * np.sqrt(annualise)


def vol_target_weight(s: pd.Series, target_vol: float = 0.10,
                      window: int = 60, cap: float = 3.0) -> pd.Series:
    return (target_vol / realised_vol(s, window).replace(0.0, np.nan)).clip(upper=cap)


def inverse_vol_weights(sleeves: Dict[str, pd.Series], window: int = 60) -> pd.DataFrame:
    vols = pd.DataFrame({k: realised_vol(v, window) for k, v in sleeves.items()})
    inv = 1.0 / vols.replace(0.0, np.nan)
    return inv.div(inv.sum(axis=1), axis=0)


def mean_of_z(components: Dict[str, pd.Series], window: Optional[int] = None,
              invert: Sequence[str] = ()) -> pd.Series:
    """Average of robust z-scores. Legs named in `invert` have their sign
    flipped first; without this a risk-on/risk-off basket cancels itself out."""
    zs = {}
    for name, s in components.items():
        z = robust_z(s, window)
        zs[name] = -z if name in invert else z
    return pd.DataFrame(zs).mean(axis=1, skipna=True)


def first_principal_component(frame: pd.DataFrame, window: int = 252) -> pd.Series:
    """Rolling first PC of standardised returns. Sign is pinned to the first
    column, otherwise the eigenvector flips arbitrarily between windows."""
    rets = frame.pct_change()
    out = pd.Series(index=frame.index, dtype=float)
    anchor_name = list(frame.columns)[0]

    for i in range(window, len(rets)):
        block = rets.iloc[i - window:i].dropna()
        if len(block) < window // 2 or block.shape[1] < 2:
            continue
        std = block.std().replace(0.0, np.nan)
        z = ((block - block.mean()) / std).dropna(axis=1, how="all").fillna(0.0)
        if z.shape[1] < 2:
            continue
        try:
            _, _, vt = np.linalg.svd(z.values, full_matrices=False)
        except np.linalg.LinAlgError:
            continue
        loadings = vt[0]
        if anchor_name in z.columns:
            pos = list(z.columns).index(anchor_name)
            if loadings[pos] < 0:
                loadings = -loadings
        out.iloc[i] = float(z.values[-1] @ loadings)
    return out


def trend_replication(s: pd.Series, lookbacks: Sequence[int] = (21, 63, 252),
                      vol_window: int = 60, cap: float = 2.0) -> pd.Series:
    """Vol-scaled time-series momentum blend."""
    vol = realised_vol(s, vol_window).replace(0.0, np.nan)
    signals = []
    for lb in lookbacks:
        mom = s / s.shift(lb) - 1.0
        signals.append(np.sign(mom) * (mom.abs() / vol).clip(upper=cap))
    return pd.concat(signals, axis=1).mean(axis=1)


# --- triggers --------------------------------------------------------------
@dataclass(frozen=True)
class TriggerRule:
    rule: str                                   # "gt" or "lt"
    pct: float                                  # 0-100
    window: Union[str, int] = "expanding"       # "expanding" or observation count

    def __post_init__(self) -> None:
        if self.rule not in ("gt", "lt"):
            raise ValueError(f"rule must be 'gt' or 'lt', got {self.rule!r}")
        if not 0 < self.pct < 100:
            raise ValueError(f"pct must be between 0 and 100, got {self.pct}")


def causal_percentile_rank(s: pd.Series, window: Union[str, int] = "expanding",
                           min_periods: int = 252) -> pd.Series:
    """Percentile rank within own history. At date t uses s[:t] inclusive."""
    clean = s.dropna()
    if clean.empty:
        return pd.Series(index=s.index, dtype=float)

    def _rank(arr: np.ndarray) -> float:
        return float((arr <= arr[-1]).sum()) / float(len(arr))

    if window == "expanding":
        ranks = clean.expanding(min_periods=min_periods).apply(_rank, raw=True)
    else:
        w = int(window)
        ranks = clean.rolling(w, min_periods=min(min_periods, w)).apply(_rank, raw=True)
    return ranks.reindex(s.index)


def hinge(ranks: pd.Series, rule: TriggerRule) -> pd.Series:
    """Firing strength in [0, 1]: zero at the threshold, one at the extreme.

    Retains the tail-only response of a binary trigger while keeping magnitude,
    which a plain z-score average discards.
    """
    p = rule.pct / 100.0
    if rule.rule == "gt":
        raw = (ranks - p) / (1.0 - p) if p < 1.0 else (ranks - p)
    else:
        raw = (p - ranks) / p if p > 0 else (p - ranks)
    return raw.clip(lower=0.0, upper=1.0)


def fired(ranks: pd.Series, rule: TriggerRule) -> pd.Series:
    p = rule.pct / 100.0
    out = ranks >= p if rule.rule == "gt" else ranks <= p
    return out.where(ranks.notna())


# --- inputs and aggregation ------------------------------------------------
@dataclass
class SentimentInput:
    """One input, held at its native frequency.

    Percentile ranks are computed on true observations. Carrying a weekly
    series onto daily dates first would turn a 260-observation five-year window
    into a one-year window and repeat each reading five times in the
    distribution. `fill_limit` governs how many business days the resulting
    rank is carried forward before the input leaves the denominator.
    """

    id: str
    series: pd.Series
    cluster: str
    sell: Optional[TriggerRule] = None
    buy: Optional[TriggerRule] = None
    label: str = ""
    is_substitute: bool = False
    min_periods: int = 252
    fill_limit: int = 0

    def ranks_for(self, side: str) -> pd.Series:
        rule = self.sell if side == "sell" else self.buy
        if rule is None:
            return pd.Series(index=self.series.index, dtype=float)
        return causal_percentile_rank(self.series, rule.window, self.min_periods)

    def ranks_on(self, side: str, index: pd.Index) -> pd.Series:
        ranks = self.ranks_for(side)
        if self.fill_limit > 0 and not ranks.dropna().empty:
            return ranks.reindex(index.union(ranks.index)).ffill(
                limit=self.fill_limit).reindex(index)
        return ranks.reindex(index)


@dataclass
class AggregationResult:
    reading: pd.Series
    denominator: pd.Series
    fired_count: pd.Series
    per_input_hinge: pd.DataFrame
    per_input_fired: pd.DataFrame
    per_input_rank: pd.DataFrame
    dropped: pd.Series
    mode: str = ""
    side: str = ""


class SentimentEngine:
    def __init__(self, inputs: Sequence[SentimentInput],
                 cluster_weights: Optional[Dict[str, float]] = None):
        ids = [i.id for i in inputs]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate input ids: {dupes}")
        self.inputs = list(inputs)
        self.cluster_weights = cluster_weights

    def _side_inputs(self, side: str) -> List[SentimentInput]:
        return [i for i in self.inputs if (i.sell if side == "sell" else i.buy) is not None]

    def published_denominator(self, side: str) -> int:
        return len(self._side_inputs(side))

    def compute(self, side: str, mode: str = "improved",
                index: Optional[pd.Index] = None) -> AggregationResult:
        if side not in ("sell", "buy"):
            raise ValueError("side must be 'sell' or 'buy'")
        if mode not in ("replica", "improved"):
            raise ValueError("mode must be 'replica' or 'improved'")

        members = self._side_inputs(side)
        if not members:
            raise ValueError(f"no inputs carry a {side} rule")

        if index is None:
            index = members[0].series.index
            for m in members[1:]:
                index = index.union(m.series.index)
            index = index.sort_values()

        ranks, hinges, fires = {}, {}, {}
        for inp in members:
            rule = inp.sell if side == "sell" else inp.buy
            r = inp.ranks_on(side, index)
            ranks[inp.id] = r
            hinges[inp.id] = hinge(r, rule)
            fires[inp.id] = fired(r, rule)

        rank_df = pd.DataFrame(ranks, index=index)
        hinge_df = pd.DataFrame(hinges, index=index)
        fire_df = pd.DataFrame(fires, index=index)

        available = rank_df.notna()
        denominator = available.sum(axis=1)
        fired_count = fire_df.astype("boolean").fillna(False).astype(bool).sum(axis=1)

        if mode == "replica":
            # Divide by the live denominator, not HSBC's fixed 20 or 13, so an
            # unavailable input shows as a smaller denominator rather than as
            # falling sentiment.
            reading = fired_count / denominator.replace(0, np.nan)
        else:
            reading = self._cluster_weighted(hinge_df, available, members)

        dropped = available.apply(
            lambda row: sorted([c for c in available.columns if not row[c]]), axis=1)

        return AggregationResult(
            reading=reading, denominator=denominator, fired_count=fired_count,
            per_input_hinge=hinge_df, per_input_fired=fire_df, per_input_rank=rank_df,
            dropped=dropped, mode=mode, side=side)

    def _cluster_weighted(self, hinge_df: pd.DataFrame, available: pd.DataFrame,
                          members: Sequence[SentimentInput]) -> pd.Series:
        """Mean hinge within cluster, then across clusters.

        Stops a theme with many correlated representatives from dominating the
        reading. Clusters with nothing available drop out and the remaining
        weights renormalise.
        """
        by_cluster: Dict[str, List[str]] = {}
        for m in members:
            by_cluster.setdefault(m.cluster, []).append(m.id)

        cluster_means, cluster_live = {}, {}
        for cluster, ids in by_cluster.items():
            sub = hinge_df[ids].where(available[ids])
            cluster_means[cluster] = sub.mean(axis=1, skipna=True)
            cluster_live[cluster] = available[ids].any(axis=1)

        means = pd.DataFrame(cluster_means)
        live = pd.DataFrame(cluster_live)

        if self.cluster_weights:
            w = pd.Series({c: self.cluster_weights.get(c, 1.0) for c in means.columns})
        else:
            w = pd.Series(1.0, index=means.columns)

        weights = live.astype(float).mul(w, axis=1)
        total = weights.sum(axis=1).replace(0.0, np.nan)
        return (means.fillna(0.0) * weights).sum(axis=1) / total

    def compute_all(self, index: Optional[pd.Index] = None) -> Dict[str, AggregationResult]:
        out = {}
        for side in ("sell", "buy"):
            if not self._side_inputs(side):
                continue
            for mode in ("replica", "improved"):
                out[f"{side}_{mode}"] = self.compute(side, mode, index)
        return out


# --- bands -----------------------------------------------------------------
PUBLISHED_BANDS: List[tuple] = [
    ("No signal", 0.00, 0.20),
    ("Mild", 0.20, 0.30),
    ("Moderate", 0.30, 0.40),
    ("Strong", 0.40, 0.50),
    ("Extreme", 0.50, 1.01),
]


def label_bands(reading: pd.Series, bands: Sequence[tuple] = PUBLISHED_BANDS) -> pd.Series:
    def _label(v: float) -> Optional[str]:
        if pd.isna(v):
            return None
        for name, lo, hi in bands:
            if lo <= v < hi:
                return name
        return bands[-1][0]

    return reading.map(_label)


def bands_from_thresholds(thresholds: Dict[str, float]) -> List[tuple]:
    """Build band edges from calibrated thresholds."""
    order = ["Mild", "Moderate", "Strong", "Extreme"]
    edges, lo = [], 0.0
    for name in order:
        hi = thresholds.get(name)
        if hi is None or pd.isna(hi):
            continue
        edges.append(("No signal" if not edges else order[len(edges) - 1], lo, hi))
        lo = hi
    edges.append((order[-1], lo, 1.01))
    return edges
