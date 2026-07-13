"""
MockProvider - base class for all data providers.

Every provider (snapshot, blpapi) subclasses this and implements get_releases(),
returning a list of dicts on the shared contract:

  event, region, pillar, theme, impact, assets, higher_is, unit,
  date ("%d %b %Y"), date_iso, consensus, actual, previous, status

The base class returns an empty calendar, so if a subclass ever fails to load it
degrades to "no releases" rather than crashing the dashboard.
"""

from __future__ import annotations


class MockProvider:
    def __init__(self, config: dict | None = None, *args, **kwargs):
        self.config = config or {}

    def get_releases(self):
        # Base behaviour: no calendar. Subclasses override this.
        return []
