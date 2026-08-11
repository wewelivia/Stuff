"""Builds SentimentInput objects from config and vendor data.

Each input declares its series and a transform recipe in sentiment_tickers.yaml.
This module resolves the series, applies the recipe and hands the result to the
engine. Inputs that cannot be built are skipped and reported rather than
raising, so one missing series does not take the tab down.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

import sentiment_engine as eng
from providers import SeriesSpec, SeriesStore

log = logging.getLogger(__name__)


@dataclass
class BuildReport:
    built: List[str] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    substitutes: List[str] = field(default_factory=list)
    degraded: Dict[str, str] = field(default_factory=dict)
    # Inputs that build but can never produce a percentile, which otherwise
    # shows in the tab as a silent "unavailable" with no explanation.
    warnings: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {"built": self.built, "skipped": self.skipped,
                "substitutes": self.substitutes, "degraded": self.degraded,
                "warnings": self.warnings,
                "n_built": len(self.built), "n_skipped": len(self.skipped)}


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


OBS_PER_YEAR = {"daily": 252, "weekly": 52, "semimonthly": 24, "monthly": 12}

# Observations required before a percentile is meaningful, per frequency.
# Roughly two years in each case.
MIN_PERIODS = {"daily": 252, "weekly": 104, "semimonthly": 48, "monthly": 24}

# Business days a percentile rank may be carried forward before the input
# leaves the denominator. Weekly data must survive four intervening days,
# otherwise a weekly input is absent on four days in five and the reading looks
# as though inputs have stopped firing. Daily inputs get a short bridge for
# holidays and vendors that publish a day late. Limits sit inside the staleness
# cutoffs in providers.base, so a genuinely dead series still drops out.
FILL_LIMIT_BDAYS = {"daily": 3, "weekly": 10, "semimonthly": 20, "monthly": 45}


def _rule(spec: Optional[dict], freq: str = "daily") -> Optional[eng.TriggerRule]:
    """Rolling-year windows convert on the input's own frequency. A 5-year
    window is 1260 daily observations but only 260 weekly ones."""
    if not spec:
        return None
    window = spec.get("window", "expanding")
    if isinstance(window, dict):
        years = window.get("rolling_years")
        per_year = OBS_PER_YEAR.get(freq, 252)
        window = int(round(years * per_year)) if years else "expanding"
    return eng.TriggerRule(rule=spec["rule"], pct=float(spec["pct"]), window=window)


def _spec_from(entry: dict, default_freq: str = "daily") -> SeriesSpec:
    return SeriesSpec(
        source=entry.get("source", "bloomberg"),
        code=entry["code"],
        field=entry.get("field", "PX_LAST"),
        frequency=entry.get("frequency", default_freq),
        release_lag_days=int(entry.get("release_lag_days", 0)),
        label=entry.get("label", ""))


class SentimentBuilder:
    def __init__(self, config: dict, store: SeriesStore):
        self.config = config
        self.store = store
        self.defaults = config.get("defaults", {})
        self.clusters = self._invert_clusters(config.get("clusters", {}))

    @staticmethod
    def _invert_clusters(clusters: Dict[str, List[str]]) -> Dict[str, str]:
        out = {}
        for cluster, members in clusters.items():
            for m in members:
                out[m] = cluster
        return out

    def build(self, refresh: bool = True) -> Tuple[List[eng.SentimentInput], BuildReport]:
        report = BuildReport()
        inputs: List[eng.SentimentInput] = []

        for entry in self.config.get("inputs", []):
            iid = entry["id"]
            try:
                series = self._build_series(entry, refresh)
            except Exception as exc:
                report.skipped[iid] = f"{type(exc).__name__}: {exc}"
                log.warning("input %s skipped: %s", iid, exc)
                continue

            if series is None or series.dropna().empty:
                report.skipped[iid] = "no data"
                continue

            freq = entry.get("frequency", "daily")
            min_periods = MIN_PERIODS.get(freq, 252)

            # A rolling window longer than the series can ever hold produces no
            # percentile at all, which shows up as a permanently missing input.
            available = len(series.dropna())
            for side, rule in (("sell", entry.get("sell")), ("buy", entry.get("buy"))):
                converted = _rule(rule, freq)
                if converted is None or converted.window == "expanding":
                    continue
                if converted.window > available:
                    msg = (f"{side} window is {converted.window} observations but only "
                           f"{available} exist at {freq} frequency, so no percentile can "
                           f"be computed and the input will never fire")
                    report.warnings[iid] = msg
                    log.warning("%s: %s", iid, msg)
                elif available < min_periods:
                    msg = (f"only {available} observations, below the {min_periods} "
                           f"needed before a percentile is meaningful")
                    report.warnings.setdefault(iid, msg)
                    log.warning("%s: %s", iid, msg)

            inputs.append(eng.SentimentInput(
                id=iid, series=series, cluster=self.clusters.get(iid, iid),
                sell=_rule(entry.get("sell"), freq), buy=_rule(entry.get("buy"), freq),
                label=entry.get("ui_label") or entry.get("name", iid),
                is_substitute=bool(entry.get("is_substitute")),
                min_periods=min_periods,
                fill_limit=FILL_LIMIT_BDAYS.get(freq, 0)))

            report.built.append(iid)
            if entry.get("is_substitute"):
                report.substitutes.append(iid)
            if entry.get("status") == "PARTIAL":
                report.degraded[iid] = entry.get("degraded_mode", "partial legs")

        return inputs, report

    # --- series construction ------------------------------------------
    def _build_series(self, entry: dict, refresh: bool) -> Optional[pd.Series]:
        recipe = entry.get("compute", {}) or {}
        op = recipe.get("op", "level")
        freq = entry.get("frequency", "daily")

        raw = self._resolve_inputs(entry, refresh)
        if not raw:
            return None

        base = self._apply_op(op, raw, entry, recipe)
        if base is None or base.dropna().empty:
            return None

        if recipe.get("smooth"):
            sm = recipe["smooth"]
            base = eng.smooth(base, float(sm.get("months", 1)), freq)

        post = recipe.get("post") or {}
        if post.get("op") == "diff":
            base = eng.diff_horizon(base, _months(post.get("horizon", "3M")), freq)
        elif recipe.get("horizon") and op == "diff":
            pass

        # Positioning inputs where the level is structural rather than
        # informational must not be surfaced as a level.
        mb = entry.get("macrobond") or {}
        if mb.get("require_difference") and not (post.get("op") == "diff" or op == "diff"):
            base = eng.diff_horizon(base, 3.0, freq)

        return base

    def _resolve_inputs(self, entry: dict, refresh: bool) -> Dict[str, pd.Series]:
        out: Dict[str, pd.Series] = {}
        freq = entry.get("frequency", "daily")

        mb = entry.get("macrobond")
        if mb:
            for role in ("series", "long", "short", "open_interest"):
                code = mb.get(role)
                if not code:
                    continue
                spec = SeriesSpec(source="macrobond", code=code, frequency=freq,
                                  release_lag_days=int(mb.get("release_lag_days", 0)))
                s = self.store.get(spec, refresh=refresh)
                if not s.empty:
                    out[role] = s

            basket = mb.get("basket") or []
            for leg in basket:
                for role in ("long", "short", "oi"):
                    code = leg.get(role)
                    if not code:
                        continue
                    spec = SeriesSpec(source="macrobond", code=code, frequency=freq,
                                      release_lag_days=int(mb.get("release_lag_days", 0)))
                    s = self.store.get(spec, refresh=refresh)
                    if not s.empty:
                        out[f"{leg['key']}__{role}"] = s

        for series_entry in entry.get("series", []) or []:
            role = series_entry.get("role", "primary")
            field_name = series_entry.get("field", self.defaults.get("field", "PX_LAST"))
            candidates = series_entry.get("candidates") or []
            collected = []
            for code in candidates:
                spec = SeriesSpec(source="bloomberg", code=code, field=field_name,
                                  frequency=freq)
                s = self.store.get(spec, refresh=refresh)
                if s.empty:
                    continue
                if role.endswith("basket") or role == "basket":
                    collected.append(s.rename(code))
                else:
                    out[role] = s
                    break
            if collected:
                # Keep members individually as well as the mean: PCA and RSI
                # need the constituents, everything else wants the average.
                for member in collected:
                    out[f"{role}{MEMBER_SEP}{member.name}"] = member
                out[role] = pd.concat(collected, axis=1).mean(axis=1)

        return out

    def _apply_op(self, op: str, raw: Dict[str, pd.Series],
                  entry: dict, recipe: dict) -> Optional[pd.Series]:
        freq = entry.get("frequency", "daily")
        mb = entry.get("macrobond") or {}

        if mb.get("basket"):
            return self._basket_net(raw, mb)

        if {"long", "short"} <= set(raw):
            net = raw["long"] - raw["short"]
            if mb.get("normalise_by_open_interest") and "open_interest" in raw:
                net = net / raw["open_interest"].replace(0.0, np.nan)
            return net

        if op == "level":
            return _pick(raw, "series", "primary") if _pick(raw, "series", "primary") is not None \
                else _first(raw)

        if op == "diff":
            base = _pick(raw, "series", "primary")
            if base is None:
                base = _first(raw)
            if base is None:
                return None
            return eng.diff_horizon(base, _months(recipe.get("horizon", "3M")), freq)

        if op == "ratio":
            num, den = raw.get("numerator"), raw.get("denominator")
            if num is None or den is None:
                return None
            return num / den.replace(0.0, np.nan)

        if op == "rolling_beta":
            dep = _pick(raw, "dependent", "dependent_basket")
            fac = raw.get("factor")
            if dep is None or fac is None:
                return None
            return eng.rolling_beta(dep, fac, int(recipe.get("window_days", 20)))

        if op == "mean_of_zscores":
            comps = {k: v for k, v in raw.items()
                     if MEMBER_SEP not in k and not k.endswith("__oi")}
            return eng.mean_of_z(comps, invert=tuple(mb.get("invert", ())))

        if op == "mean_rsi":
            window = int(recipe.get("rsi_window_days", 14))
            comps = _members(raw) or {k: v for k, v in raw.items() if MEMBER_SEP not in k}
            frame = pd.DataFrame({k: eng.rsi(v, window) for k, v in comps.items()})
            return frame.mean(axis=1, skipna=True)

        if op == "pca_first_component":
            comps = _members(raw) or {k: v for k, v in raw.items() if MEMBER_SEP not in k}
            frame = pd.DataFrame(comps).dropna(how="all")
            if frame.shape[1] < 2:
                return None
            return eng.first_principal_component(frame, int(recipe.get("window_days", 252)))

        if op == "vol_target_allocation":
            und = raw.get("underlying")
            if und is None:
                und = _first(raw)
            if und is None:
                return None
            return eng.vol_target_weight(und, float(recipe.get("target_vol", 0.10)),
                                         int(recipe.get("vol_window_days", 60)))

        if op == "inverse_vol_allocation":
            sleeves = {k: v for k, v in raw.items() if k.startswith("sleeve")}
            if len(sleeves) < 2:
                return None
            weights = eng.inverse_vol_weights(sleeves, int(recipe.get("vol_window_days", 60)))
            equity_cols = [c for c in weights.columns if "equity" in c]
            return weights[equity_cols[0]] if equity_cols else weights.iloc[:, 0]

        if op == "trend_replication":
            und = raw.get("underlying")
            if und is None:
                und = _first(raw)
            if und is None:
                return None
            return eng.trend_replication(und, tuple(recipe.get("lookbacks_days", (21, 63, 252))),
                                         int(recipe.get("vol_scale_window_days", 60)))

        if op == "zscore_composite":
            comps = {k: v for k, v in raw.items() if MEMBER_SEP not in k}
            return eng.mean_of_z(comps, window=int(recipe.get("window_days", 252)))

        return _first(raw)

    def _basket_net(self, raw: Dict[str, pd.Series], mb: dict) -> Optional[pd.Series]:
        """Sign-adjusted mean of z-scored net positioning across basket legs."""
        legs = {}
        for leg in mb.get("basket", []):
            key = leg["key"]
            long_s, short_s = raw.get(f"{key}__long"), raw.get(f"{key}__short")
            if long_s is None or short_s is None:
                continue
            net = long_s - short_s
            oi = raw.get(f"{key}__oi")
            if mb.get("normalise_by_open_interest") and oi is not None:
                net = net / oi.replace(0.0, np.nan)
            legs[key] = net * float(leg.get("risk_sign", 1))
        if not legs:
            return None
        return eng.mean_of_z(legs)


MEMBER_SEP = "::"


def _first(raw: Dict[str, pd.Series]) -> Optional[pd.Series]:
    for key, value in raw.items():
        if MEMBER_SEP not in key:
            return value
    return next(iter(raw.values())) if raw else None


def _pick(raw: Dict[str, pd.Series], *keys: str) -> Optional[pd.Series]:
    """First present key. Cannot use `or` chaining: a pandas Series has no
    unambiguous truth value."""
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return None


def _members(raw: Dict[str, pd.Series], role: str = "basket") -> Dict[str, pd.Series]:
    """Individual basket constituents, for ops that need them separately."""
    prefix = f"{role}{MEMBER_SEP}"
    return {k.split(MEMBER_SEP, 1)[1]: v for k, v in raw.items() if k.startswith(prefix)}


def _months(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).upper().replace("M", "")
    try:
        return float(text)
    except ValueError:
        return 3.0
