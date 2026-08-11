"""On-disk cache for vendor series.

Uses parquet where pyarrow or fastparquet is installed and falls back to CSV
otherwise. At this volume, roughly sixty series of a few thousand rows, the
difference is immaterial and CSV avoids a dependency that needs approval on a
managed machine.

Keep the cache on a local unsynced path. OneDrive-backed folders corrupt on
concurrent write.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Dict, List, Optional

import pandas as pd

from .base import SeriesSpec

log = logging.getLogger(__name__)


def _detect_backend() -> str:
    for module in ("pyarrow", "fastparquet"):
        try:
            __import__(module)
            return "parquet"
        except ImportError:
            continue
    return "csv"


BACKEND = _detect_backend()
EXTENSION = ".parquet" if BACKEND == "parquet" else ".csv"


class SeriesCache:
    def __init__(self, root: str, backend: Optional[str] = None):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.backend = backend or BACKEND
        self.extension = ".parquet" if self.backend == "parquet" else ".csv"
        self._meta_path = os.path.join(self.root, "_manifest.json")
        if self.backend == "csv":
            log.info("parquet engine not available, caching to CSV in %s", self.root)

    def path_for(self, spec: SeriesSpec) -> str:
        safe = spec.key
        return os.path.join(self.root, f"{safe}{self.extension}")

    def _legacy_path(self, spec: SeriesSpec) -> str:
        other = ".csv" if self.extension == ".parquet" else ".parquet"
        return os.path.join(self.root, f"{spec.key}{other}")

    def read(self, spec: SeriesSpec) -> Optional[pd.Series]:
        path = self.path_for(spec)
        if not os.path.exists(path):
            # Pick up a cache written before the backend changed.
            legacy = self._legacy_path(spec)
            path = legacy if os.path.exists(legacy) else path
            if not os.path.exists(path):
                return None
        try:
            df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
        except Exception as exc:
            log.warning("could not read cache %s: %s", path, exc)
            return None
        if df.empty or "value" not in df.columns or "date" not in df.columns:
            return None
        s = df.set_index("date")["value"]
        s.index = pd.to_datetime(s.index)
        return s.sort_index()

    def write(self, spec: SeriesSpec, series: pd.Series) -> None:
        if series.empty:
            return
        df = pd.DataFrame({"date": pd.to_datetime(series.index), "value": series.values})
        df = df.drop_duplicates(subset="date", keep="last").sort_values("date")
        path = self.path_for(spec)
        try:
            if self.backend == "parquet":
                df.to_parquet(path, index=False)
            else:
                df.to_csv(path, index=False)
        except ImportError:
            # A parquet engine disappeared between init and write.
            self.backend, self.extension = "csv", ".csv"
            df.to_csv(self.path_for(spec), index=False)

    def merge(self, spec: SeriesSpec, fresh: pd.Series) -> pd.Series:
        existing = self.read(spec)
        if existing is None or existing.empty:
            combined = fresh
        elif fresh.empty:
            combined = existing
        else:
            combined = pd.concat([existing, fresh])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        self.write(spec, combined)
        return combined

    def last_date(self, spec: SeriesSpec) -> Optional[dt.date]:
        s = self.read(spec)
        if s is None or s.empty:
            return None
        return s.index[-1].date()

    def update_manifest(self, entries: List[Dict[str, object]]) -> None:
        payload = {"updated": dt.datetime.now().isoformat(timespec="seconds"),
                   "backend": self.backend, "series": entries}
        with open(self._meta_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)

    def manifest(self) -> Dict[str, object]:
        if not os.path.exists(self._meta_path):
            return {}
        try:
            with open(self._meta_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
