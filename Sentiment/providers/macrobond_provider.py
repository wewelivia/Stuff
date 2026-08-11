"""Macrobond provider. COM connection to the local desktop application.

Requires the Macrobond application to be running and signed in; it hosts the
COM server. Uses macrobond_data_api where installed, otherwise raw win32com.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import pandas as pd

from .base import FetchResult, Provider, SeriesSpec


class MacrobondProvider(Provider):
    name = "macrobond"

    def __init__(self) -> None:
        self._api = None
        self._ctx = None
        self._db = None
        self._mode = None

        try:
            from macrobond_data_api.com import ComClient
            self._ctx = ComClient()
            self._api = self._ctx.__enter__()
            self._mode = "api"
            return
        except Exception:
            self._ctx = None

        import win32com.client
        self._db = win32com.client.Dispatch("Macrobond.Connection").Database
        self._mode = "com"

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.__exit__(None, None, None)
            except Exception:
                pass

    def fetch(self, spec: SeriesSpec, start: dt.date, end: dt.date) -> FetchResult:
        try:
            dates, values = self._raw(spec.code)
        except Exception as exc:
            return FetchResult(spec, pd.Series(dtype=float), f"{type(exc).__name__}: {exc}")

        if not dates or not values:
            return FetchResult(spec, pd.Series(dtype=float), "no observations")

        idx = pd.to_datetime([_as_date(d) for d in dates])
        s = pd.Series(values, index=idx).dropna().sort_index()
        s = s.loc[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
        if s.empty:
            return FetchResult(spec, s, "no observations in range")
        return FetchResult(spec, s, None)

    def _raw(self, code: str):
        if self._mode == "api":
            series = self._api.get_one_series(code)
            if getattr(series, "is_error", False):
                raise RuntimeError(getattr(series, "error_message", "unknown error"))
            return list(series.dates), list(series.values)

        series = self._db.FetchOneSeries(code)
        if series.IsError:
            raise RuntimeError(series.ErrorMessage)
        return list(series.DatesAtStartOfPeriod), list(series.Values)

    def fetch_many(self, specs: List[SeriesSpec], start: dt.date, end: dt.date) -> List[FetchResult]:
        return [self.fetch(s, start, end) for s in specs]


def _as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.datetime.fromisoformat(str(value)[:10]).date()
