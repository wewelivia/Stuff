"""Data providers and the series store."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, List, Optional

import pandas as pd

from .base import (FetchResult, Provider, SeriesSpec, apply_release_lag,
                   is_stale, to_weekly)
from .cache import SeriesCache

log = logging.getLogger(__name__)

__all__ = ["SeriesSpec", "FetchResult", "Provider", "SeriesCache", "SeriesStore",
           "apply_release_lag", "is_stale", "to_weekly"]


class SeriesStore:
    """Routes requests to the right provider, caches, applies release lags.

    Providers are created lazily so the store works on a machine with only one
    vendor available. A series that cannot be fetched returns empty rather than
    raising, and the caller decides whether to drop the input.
    """

    def __init__(self, cache_dir: str, history_start: dt.date,
                 enable_bloomberg: bool = True, enable_macrobond: bool = True):
        self.cache = SeriesCache(cache_dir)
        self.history_start = history_start
        self._providers: Dict[str, Optional[Provider]] = {}
        self._enabled = {"bloomberg": enable_bloomberg, "macrobond": enable_macrobond}
        self._errors: Dict[str, str] = {}

    def _provider(self, source: str) -> Optional[Provider]:
        if source in self._providers:
            return self._providers[source]
        if not self._enabled.get(source, False):
            self._providers[source] = None
            return None

        try:
            if source == "bloomberg":
                from .bloomberg_provider import BloombergProvider
                self._providers[source] = BloombergProvider()
            elif source == "macrobond":
                from .macrobond_provider import MacrobondProvider
                self._providers[source] = MacrobondProvider()
            else:
                self._providers[source] = None
        except Exception as exc:
            self._errors[source] = f"{type(exc).__name__}: {exc}"
            log.warning("provider %s unavailable: %s", source, exc)
            self._providers[source] = None
        return self._providers[source]

    def get(self, spec: SeriesSpec, refresh: bool = True,
            apply_lag: bool = True) -> pd.Series:
        cached = self.cache.read(spec)

        if refresh:
            last = self.cache.last_date(spec)
            start = self.history_start if last is None else last - dt.timedelta(days=30)
            provider = self._provider(spec.source)
            if provider is not None:
                result = provider.fetch(spec, start, dt.date.today())
                if result.ok:
                    cached = self.cache.merge(spec, result.series)
                elif result.error:
                    log.warning("fetch failed for %s: %s", spec.code, result.error)

        if cached is None or cached.empty:
            return pd.Series(dtype=float)

        out = cached
        if spec.frequency == "weekly":
            out = to_weekly(out)
        if apply_lag:
            out = apply_release_lag(out, spec.release_lag_days)
        return out

    def get_many(self, specs: List[SeriesSpec], refresh: bool = True) -> Dict[str, pd.Series]:
        return {s.code: self.get(s, refresh=refresh) for s in specs}

    def provider_status(self) -> Dict[str, object]:
        return {
            "enabled": dict(self._enabled),
            "loaded": {k: (v is not None) for k, v in self._providers.items()},
            "errors": dict(self._errors),
        }

    def close(self) -> None:
        for p in self._providers.values():
            if p is not None:
                p.close()
