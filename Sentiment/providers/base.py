"""Series specifications and the provider contract."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

VALID_SOURCES = ("bloomberg", "macrobond", "derived")
# semimonthly covers Bloomberg SHORT_INT, which reports around 24 times a year.
VALID_FREQ = ("daily", "weekly", "semimonthly", "monthly")

STALENESS_LIMIT_DAYS: Dict[str, int] = {
    "daily": 7, "weekly": 21, "semimonthly": 45, "monthly": 60}


@dataclass(frozen=True)
class SeriesSpec:
    """One vendor series."""

    source: str
    code: str
    field: str = "PX_LAST"
    frequency: str = "daily"
    release_lag_days: int = 0
    label: str = ""

    def __post_init__(self) -> None:
        if self.source not in VALID_SOURCES:
            raise ValueError(f"unknown source {self.source!r}")
        if self.frequency not in VALID_FREQ:
            raise ValueError(f"unknown frequency {self.frequency!r}")

    @property
    def key(self) -> str:
        safe = self.code.replace(" ", "_").replace("/", "-")
        return f"{self.source}__{safe}__{self.field}"


@dataclass
class FetchResult:
    spec: SeriesSpec
    series: pd.Series
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.series.empty

    def summary(self) -> Dict[str, object]:
        if self.series.empty:
            return {"code": self.spec.code, "ok": False, "error": self.error, "n": 0}
        last = self.series.index[-1].date()
        return {
            "code": self.spec.code,
            "ok": self.ok,
            "error": self.error,
            "n": int(len(self.series)),
            "first": str(self.series.index[0].date()),
            "last": str(last),
            "staleness_days": (dt.date.today() - last).days,
        }


class Provider:
    """Interface implemented by each vendor provider."""

    name = "base"

    def fetch(self, spec: SeriesSpec, start: dt.date, end: dt.date) -> FetchResult:
        raise NotImplementedError

    def close(self) -> None:
        pass


def apply_release_lag(series: pd.Series, lag_days: int) -> pd.Series:
    """Stamp observations at the date they became public.

    Without this, a CFTC reading dated Tuesday is treated as known on Tuesday
    when it is not published until Friday, which puts a look-ahead into any
    backtest.
    """
    if lag_days <= 0 or series.empty:
        return series
    out = series.copy()
    out.index = pd.to_datetime(out.index) + pd.Timedelta(days=lag_days)
    return out


def is_stale(series: pd.Series, frequency: str, asof: Optional[dt.date] = None) -> bool:
    if series.empty:
        return True
    asof = asof or dt.date.today()
    limit = STALENESS_LIMIT_DAYS.get(frequency, 60)
    return (asof - series.index[-1].date()).days > limit


def to_weekly(series: pd.Series) -> pd.Series:
    """Collapse a forward-filled daily series to its true weekly observations.

    Macrobond stores CFTC net series daily by forward fill while the components
    stay weekly. Percentiles and differences computed on the daily version
    count each weekly reading about five times.
    """
    if series.empty:
        return series
    return series.resample("W-FRI").last().dropna()
