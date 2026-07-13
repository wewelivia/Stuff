"""
SnapshotProvider - on-demand ("snapshot") data source for the View Challenges dashboard.

WHY THIS EXISTS
---------------
Reliable economic *consensus* data behind a live API is either paywalled
(FMP Premium, EODHD, FactSet) or tied to a terminal (Bloomberg). For now we run
the dashboard ON DEMAND instead of live: a point-in-time snapshot of the release
data lives in a local JSON file and is served through the SAME get_releases()
contract every other provider uses. Nothing downstream changes - challenge_engine.py,
house_view.yaml and the HTML are untouched. Logic and format are preserved exactly.

REFRESH WORKFLOW (the whole point)
----------------------------------
The snapshot file (releases_snapshot.json) is regenerated on demand:
  1. Ask Claude: "refresh the House View snapshot".
  2. Claude returns an updated releases_snapshot.json - latest prints, consensus,
     previous and status, with a `source` per release so provenance is visible.
  3. Drop the file in beside this provider and refresh the dashboard.
No API key, no network call, no paywall. As current as your last refresh.

CONTRACT
--------
Each object in releases_snapshot.json carries the standard get_releases() fields:
  event, region, pillar, theme, impact, assets, higher_is, unit,
  date ("%d %b %Y"), date_iso, consensus, actual, previous, status
Records are returned sorted by date_iso DESCENDING, exactly like the live providers.
Released items carry actual+consensus+previous; Upcoming items carry the date and
leave consensus/actual null until the print lands.
"""

from __future__ import annotations

import json
import os

try:
    # Subclass Mock so the non-calendar dashboard sections still render,
    # mirroring FmpProvider(MockProvider). If the import path differs in your
    # tree, this still imports cleanly and falls back to a plain object.
    from providers.mock_provider import MockProvider
except Exception:  # pragma: no cover
    MockProvider = object


# The get_releases() output contract, per the project spec.
CONTRACT_FIELDS = (
    "event", "region", "pillar", "theme", "impact", "assets",
    "higher_is", "unit", "date", "date_iso", "consensus",
    "actual", "previous", "status",
)


class SnapshotProvider(MockProvider):
    """Serve get_releases() from a local point-in-time JSON snapshot."""

    def __init__(self, config: dict | None = None, *args, **kwargs):
        try:
            super().__init__(config, *args, **kwargs)
        except Exception:
            # MockProvider may have a different __init__ signature in your tree;
            # the snapshot path resolution below does not depend on it.
            pass

        self.config = config or {}
        snap_cfg = (self.config.get("snapshot") or {})

        # Resolve the snapshot path:
        #   config snapshot.path  >  env HV_SNAPSHOT_PATH  >  file next to this module
        self.snapshot_path = (
            snap_cfg.get("path")
            or os.environ.get("HV_SNAPSHOT_PATH")
            or os.path.join(os.path.dirname(__file__), "releases_snapshot.json")
        )
        self.as_of = None

    # -- the only "live" method ---------------------------------------------
    def get_releases(self):
        releases = self._load_snapshot()

        # Tolerate missing optional fields so the engine never KeyErrors.
        for r in releases:
            for f in CONTRACT_FIELDS:
                r.setdefault(f, None)

        # Same ordering guarantee as the live providers.
        releases.sort(key=lambda r: (r.get("date_iso") or ""), reverse=True)
        return releases

    # -- helpers -------------------------------------------------------------
    def _load_snapshot(self):
        if not os.path.exists(self.snapshot_path):
            # Graceful: an empty calendar rather than a crash, matching the
            # "never break the dashboard" behaviour of the other providers.
            return []

        with open(self.snapshot_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        # Accept either a bare list, or {"as_of": "...", "releases": [...]}.
        if isinstance(data, dict):
            self.as_of = data.get("as_of")
            return list(data.get("releases", []))
        return list(data)
