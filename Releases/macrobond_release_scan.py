#!/usr/bin/env python3
"""
macrobond_release_scan.py
=========================

Extract the Macrobond release calendar for a given day, pull the latest reading
for every series attached to those releases, and rank what stands out on a
purely statistical basis (no consensus required).

Run this on the Windows machine that has the Macrobond application installed.

    pip install macrobond-data-api
    python macrobond_release_scan.py --probe                 # inspect metadata shape first
    python macrobond_release_scan.py                          # today, core regions
    python macrobond_release_scan.py --date 2026-08-14 --regions us,ea,gb
    python macrobond_release_scan.py --selftest               # offline maths check, no Macrobond

Scoring
-------
For each series we take the latest observation and ask how unusual it is against
its own history. Non-stationary series are scored on their period-on-period
change, stationary ones on the level. A trend test decides which.

  z          : (latest - mean of trailing window) / sd of trailing window
  trend_break: (mean of last 3 - mean of prior 12) / sd, in the same space
  pctile     : percentile of the latest observation within the trailing window

The trailing window excludes the latest observation so the print does not
contaminate its own benchmark.

Author: built for the House View macro dashboard.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Configuration defaults
# --------------------------------------------------------------------------

CORE_REGIONS = ["us", "ea", "de", "fr", "it", "es", "gb", "jp", "cn"]

# Region presets. Codes assumed to be ISO 3166-1 alpha-2 lowercase, plus 'ea'
# for the euro area. Run --list-regions to print what Macrobond actually uses
# and reconcile before trusting a long list.
DEVELOPED = [
    "us", "ca", "gb", "ea", "de", "fr", "it", "es", "nl", "be", "at", "ie",
    "pt", "gr", "fi", "lu", "ch", "no", "se", "dk", "is", "jp", "au", "nz",
    "sg", "hk", "kr", "tw", "il",
]

# Mainstream EM with a macro calendar that moves global rates or FX.
EM_MAJOR = [
    "cn", "in", "id", "my", "th", "ph", "br", "mx", "cl", "co", "pe",
    "pl", "cz", "hu", "ro", "tr", "sa", "ae", "za",
]

# Excluded by default under the 'all' preset.
AFRICA = [
    "dz", "ao", "bj", "bw", "bf", "bi", "cm", "cv", "cf", "td", "km", "cd",
    "cg", "ci", "dj", "eg", "gq", "er", "et", "ga", "gm", "gh", "gn", "gw",
    "ke", "ls", "lr", "ly", "mg", "mw", "ml", "mr", "mu", "ma", "mz", "na",
    "ne", "ng", "rw", "sn", "sc", "sl", "so", "ss", "sd", "sz", "tz", "tg",
    "tn", "ug", "zm", "zw",
]
# South Africa is deliberately NOT in the list above. It is the one African
# market with a policy calendar that feeds global EM rates pricing. Add "za"
# to AFRICA if you would rather drop it too.

FRONTIER = [
    "bd", "lk", "pk", "vn", "kz", "uz", "ge", "am", "az", "by", "ua", "rs",
    "hr", "si", "bg", "mk", "al", "ba", "md", "mn", "np", "kh", "la", "mm",
    "bh", "om", "jo", "lb", "iq", "ir", "ve", "ec", "bo", "py", "uy", "do",
    "gt", "cr", "pa", "sv", "hn", "ni", "jm", "tt", "cy", "mt", "ee", "lv",
    "lt", "sk",
]

REGION_PRESETS: Dict[str, Optional[List[str]]] = {
    "core": CORE_REGIONS,
    "dm": DEVELOPED,
    "dm-em": DEVELOPED + EM_MAJOR,
    "all": None,            # no include filter, exclusions still apply
}
DEFAULT_EXCLUDED = AFRICA + FRONTIER

# Words that suggest a series is the headline aggregate of its release rather
# than a component. Used to prioritise which series to score when a release
# carries hundreds of members.
HEADLINE_HINTS = (
    "total", "all items", "headline", "overall", "aggregate", "economy",
    "composite", "whole economy", "national", "seasonally adjusted",
)
COMPONENT_PENALTY = (
    "by region", "by state", "by county", "by province", "excluding",
    "detail", "sub-", "breakdown", "of which", "contribution",
)

# --- economic vs company releases -----------------------------------------
# Macrobond carries corporate earnings releases alongside economic ones. Until
# the probe tells us the definitive discriminator attribute, we exclude on
# structural markers first and fall back to wording.

# Presence of any of these attributes means the release is tied to an issuer.
COMPANY_MARKER_ATTRS = (
    "Company", "CompanyId", "CompanyName", "Security", "SecurityId",
    "Isin", "Cusip", "Sedol", "Ticker", "Exchange", "GicsSector", "GicsIndustry",
)

# Attributes that plausibly hold a release type. Checked against the patterns
# below. The probe prints the value distribution of each so we can confirm.
TYPE_LIKE_ATTRS = (
    "ReleaseType", "EntityType", "Class", "Category", "Group", "Kind", "Type",
)
COMPANY_TYPE_PATTERNS = ("compan", "corporate", "earning", "issuer", "security", "equity")
ECONOMIC_TYPE_PATTERNS = ("econom", "macro", "statistic", "indicator")

# Last-resort wording check on the description.
COMPANY_TEXT_PATTERNS = (
    "earnings", "quarterly results", "annual results", "interim results",
    "financial results", "trading update", "annual report", "10-q", "10-k",
    "results release", "profit", "dividend",
)

MIN_OBS_FOR_SCORE = 24          # need this many trailing points to compute z
TRAILING_WINDOW = 60            # observations used for mean/sd
TREND_TEST_RATIO = 0.35         # |mean(diff)| > ratio * sd(diff) => trending
DEFAULT_MAX_SERIES_PER_RELEASE = 5
DEFAULT_MAX_RELEASES = 400

CACHE_FILENAME = "macrobond_release_cache.json"
CACHE_TTL_HOURS = 20


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _first(value: Any) -> Any:
    """Macrobond metadata attributes are sometimes scalars, sometimes lists."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _as_datetime(value: Any) -> Optional[datetime]:
    """Coerce a metadata value into a timezone-aware UTC datetime."""
    value = _first(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        dt = None
        for parser in (
            lambda t: datetime.fromisoformat(t),
            lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
            lambda t: datetime.strptime(t, "%Y-%m-%d"),
            lambda t: datetime.strptime(t, "%d %b %Y"),
        ):
            try:
                dt = parser(text)
                break
            except (ValueError, TypeError):
                continue
        if dt is None:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _as_mapping(entity: Any) -> Dict[str, Any]:
    """
    entity_search returns a sequence of METADATA MAPPINGS, not Entity objects
    (SearchResult.entities is documented as "a sequence of the metadata of the
    entities found"). get_series and get_entities do return Entity objects with
    a .metadata mapping. Normalise both to a plain dict.
    """
    if isinstance(entity, dict):
        return entity
    md = getattr(entity, "metadata", None)
    if isinstance(md, dict):
        return md
    if md is not None:
        try:
            return dict(md)
        except Exception:
            pass
    try:
        return dict(entity)          # mapping-like but not a dict
    except Exception:
        return {}


def _meta(entity: Any, key: str, default: Any = None) -> Any:
    md = _as_mapping(entity)
    if key in md:
        return _first(md[key])
    # Macrobond metadata keys are case sensitive but be forgiving anyway
    lowered = {k.lower(): v for k, v in md.items()}
    if key.lower() in lowered:
        return _first(lowered[key.lower()])
    return default


def _entity_name(entity: Any) -> Optional[str]:
    """Name is a metadata key on search results, an attribute on entities."""
    for key in ("Name", "PrimName", "PrimaryName"):
        name = _meta(entity, key)
        if name:
            return str(name)
    name = getattr(entity, "name", None)
    return str(name) if name else None


def resolve_regions(spec: str, extra_excludes: str = "",
                    keep_excluded: bool = False) -> Tuple[Optional[List[str]], set]:
    """
    Turn a --regions value into (include list or None, exclusion set).

    Accepts a preset name ('core', 'dm', 'dm-em', 'all') or a comma separated
    list of codes. An explicit list is taken at face value: if you name a
    country you get it, exclusions do not silently override you.
    """
    spec = (spec or "core").strip().lower()
    excluded = set() if keep_excluded else set(DEFAULT_EXCLUDED)
    excluded |= {c.strip().lower() for c in (extra_excludes or "").split(",") if c.strip()}

    if spec in REGION_PRESETS:
        include = REGION_PRESETS[spec]
        if include is not None:
            include = [r for r in include if r not in excluded]
        return include, excluded

    include = [c.strip().lower() for c in spec.split(",") if c.strip()]
    # explicit list wins over the default exclusions
    excluded -= set(include)
    return include, excluded


def drop_excluded_regions(releases: List[Any], excluded: set,
                          verbose: bool = True) -> List[Any]:
    """Post-hoc region exclusion, needed for the 'all' preset."""
    if not excluded:
        return releases
    kept, dropped = [], {}
    for ent in releases:
        region = str(_meta(ent, "Region") or "").lower()
        if region and region in excluded:
            dropped[region] = dropped.get(region, 0) + 1
        else:
            kept.append(ent)
    if verbose and dropped:
        total = sum(dropped.values())
        top = ", ".join(f"{r}({n})" for r, n in
                        sorted(dropped.items(), key=lambda kv: -kv[1])[:10])
        print(f"  excluded {total} releases from {len(dropped)} excluded regions: {top}")
    return kept


def classify_release(entity: Any) -> Tuple[str, str]:
    """
    Return (kind, reason) where kind is 'economic', 'company' or 'unknown'.

    Ordered from most to least reliable: structural attributes that only an
    issuer-linked release would carry, then an explicit type attribute, then
    the wording of the description.
    """
    md = _as_mapping(entity)
    lowered = {k.lower(): v for k, v in md.items()}

    for attr in COMPANY_MARKER_ATTRS:
        if attr.lower() in lowered and lowered[attr.lower()] not in (None, "", []):
            return "company", f"has {attr}"

    for attr in TYPE_LIKE_ATTRS:
        raw = lowered.get(attr.lower())
        if raw in (None, "", []):
            continue
        text = str(_first(raw)).lower()
        if any(p in text for p in COMPANY_TYPE_PATTERNS):
            return "company", f"{attr}={text}"
        if any(p in text for p in ECONOMIC_TYPE_PATTERNS):
            return "economic", f"{attr}={text}"

    desc = str(_meta(entity, "Description") or _meta(entity, "FullDescription") or "").lower()
    if any(p in desc for p in COMPANY_TEXT_PATTERNS):
        return "company", "description wording"

    # An economic release is region-scoped and sourced from a statistical body.
    if _meta(entity, "Region"):
        return "economic", "has Region, no company markers"

    return "unknown", "no discriminator found"


def filter_releases_by_kind(releases: List[Any], kind: str,
                            verbose: bool = True) -> List[Any]:
    if kind == "all":
        return releases
    kept, dropped = [], {}
    for ent in releases:
        k, reason = classify_release(ent)
        # 'unknown' is kept when asking for economic: better a false positive we
        # can see in the report than a silently missing release.
        if k == kind or (kind == "economic" and k == "unknown"):
            kept.append(ent)
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
    if verbose and dropped:
        print(f"  excluded {sum(dropped.values())} non-{kind} releases:")
        for reason, n in sorted(dropped.items(), key=lambda kv: -kv[1])[:8]:
            print(f"    {n:>5}  {reason}")
    return kept


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

def _is_trending(levels: Sequence[float]) -> bool:
    """Crude stationarity test: does the level drift persistently?"""
    if len(levels) < 8:
        return False
    diffs = [b - a for a, b in zip(levels[:-1], levels[1:])]
    sd = statistics.pstdev(diffs)
    if sd == 0:
        return False
    return abs(statistics.fmean(diffs)) > TREND_TEST_RATIO * sd


def score_series(dates: Sequence[datetime], values: Sequence[Optional[float]]) -> Optional[Dict[str, Any]]:
    """Return the statistical profile of the latest observation, or None."""
    clean: List[Tuple[datetime, float]] = []
    for d, v in zip(dates, values):
        f = _safe_float(v)
        if f is not None:
            clean.append((d, f))
    if len(clean) < MIN_OBS_FOR_SCORE + 2:
        return None

    obs_dates = [d for d, _ in clean]
    levels = [v for _, v in clean]

    tail = levels[-(TRAILING_WINDOW + 2):]
    trending = _is_trending(tail)

    if trending:
        space = "change"
        series = [b - a for a, b in zip(levels[:-1], levels[1:])]
        series_dates = obs_dates[1:]
    else:
        space = "level"
        series = levels
        series_dates = obs_dates

    if len(series) < MIN_OBS_FOR_SCORE + 1:
        return None

    latest = series[-1]
    window = series[-(TRAILING_WINDOW + 1):-1]
    if len(window) < MIN_OBS_FOR_SCORE:
        return None

    mean = statistics.fmean(window)
    sd = statistics.pstdev(window)
    z = (latest - mean) / sd if sd > 0 else 0.0

    if len(series) >= 16:
        recent3 = statistics.fmean(series[-3:])
        prior12 = statistics.fmean(series[-15:-3])
        trend_break = (recent3 - prior12) / sd if sd > 0 else 0.0
    else:
        trend_break = 0.0

    below = sum(1 for w in window if w <= latest)
    pctile = 100.0 * below / len(window)

    return {
        "space": space,
        "latest_value": levels[-1],
        "latest_scored": latest,
        "obs_date": obs_dates[-1],
        "prev_value": levels[-2] if len(levels) > 1 else None,
        "window_mean": mean,
        "window_sd": sd,
        "z": z,
        "abs_z": abs(z),
        "trend_break": trend_break,
        "pctile": pctile,
        "n_obs": len(series),
        "scored_date": series_dates[-1],
    }


def verdict(profile: Dict[str, Any]) -> str:
    az = profile["abs_z"]
    direction = "above" if profile["z"] > 0 else "below"
    if az >= 3.0:
        strength = "Extreme"
    elif az >= 2.0:
        strength = "Notable"
    elif az >= 1.25:
        strength = "Mild"
    else:
        return "In line with own history"
    return f"{strength} print, {profile['z']:+.1f} sd {direction} trend"


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

class Cache:
    """
    On-disk JSON cache. Three separate stores with different lifetimes:

      releases        the calendar itself, refreshed daily
      release_members which series belong to a release, refreshed weekly since
                      membership is near-static. This is the big saving: it
                      removes one search per release per run.
      series_modified the last-modified timestamp of each series, used to ask
                      Macrobond for changes only.

    Keep this on a local unsynced path. OneDrive corrupts files that are
    rewritten frequently.
    """

    VERSION = 2

    def __init__(self, path: str, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self.data: Dict[str, Any] = {
            "version": self.VERSION,
            "releases": {},
            "release_members": {},
            "series_modified": {},
        }
        self.hits = {"calendar": 0, "members": 0}
        if enabled:
            self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if loaded.get("version") != self.VERSION:
                print(f"  cache version changed, starting fresh ({self.path})")
                return
            self.data = loaded
            for key in ("releases", "release_members", "series_modified"):
                self.data.setdefault(key, {})
        except Exception as exc:  # noqa: BLE001
            print(f"  cache unreadable, starting fresh: {exc}")

    def save(self) -> None:
        if not self.enabled:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, default=str)
            os.replace(tmp, self.path)      # atomic, survives an interrupted run
        except Exception as exc:  # noqa: BLE001
            print(f"  could not write the cache: {exc}")

    @staticmethod
    def _fresh(stamp: Optional[str], max_age: timedelta) -> bool:
        if not stamp:
            return False
        when = _as_datetime(stamp)
        if when is None:
            return False
        return datetime.now(timezone.utc) - when <= max_age

    # -- calendar ----------------------------------------------------------
    def get_calendar(self, key: str, ttl_hours: float) -> Optional[List[Any]]:
        if not self.enabled:
            return None
        entry = self.data["releases"].get(key)
        if entry and self._fresh(entry.get("fetched"), timedelta(hours=ttl_hours)):
            self.hits["calendar"] += 1
            return entry.get("rows", [])
        return None

    def put_calendar(self, key: str, rows: List[Any]) -> None:
        if not self.enabled:
            return
        self.data["releases"][key] = {
            "fetched": datetime.now(timezone.utc).isoformat(),
            "rows": [_as_mapping(r) for r in rows],
        }

    # -- release membership ------------------------------------------------
    def get_members(self, release: str, ttl_days: float) -> Optional[List[str]]:
        if not self.enabled:
            return None
        entry = self.data["release_members"].get(release)
        if entry and self._fresh(entry.get("fetched"), timedelta(days=ttl_days)):
            self.hits["members"] += 1
            return entry.get("series", [])
        return None

    def put_members(self, release: str, series: List[str]) -> None:
        if not self.enabled:
            return
        self.data["release_members"][release] = {
            "fetched": datetime.now(timezone.utc).isoformat(),
            "series": series,
        }

    # -- series last-modified ---------------------------------------------
    def get_modified(self, name: str) -> Optional[datetime]:
        if not self.enabled:
            return None
        return _as_datetime(self.data["series_modified"].get(name))

    def put_modified(self, name: str, when: Optional[datetime]) -> None:
        if not self.enabled:
            return
        self.data["series_modified"][name] = (when or datetime.now(timezone.utc)).isoformat()


# --------------------------------------------------------------------------
# Macrobond access
# --------------------------------------------------------------------------

def open_client(mode: str):
    if mode == "com":
        from macrobond_data_api.com import ComClient  # type: ignore
        return ComClient()
    from macrobond_data_api.web import WebClient  # type: ignore
    return WebClient()


# Macrobond returns "up to ~6000 entities in the search result" and guarantees
# "at least 2000 entities before the result is truncated". The exact ceiling is
# therefore not a number we can rely on, so we trust is_truncated and never
# infer truncation from the row count.
def _search(api, verbose: bool, **kwargs) -> Tuple[List[Any], bool]:
    """Run one entity_search. Returns (rows, was_truncated)."""
    try:
        result = api.entity_search(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"    search failed ({kwargs}): {exc}")
        return [], False
    rows = list(result)
    truncated = bool(getattr(result, "is_truncated", False))
    return rows, truncated


def fetch_releases(api, regions: Optional[List[str]], verbose: bool = True,
                   must_have: Optional[Dict[str, Any]] = None,
                   must_not_have: Optional[Dict[str, Any]] = None,
                   must_not_have_attributes: Optional[List[str]] = None) -> List[Any]:
    """
    All Release entities, optionally narrowed by region.

    Macrobond truncates a search at 2000 results, so we query one region at a
    time and merge. That keeps each call well under the cap and makes any
    remaining truncation visible per region rather than silently global.

    Extra filters are pushed into the search itself, which both shrinks the
    result and keeps us under the cap. Use these once the probe has confirmed
    the attribute that separates economic from company releases.
    """
    collected: Dict[str, Any] = {}
    truncated_regions: List[str] = []

    extra: Dict[str, Any] = {}
    if must_not_have:
        extra["must_not_have_values"] = must_not_have
    if must_not_have_attributes:
        extra["must_not_have_attributes"] = list(must_not_have_attributes)

    def absorb(rows: Iterable[Any]) -> None:
        for row in rows:
            name = _entity_name(row)
            if name:
                collected[name] = row

    targets = regions if regions else [None]
    for region in targets:
        values = dict(must_have or {})
        if region:
            values["Region"] = region
        kwargs: Dict[str, Any] = {"entity_types": ["Release"], **extra}
        if values:
            kwargs["must_have_values"] = values

        rows, truncated = _search(api, verbose, **kwargs)
        if not rows and region:
            # some releases may not carry a Region attribute at all
            rows, truncated = _search(api, verbose, entity_types=["Release"],
                                      text=region, **extra)
        absorb(rows)
        if truncated:
            truncated_regions.append(region or "all")
        if verbose:
            print(f"    {region or 'all'}: {len(rows)} releases"
                  f"{'  TRUNCATED' if truncated else ''}")

    if not collected:
        rows, _ = _search(api, verbose, entity_types=["Release"])
        absorb(rows)

    if verbose:
        print(f"  {len(collected)} unique release entities")
        if truncated_regions:
            print(f"  WARNING: Macrobond reported a truncated result for: "
                  f"{', '.join(truncated_regions)}. Narrow --regions, or push a "
                  f"filter server-side with --must-not-have-attr.")
    return list(collected.values())


def releases_on_day(releases: Iterable[Any], target: date, tz_offset_hours: float) -> List[Dict[str, Any]]:
    """Filter release entities to those whose last or next event falls on `target`."""
    shift = timedelta(hours=tz_offset_hours)
    day_start = datetime.combine(target, time.min, tzinfo=timezone.utc) - shift
    day_end = day_start + timedelta(days=1)

    hits: List[Dict[str, Any]] = []
    for ent in releases:
        name = _entity_name(ent)
        if not name:
            continue
        last = _as_datetime(_meta(ent, "LastReleaseEventTime"))
        nxt = _as_datetime(_meta(ent, "NextReleaseEventTime"))

        status = None
        event_time = None
        if last is not None and day_start <= last < day_end:
            status = "Released"
            event_time = last
        elif nxt is not None and day_start <= nxt < day_end:
            status = "Upcoming"
            event_time = nxt
        if status is None:
            continue

        hits.append({
            "release": name,
            "description": _meta(ent, "Description") or _meta(ent, "FullDescription") or name,
            "region": (_meta(ent, "Region") or "").lower(),
            "source": _meta(ent, "Source") or "",
            "status": status,
            "event_time_utc": event_time,
            "event_time_local": event_time + shift if event_time else None,
        })
    hits.sort(key=lambda h: (h["event_time_utc"] or datetime.max.replace(tzinfo=timezone.utc)))
    return hits


def series_for_release(api, release_name: str, regions: Optional[List[str]], limit: int,
                       cache: Optional["Cache"] = None,
                       ttl_days: float = 7.0) -> List[str]:
    """
    Names of time series that belong to a release, headline candidates first.

    Cached, because release membership changes on the timescale of Macrobond
    adding a new series, not daily. This is the single biggest saving: without
    it we fire one search per release on every run.
    """
    if cache is not None:
        cached = cache.get_members(release_name, ttl_days)
        if cached is not None:
            return cached[:limit]

    from macrobond_data_api.common.types import SearchFilter  # type: ignore

    entities: List[Any] = []
    try:
        filters = [SearchFilter(entity_types=["TimeSeries"], must_have_values={"Release": release_name})]
        entities = list(api.entity_search_multi_filter(*filters, no_metadata=False))
    except Exception:
        try:
            entities = list(api.entity_search(entity_types=["TimeSeries"],
                                              must_have_values={"Release": release_name}))
        except Exception:
            return []

    scored: List[Tuple[float, str]] = []
    for ent in entities:
        name = _entity_name(ent)
        if not name:
            continue
        region = (_meta(ent, "Region") or "").lower()
        if regions and region and region not in regions:
            continue
        desc = str(_meta(ent, "Description") or name).lower()
        rank = float(len(desc))
        if any(h in desc for h in HEADLINE_HINTS):
            rank -= 40.0
        if any(p in desc for p in COMPONENT_PENALTY):
            rank += 60.0
        scored.append((rank, name))

    scored.sort()
    ranked = [n for _, n in scored]
    if cache is not None:
        # cache the full ranked list so raising --max-series-per-release later
        # does not force a re-search
        cache.put_members(release_name, ranked[:200])
    return ranked[:limit]


def load_series_batch(api, names: Sequence[str], cache: Optional["Cache"] = None,
                      incremental: bool = True) -> Tuple[Dict[str, Any], int]:
    """
    Download series, asking Macrobond only for what has changed.

    get_many_series accepts (name, last_modified) tuples. With
    include_not_modified=False, anything unchanged since the timestamp we hold
    is omitted from the response and no data crosses the wire. A series that
    comes back NotModified has no new print, so there is nothing to score and
    dropping it is correct, not a compromise.

    Returns (series by name, count skipped as unchanged).
    """
    out: Dict[str, Any] = {}
    use_incremental = incremental and cache is not None and cache.enabled

    for i in range(0, len(names), 150):
        chunk = list(names[i:i + 150])
        fetched = False

        if use_incremental:
            requests: List[Any] = []
            for name in chunk:
                stamp = cache.get_modified(name)
                requests.append((name, stamp) if stamp else name)
            try:
                for s in api.get_many_series(requests, include_not_modified=False):
                    if s is None or getattr(s, "is_error", False):
                        continue
                    name = _entity_name(s) or getattr(s, "name", "")
                    if name:
                        out[name] = s
                fetched = True
            except Exception as exc:  # noqa: BLE001
                print(f"  incremental fetch unavailable, falling back: {exc}")

        if not fetched:
            try:
                for s in api.get_series(chunk, raise_error=False):
                    if getattr(s, "is_error", False):
                        continue
                    name = _entity_name(s) or getattr(s, "name", "")
                    if name:
                        out[name] = s
            except Exception as exc:  # noqa: BLE001
                print(f"  series batch failed ({len(chunk)} names): {exc}")

    if cache is not None:
        for name, s in out.items():
            cache.put_modified(name, getattr(s, "last_modified", None))

    return out, len(names) - len(out)


def series_arrays(series: Any) -> Tuple[List[datetime], List[Optional[float]]]:
    dates = list(getattr(series, "dates", []) or [])
    values = list(getattr(series, "values", []) or [])
    if dates and values:
        return dates, values
    try:
        df = series.to_pd_data_frame()
        return list(df.index), list(df.iloc[:, 0])
    except Exception:
        return [], []


# --------------------------------------------------------------------------
# Probe mode
# --------------------------------------------------------------------------

def list_regions(api) -> None:
    """Print the region codes Macrobond actually uses, and reconcile with ours."""
    print("\n=== Macrobond region codes ===")
    try:
        result = api.metadata_list_values("Region")
    except Exception as exc:  # noqa: BLE001
        print(f"  metadata_list_values('Region') failed: {exc}")
        return

    found: Dict[str, str] = {}
    for item in result:
        code = _meta(item, "Value") or getattr(item, "value", None)
        desc = _meta(item, "Description") or getattr(item, "description", "")
        if code:
            found[str(code).lower()] = str(desc)
    print(f"  {len(found)} region values")
    for code, desc in sorted(found.items()):
        print(f"    {code:<8} {desc}")

    ours = set(DEVELOPED + EM_MAJOR + AFRICA + FRONTIER + CORE_REGIONS)
    unknown = sorted(c for c in ours if c not in found)
    if unknown:
        print(f"\n  WARNING: {len(unknown)} codes in this script are not valid "
              f"Macrobond regions and will match nothing:")
        print(f"    {', '.join(unknown)}")
    else:
        print("\n  every code in this script matches a Macrobond region.")

    unmapped = sorted(c for c in found if c not in ours)
    if unmapped:
        print(f"\n  {len(unmapped)} Macrobond regions are in neither the include "
              f"presets nor the exclusion lists:")
        print(f"    {', '.join(unmapped)}")


def run_probe(api, regions: Optional[List[str]], outdir: str = ".") -> None:
    print("\n=== PROBE: raw shape of a search result ===")
    raw, truncated = _search(api, True, entity_types=["Release"],
                             must_have_values={"Region": (regions or ["us"])[0]})
    print(f"  rows: {len(raw)}   truncated: {truncated}")
    if raw:
        first = raw[0]
        print(f"  python type of a row: {type(first)}")
        print(f"  is a Mapping: {isinstance(first, dict)}")
        print(f"  has .name attribute: {hasattr(first, 'name')}")
        print(f"  has .metadata attribute: {hasattr(first, 'metadata')}")

    print("\n=== PROBE: Release entities ===")
    releases = fetch_releases(api, regions)
    print(f"total unique release entities: {len(releases)}")
    if not releases:
        print("nothing came back. Check the Data+ licence and the entity type name.")
        return

    keys: Dict[str, int] = {}
    samples: Dict[str, Any] = {}
    for ent in releases:
        for k, v in _as_mapping(ent).items():
            keys[k] = keys.get(k, 0) + 1
            samples.setdefault(k, v)
    print(f"\nmetadata attributes present on Release entities "
          f"(attribute: count of {len(releases)}, example value):")
    for k, v in sorted(keys.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<34} {v:>6}   {samples[k]!r}")

    dated = [e for e in releases
             if _as_datetime(_meta(e, "NextReleaseEventTime"))
             or _as_datetime(_meta(e, "LastReleaseEventTime"))]
    print(f"\nreleases carrying a parseable event time: {len(dated)} of {len(releases)}")

    # Which attribute separates economic from company releases? Print the value
    # distribution of every low-cardinality attribute and read it off.
    print("\nvalue distribution of low-cardinality attributes "
          "(candidate economic/company discriminators):")
    for attr in sorted(keys):
        values: Dict[str, int] = {}
        for ent in releases:
            raw = _meta(ent, attr)
            if raw in (None, "", []):
                continue
            values[str(raw)] = values.get(str(raw), 0) + 1
        if not values or len(values) > 25:
            continue
        print(f"  {attr}  ({len(values)} distinct)")
        for v, n in sorted(values.items(), key=lambda kv: -kv[1]):
            print(f"      {n:>6}  {v}")

    print("\nclassification under the current heuristic:")
    tally: Dict[str, int] = {}
    reasons: Dict[str, int] = {}
    for ent in releases:
        k, reason = classify_release(ent)
        tally[k] = tally.get(k, 0) + 1
        reasons[f"{k}: {reason}"] = reasons.get(f"{k}: {reason}", 0) + 1
    for k, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<10} {n}")
    print("  reasons:")
    for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:12]:
        print(f"      {n:>6}  {r}")

    print("\n10 releases classified as company (check these are genuinely corporate):")
    for ent in [e for e in releases if classify_release(e)[0] == "company"][:10]:
        print(f"    {_entity_name(ent)}  |  {_meta(ent, 'Description')}")
    print("\n10 classified as unknown (these are kept when --kind economic):")
    for ent in [e for e in releases if classify_release(e)[0] == "unknown"][:10]:
        print(f"    {_entity_name(ent)}  |  {_meta(ent, 'Description')}")

    print("\nfirst 5 release entities in full:")
    for ent in releases[:5]:
        print(f"\n  name: {_entity_name(ent)}")
        for k, v in _as_mapping(ent).items():
            print(f"    {k}: {v!r}")

    dump = os.path.join(outdir, "macrobond_probe_releases.json")
    try:
        os.makedirs(outdir, exist_ok=True)
        with open(dump, "w", encoding="utf-8") as fh:
            json.dump([_as_mapping(e) for e in releases[:300]], fh, indent=2, default=str)
        print(f"\nfirst 300 release metadata records written to {dump}")
    except Exception as exc:  # noqa: BLE001
        print(f"\ncould not write the probe dump: {exc}")

    sample = _entity_name(releases[0])
    if sample:
        print(f"\n=== PROBE: series attached to release {sample!r} ===")
        try:
            rows, _ = _search(api, True, entity_types=["TimeSeries"],
                              must_have_values={"Release": sample})
            print(f"  {len(rows)} member series")
            for row in rows[:5]:
                print(f"    {_entity_name(row)}  |  {_meta(row, 'Description')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  member lookup failed: {exc}")


# --------------------------------------------------------------------------
# Main scan
# --------------------------------------------------------------------------

def run_scan(args) -> Dict[str, Any]:
    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    regions, excluded = resolve_regions(args.regions, args.exclude, args.keep_excluded)

    print(f"Macrobond release scan for {target.isoformat()}")
    print(f"regions: {'all' if regions is None else ','.join(regions)}"
          f"  ({len(regions) if regions else 'unbounded'})")
    if excluded:
        print(f"excluding {len(excluded)} regions (Africa ex-ZA and frontier by default)")

    cache = Cache(args.cache, enabled=not args.no_cache)
    if args.refresh_cache:
        cache.data["releases"] = {}
        cache.data["release_members"] = {}
        print("cache: calendar and membership cleared, timestamps kept")

    searches = 0

    with open_client(args.client) as api:
        if args.list_regions:
            list_regions(api)
            return {}
        if args.probe:
            run_probe(api, regions, args.outdir)
            return {}

        print("\n[1/4] fetching release calendar")
        cache_key = f"{args.regions}|{args.exclude}|{args.kind}|{args.must_have}|{args.must_not_have}"
        releases = cache.get_calendar(cache_key, args.calendar_ttl_hours)
        if releases is None:
            releases = fetch_releases(
                api, regions,
                must_have=_parse_kv(args.must_have),
                must_not_have=_parse_kv(args.must_not_have),
                must_not_have_attributes=[a for a in args.must_not_have_attr.split(",") if a],
            )
            searches += len(regions) if regions else 1
            releases = drop_excluded_regions(releases, excluded)
            releases = filter_releases_by_kind(releases, args.kind)
            cache.put_calendar(cache_key, releases)
        else:
            print(f"  from cache, under {args.calendar_ttl_hours}h old")
        print(f"  {len(releases)} {args.kind} releases after filtering")

        todays = releases_on_day(releases, target, args.tz_offset)
        todays = todays[:args.max_releases]
        released = [r for r in todays if r["status"] == "Released"]
        upcoming = [r for r in todays if r["status"] != "Released"]
        print(f"  {len(todays)} releases on {target.isoformat()} "
              f"({len(released)} released, {len(upcoming)} upcoming)")

        # Only Released entries have a new print. Fetching series for Upcoming
        # ones downloads data we already scored on a previous run.
        if args.status == "all":
            to_score = todays
        elif args.status == "upcoming":
            to_score = upcoming
        else:
            to_score = released
        if upcoming and args.status == "released":
            print(f"  skipping {len(upcoming)} upcoming releases, no new print to score")

        if not to_score:
            print("  nothing to score. The calendar is still written to the report.")
            out = {"date": target.isoformat(),
                   "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "regions": "all" if regions is None else ",".join(regions),
                   "releases": todays, "rows": []}
            cache.save()
            write_outputs(out, args.outdir, args.top)
            return out

        print("\n[2/4] resolving member series")
        release_series: Dict[str, List[str]] = {}
        all_names: List[str] = []
        for rel in to_score:
            before = cache.hits["members"]
            names = series_for_release(api, rel["release"], regions,
                                       args.max_series_per_release, cache,
                                       args.members_ttl_days)
            if cache.hits["members"] == before:
                searches += 1
            release_series[rel["release"]] = names
            all_names.extend(names)
        all_names = list(dict.fromkeys(all_names))
        print(f"  {cache.hits['members']} of {len(to_score)} releases from cache, "
              f"{searches} searches this run")
        print(f"  {len(all_names)} unique series to fetch")

        print("\n[3/4] downloading observations")
        loaded, unchanged = load_series_batch(api, all_names, cache,
                                              incremental=not args.full_download)
        print(f"  {len(loaded)} series with new or revised data, "
              f"{unchanged} unchanged and not transferred")

    cache.save()

    print("\n[4/4] scoring")
    rows: List[Dict[str, Any]] = []
    for rel in to_score:
        for name in release_series.get(rel["release"], []):
            s = loaded.get(name)
            if s is None:
                continue
            dates, values = series_arrays(s)
            profile = score_series(dates, values)
            if profile is None:
                continue
            rows.append({
                "release": rel["release"],
                "release_description": rel["description"],
                "region": rel["region"] or (_meta(s, "Region") or ""),
                "status": rel["status"],
                "event_time_local": rel["event_time_local"].strftime("%Y-%m-%d %H:%M") if rel["event_time_local"] else "",
                "series": name,
                "description": _meta(s, "Description") or name,
                "unit": _meta(s, "DisplayUnit") or _meta(s, "Unit") or "",
                "frequency": _meta(s, "Frequency") or "",
                "obs_date": profile["obs_date"].strftime("%Y-%m-%d") if profile["obs_date"] else "",
                "latest": round(profile["latest_value"], 4),
                "previous": round(profile["prev_value"], 4) if profile["prev_value"] is not None else "",
                "scored_on": profile["space"],
                "z": round(profile["z"], 2),
                "abs_z": round(profile["abs_z"], 2),
                "trend_break": round(profile["trend_break"], 2),
                "pctile": round(profile["pctile"], 1),
                "n_obs": profile["n_obs"],
                "verdict": verdict(profile),
            })

    rows.sort(key=lambda r: -r["abs_z"])
    print(f"  {len(rows)} series scored")

    out = {
        "date": target.isoformat(),
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "regions": "all" if regions is None else ",".join(regions),
        "releases": todays,
        "rows": rows,
    }
    write_outputs(out, args.outdir, args.top)
    return out


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

CSV_FIELDS = [
    "release", "release_description", "region", "status", "event_time_local",
    "series", "description", "unit", "frequency", "obs_date", "latest",
    "previous", "scored_on", "z", "abs_z", "trend_break", "pctile", "n_obs", "verdict",
]


def write_outputs(payload: Dict[str, Any], outdir: str, top: int) -> None:
    os.makedirs(outdir, exist_ok=True)
    stem = f"macrobond_releases_{payload['date']}"

    csv_path = os.path.join(outdir, stem + ".csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(payload["rows"])

    json_path = os.path.join(outdir, stem + ".json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    html_path = os.path.join(outdir, stem + ".html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(payload, top))

    print(f"\nwritten:\n  {csv_path}\n  {json_path}\n  {html_path}")

    print(f"\n--- top {min(top, len(payload['rows']))} by |z| ---")
    for r in payload["rows"][:top]:
        print(f"  {r['z']:+6.2f}sd  {r['region']:<3} {r['description'][:60]:<60} "
              f"{r['latest']:>12}  ({r['scored_on']})")


def render_html(payload: Dict[str, Any], top: int) -> str:
    rows = payload["rows"]
    highlights = rows[:top]

    def band(z: float) -> str:
        az = abs(z)
        if az >= 3:
            return "x"
        if az >= 2:
            return "h"
        if az >= 1.25:
            return "m"
        return "l"

    def tbody(items: List[Dict[str, Any]]) -> str:
        cells = []
        for r in items:
            cells.append(
                f"<tr class='{band(r['z'])}'>"
                f"<td class='z'>{r['z']:+.2f}</td>"
                f"<td class='reg'>{r['region'].upper()}</td>"
                f"<td class='desc'>{r['description']}<span class='sub'>{r['release_description']}</span></td>"
                f"<td class='num'>{r['latest']}</td>"
                f"<td class='num'>{r['previous']}</td>"
                f"<td class='num'>{r['trend_break']:+.2f}</td>"
                f"<td class='num'>{r['pctile']:.0f}</td>"
                f"<td class='sp'>{r['scored_on']}</td>"
                f"<td class='dt'>{r['obs_date']}</td>"
                f"</tr>"
            )
        return "".join(cells)

    def hhmm(value: Any) -> str:
        if isinstance(value, datetime):
            return value.strftime("%H:%M")
        if isinstance(value, str) and value:
            return value.split(" ")[-1]
        return ""

    cal = "".join(
        f"<tr><td class='dt'>{hhmm(r['event_time_local'])}</td>"
        f"<td class='reg'>{r['region'].upper()}</td>"
        f"<td>{r['description']}</td>"
        f"<td class='sp'>{r['status']}</td></tr>"
        for r in payload["releases"]
    )

    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<title>Macrobond release scan {payload['date']}</title>
<style>
:root {{ --bg:#0f1115; --card:#171a21; --line:#262b36; --fg:#e6e9ef; --dim:#8b93a3;
        --x:#ff5c5c; --h:#ff9f43; --m:#f7d154; --l:#5a6273; }}
@media (prefers-color-scheme: light) {{
  :root {{ --bg:#f6f7f9; --card:#fff; --line:#e2e5ea; --fg:#1a1d23; --dim:#6b7280; --l:#c3c8d2; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:28px; background:var(--bg); color:var(--fg);
        font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
h1 {{ font-size:20px; margin:0 0 4px; letter-spacing:-.01em; }}
.meta {{ color:var(--dim); font-size:12px; margin-bottom:22px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:18px 20px; margin-bottom:20px; }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim);
      margin:0 0 12px; font-weight:600; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.05em;
      color:var(--dim); font-weight:600; padding:0 8px 8px; border-bottom:1px solid var(--line); }}
td {{ padding:8px; border-bottom:1px solid var(--line); vertical-align:top; }}
tr:last-child td {{ border-bottom:none; }}
.z {{ font-variant-numeric:tabular-nums; font-weight:600; width:64px; }}
tr.x .z {{ color:var(--x); }} tr.h .z {{ color:var(--h); }}
tr.m .z {{ color:var(--m); }} tr.l .z {{ color:var(--l); }}
.num {{ font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap; }}
.reg {{ color:var(--dim); font-weight:600; width:46px; }}
.dt, .sp {{ color:var(--dim); white-space:nowrap; }}
.desc .sub {{ display:block; color:var(--dim); font-size:11px; margin-top:2px; }}
.note {{ color:var(--dim); font-size:12px; margin-top:10px; }}
</style></head><body>
<h1>Macrobond release scan — {payload['date']}</h1>
<div class="meta">{len(payload['releases'])} releases · {len(rows)} series scored ·
regions {payload['regions']} · generated {payload['generated_utc']}</div>

<div class="card"><h2>What jumps out</h2>
<table><thead><tr><th>z</th><th>Reg</th><th>Series</th><th>Latest</th><th>Prev</th>
<th>Trend break</th><th>Pctile</th><th>Scored on</th><th>Obs</th></tr></thead>
<tbody>{tbody(highlights)}</tbody></table>
<div class="note">z is the latest observation against a trailing 60-observation window that
excludes the print itself. Series with a persistent drift are scored on their
period-on-period change rather than the level; the column says which.</div>
</div>

<div class="card"><h2>Release calendar</h2>
<table><thead><tr><th>Time</th><th>Reg</th><th>Release</th><th>Status</th></tr></thead>
<tbody>{cal}</tbody></table></div>

</body></html>"""


# --------------------------------------------------------------------------
# Offline self-test
# --------------------------------------------------------------------------

def run_selftest() -> int:
    import random
    random.seed(7)
    failures = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal failures
        if cond:
            print(f"  PASS  {label}")
        else:
            failures += 1
            print(f"  FAIL  {label} {detail}")

    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    dates = [base + timedelta(days=30 * i) for i in range(80)]

    # 1. stationary series with a benign last print
    calm = [50 + random.gauss(0, 1) for _ in range(80)]
    p = score_series(dates, calm)
    check("stationary series scored on level", p is not None and p["space"] == "level",
          f"got {p and p['space']}")
    check("calm print is not flagged", p is not None and p["abs_z"] < 2.5, f"z={p and p['z']}")

    # 2. stationary series with a 4sd shock
    shock = calm[:-1] + [50 + 4.0 * statistics.pstdev(calm[:-1])]
    p = score_series(dates, shock)
    check("4sd shock flagged", p is not None and p["abs_z"] > 3.0, f"z={p and p['z']}")
    check("shock verdict is Extreme", p is not None and verdict(p).startswith("Extreme"))

    # 3. trending level (a CPI-style index) must be scored on the change
    trend = [100 * (1.002 ** i) + random.gauss(0, 0.05) for i in range(80)]
    p = score_series(dates, trend)
    check("trending index scored on change", p is not None and p["space"] == "change",
          f"got {p and p['space']}")
    check("steady trend gives small z", p is not None and p["abs_z"] < 2.5, f"z={p and p['z']}")

    # 4. trending level with a jump in the change
    jumpy = trend[:-1] + [trend[-2] * 1.02]
    p = score_series(dates, jumpy)
    check("jump in a trending index flagged", p is not None and p["abs_z"] > 3.0, f"z={p and p['z']}")

    # 5. too little history returns None
    check("short series returns None", score_series(dates[:10], calm[:10]) is None)

    # 6. missing values tolerated
    holes: List[Optional[float]] = list(calm)
    for i in (3, 11, 44):
        holes[i] = None
    check("None values tolerated", score_series(dates, holes) is not None)

    # 7. percentile of a record high
    p = score_series(dates, shock)
    check("record high sits at the 100th percentile", p is not None and p["pctile"] >= 99.0,
          f"pctile={p and p['pctile']}")

    # 8. metadata coercion
    check("list metadata unwrapped", _first(["us", "ea"]) == "us")
    check("ISO string parsed", _as_datetime("2026-08-17T13:30:00Z") ==
          datetime(2026, 8, 17, 13, 30, tzinfo=timezone.utc))
    check("naive datetime made UTC", _as_datetime(datetime(2026, 8, 17)) ==
          datetime(2026, 8, 17, tzinfo=timezone.utc))
    check("junk returns None", _as_datetime("not a date") is None)

    # 9. day filtering, including the timezone shift
    class FakeEnt:
        def __init__(self, name, md):
            self.name = name
            self.metadata = md

    ents = [
        FakeEnt("rel_a", {"Description": "A", "Region": "us",
                          "LastReleaseEventTime": "2026-08-17T12:30:00Z"}),
        FakeEnt("rel_b", {"Description": "B", "Region": "gb",
                          "NextReleaseEventTime": "2026-08-17T06:00:00Z"}),
        FakeEnt("rel_c", {"Description": "C", "Region": "jp",
                          "LastReleaseEventTime": "2026-08-16T23:50:00Z"}),
    ]
    # 9b. the shape entity_search actually returns: bare metadata mappings
    dict_ents = [
        {"Name": "rel_a", "Description": "A", "Region": "us",
         "LastReleaseEventTime": "2026-08-17T12:30:00Z"},
        {"Name": "rel_b", "Description": "B", "Region": "gb",
         "NextReleaseEventTime": "2026-08-17T06:00:00Z"},
        {"Name": "rel_z", "Description": "Z", "Region": "us",
         "LastReleaseEventTime": "2026-08-11T12:30:00Z"},
    ]
    check("name read from a metadata mapping", _entity_name(dict_ents[0]) == "rel_a",
          f"got {_entity_name(dict_ents[0])}")
    check("metadata read from a mapping", _meta(dict_ents[0], "Region") == "us")
    check("name falls back to the .name attribute",
          _entity_name(FakeEnt("rel_obj", {"Description": "O"})) == "rel_obj")
    dict_hits = releases_on_day(dict_ents, date(2026, 8, 17), 0.0)
    check("mapping-shaped rows filter to the right day", len(dict_hits) == 2,
          f"got {len(dict_hits)}")
    check("mapping-shaped rows keep their description",
          {h["description"] for h in dict_hits} == {"A", "B"})

    # 9c. economic vs company classification
    econ = {"Name": "rel_us_cpi", "Description": "Consumer Price Index",
            "Region": "us", "Source": "BLS"}
    corp_attr = {"Name": "rel_aapl", "Description": "Apple Inc",
                 "Region": "us", "Company": "Apple Inc", "Isin": "US0378331005"}
    corp_text = {"Name": "rel_x", "Description": "Q3 Earnings Release", "Region": "us"}
    bare = {"Name": "rel_y", "Description": "Something"}

    check("economic release classified economic", classify_release(econ)[0] == "economic",
          f"got {classify_release(econ)}")
    check("company attribute wins", classify_release(corp_attr)[0] == "company",
          f"got {classify_release(corp_attr)}")
    check("earnings wording caught", classify_release(corp_text)[0] == "company",
          f"got {classify_release(corp_text)}")
    check("no discriminator is unknown, not a false economic",
          classify_release(bare)[0] == "unknown", f"got {classify_release(bare)}")
    check("typed attribute beats wording",
          classify_release({"Name": "r", "Description": "Earnings",
                            "ReleaseType": "Economic"})[0] == "economic")

    kept = filter_releases_by_kind([econ, corp_attr, corp_text, bare], "economic", verbose=False)
    check("economic filter drops both company rows", len(kept) == 2, f"got {len(kept)}")
    check("unknown is kept rather than silently dropped",
          any(_entity_name(k) == "rel_y" for k in kept))
    check("--kind all keeps everything",
          len(filter_releases_by_kind([econ, corp_attr], "all", verbose=False)) == 2)

    # 9d. region resolution
    inc, exc = resolve_regions("core")
    check("core preset resolves", inc == CORE_REGIONS, f"got {inc}")
    inc, exc = resolve_regions("dm-em")
    check("dm-em includes South Africa", "za" in inc)
    check("dm-em excludes Nigeria", "ng" not in inc)
    check("dm-em excludes Vietnam as frontier", "vn" not in inc)
    check("Africa list does not contain za", "za" not in AFRICA)
    check("no code is both included and excluded by default",
          not (set(DEVELOPED + EM_MAJOR) & set(DEFAULT_EXCLUDED)),
          f"overlap: {sorted(set(DEVELOPED + EM_MAJOR) & set(DEFAULT_EXCLUDED))}")

    inc, exc = resolve_regions("all")
    check("all preset has no include filter", inc is None)
    check("all preset still excludes Kenya", "ke" in exc)

    inc, exc = resolve_regions("us,ng")
    check("explicit list is taken at face value", inc == ["us", "ng"], f"got {inc}")
    check("explicit list clears that code from exclusions", "ng" not in exc)

    inc, exc = resolve_regions("dm-em", extra_excludes="tr,mx")
    check("extra excludes drop from the preset", "tr" not in inc and "mx" not in inc)

    inc, exc = resolve_regions("all", keep_excluded=True)
    check("--keep-excluded empties the exclusion set", exc == set())

    rels = [{"Name": "a", "Region": "us"}, {"Name": "b", "Region": "ng"},
            {"Name": "c", "Region": "za"}, {"Name": "d"}]
    kept = drop_excluded_regions(rels, set(DEFAULT_EXCLUDED), verbose=False)
    check("excluded region dropped", not any(_entity_name(k) == "b" for k in kept))
    check("South Africa survives", any(_entity_name(k) == "c" for k in kept))
    check("release with no region is kept", any(_entity_name(k) == "d" for k in kept))

    # 9e. cache behaviour
    import tempfile
    tmpdir = tempfile.mkdtemp()
    cpath = os.path.join(tmpdir, "cache.json")

    c = Cache(cpath, enabled=True)
    check("empty cache misses the calendar", c.get_calendar("k", 12) is None)
    c.put_calendar("k", [{"Name": "rel_a", "Region": "us"}])
    c.save()
    c2 = Cache(cpath, enabled=True)
    check("calendar survives a reload", c2.get_calendar("k", 12) is not None)
    check("calendar cache counts a hit", c2.hits["calendar"] == 1)
    check("expired calendar is a miss", c2.get_calendar("k", 0) is None)
    check("different filter key is a miss", c2.get_calendar("other", 12) is None)

    c2.put_members("rel_a", ["s1", "s2", "s3"])
    check("membership returns and respects the limit",
          Cache.__dict__ and c2.get_members("rel_a", 7)[:2] == ["s1", "s2"])
    check("expired membership is a miss", c2.get_members("rel_a", 0) is None)

    c2.put_modified("s1", datetime(2026, 8, 17, tzinfo=timezone.utc))
    c2.save()
    c3 = Cache(cpath, enabled=True)
    check("last-modified survives a reload",
          c3.get_modified("s1") == datetime(2026, 8, 17, tzinfo=timezone.utc))
    check("unknown series has no timestamp", c3.get_modified("nope") is None)

    disabled = Cache(cpath, enabled=False)
    check("disabled cache never hits", disabled.get_calendar("k", 12) is None
          and disabled.get_members("rel_a", 7) is None
          and disabled.get_modified("s1") is None)

    # a corrupt cache must not take the run down
    with open(cpath, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    c4 = Cache(cpath, enabled=True)
    check("corrupt cache falls back to empty", c4.get_calendar("k", 12) is None)

    # 9f. incremental download against a fake API
    class FakeSeries:
        def __init__(self, name, n=40):
            self.name = name
            self.metadata = {"Name": name, "Description": name}
            self.is_error = False
            self.last_modified = datetime(2026, 8, 17, tzinfo=timezone.utc)
            self.dates = [base + timedelta(days=30 * i) for i in range(n)]
            self.values = [50 + random.gauss(0, 1) for _ in range(n)]

    class FakeApi:
        def __init__(self, changed):
            self.changed = set(changed)
            self.many_calls = 0
            self.plain_calls = 0
            self.last_request = None

        def get_many_series(self, requests, include_not_modified=False):
            self.many_calls += 1
            self.last_request = list(requests)
            for req in requests:
                name = req[0] if isinstance(req, tuple) else req
                stamp = req[1] if isinstance(req, tuple) else None
                if stamp is not None and name not in self.changed:
                    continue          # NotModified, omitted from the response
                yield FakeSeries(name)

        def get_series(self, names, raise_error=False):
            self.plain_calls += 1
            return [FakeSeries(n) for n in names]

    fresh = Cache(os.path.join(tmpdir, "c2.json"), enabled=True)
    api = FakeApi(changed=[])
    loaded, unchanged = load_series_batch(api, ["s1", "s2", "s3"], fresh)
    check("first run downloads everything", len(loaded) == 3 and unchanged == 0,
          f"got {len(loaded)}, {unchanged}")
    check("first run sends bare names, no timestamps",
          all(not isinstance(r, tuple) for r in api.last_request))

    api2 = FakeApi(changed=["s2"])
    loaded, unchanged = load_series_batch(api2, ["s1", "s2", "s3"], fresh)
    check("second run sends timestamps",
          all(isinstance(r, tuple) for r in api2.last_request))
    check("only the changed series comes back", list(loaded) == ["s2"], f"got {list(loaded)}")
    check("unchanged series are counted, not transferred", unchanged == 2, f"got {unchanged}")

    api3 = FakeApi(changed=["s2"])
    loaded, _ = load_series_batch(api3, ["s1", "s2", "s3"], fresh, incremental=False)
    check("--full-download bypasses the incremental path",
          api3.plain_calls == 1 and api3.many_calls == 0 and len(loaded) == 3)

    class BrokenApi(FakeApi):
        def get_many_series(self, requests, include_not_modified=False):
            raise RuntimeError("not supported on this client")

    broken = BrokenApi(changed=[])
    loaded, _ = load_series_batch(broken, ["s1", "s2"], fresh)
    check("incremental failure falls back to get_series",
          broken.plain_calls == 1 and len(loaded) == 2)

    check("kv parser handles pairs", _parse_kv("A=1,B=2") == {"A": "1", "B": "2"})
    check("kv parser ignores junk", _parse_kv("nonsense,,C=3") == {"C": "3"})

    hits = releases_on_day(ents, date(2026, 8, 17), 0.0)
    check("two releases match the target day", len(hits) == 2, f"got {len(hits)}")
    check("released vs upcoming split correctly",
          {h["release"]: h["status"] for h in hits} == {"rel_a": "Released", "rel_b": "Upcoming"})
    hits_tokyo = releases_on_day(ents, date(2026, 8, 17), 9.0)
    check("timezone shift pulls in the Japanese print",
          any(h["release"] == "rel_c" for h in hits_tokyo))

    print(f"\n{'all checks passed' if failures == 0 else str(failures) + ' check(s) failed'}")
    return failures


# --------------------------------------------------------------------------

def _parse_kv(text: str) -> Dict[str, Any]:
    """Parse 'Key=Value,Key2=Value2' into a dict for search filters."""
    out: Dict[str, Any] = {}
    for pair in (text or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Macrobond release calendar scan")
    ap.add_argument("--date", help="YYYY-MM-DD, defaults to today")
    ap.add_argument("--regions", default="dm-em",
                    help="preset (core, dm, dm-em, all) or a comma separated list of "
                         "region codes. An explicit list overrides the exclusions")
    ap.add_argument("--exclude", default="",
                    help="extra region codes to exclude, comma separated")
    ap.add_argument("--keep-excluded", action="store_true",
                    help="do not apply the default Africa and frontier exclusions")
    ap.add_argument("--list-regions", action="store_true",
                    help="print Macrobond's region codes, check this script's lists "
                         "against them, and exit")
    ap.add_argument("--status", default="released", choices=["all", "released", "upcoming"],
                    help="which releases to SCORE. The calendar table always shows "
                         "everything on the day. Upcoming releases have no new print, "
                         "so scoring them just re-downloads old data")
    ap.add_argument("--cache", default="macrobond_scan_cache.json",
                    help="cache file path. Keep it on a local unsynced path, "
                         "OneDrive corrupts frequently rewritten files")
    ap.add_argument("--no-cache", action="store_true", help="bypass the cache entirely")
    ap.add_argument("--refresh-cache", action="store_true",
                    help="discard the cached calendar and membership, keep timestamps")
    ap.add_argument("--calendar-ttl-hours", type=float, default=12.0)
    ap.add_argument("--members-ttl-days", type=float, default=7.0,
                    help="how long to trust cached release membership")
    ap.add_argument("--full-download", action="store_true",
                    help="disable the NotModified path and re-download everything")
    ap.add_argument("--kind", default="economic", choices=["economic", "company", "all"],
                    help="economic releases only by default, company earnings excluded")
    ap.add_argument("--must-have", default="",
                    help="extra search filter, 'Key=Value,Key2=Value2'")
    ap.add_argument("--must-not-have", default="",
                    help="exclude releases with these metadata values, 'Key=Value'")
    ap.add_argument("--must-not-have-attr", default="",
                    help="exclude releases carrying these attributes, comma separated, "
                         "e.g. Company,Isin. Pushed server-side, so it also beats the "
                         "2000-result cap")
    ap.add_argument("--client", default="com", choices=["com", "web"],
                    help="com = Macrobond desktop on Windows, web = Data Web API feed")
    ap.add_argument("--tz-offset", type=float, default=1.0,
                    help="hours to add to UTC for display, 1.0 = BST")
    ap.add_argument("--max-releases", type=int, default=DEFAULT_MAX_RELEASES)
    ap.add_argument("--max-series-per-release", type=int, default=DEFAULT_MAX_SERIES_PER_RELEASE,
                    help="headline aggregates only by default. Raising this pulls in "
                         "the component tail and multiplies the download")
    ap.add_argument("--top", type=int, default=25, help="rows in the highlights table")
    ap.add_argument("--outdir", default=".", help="where to write CSV/JSON/HTML")
    ap.add_argument("--probe", action="store_true",
                    help="dump Release entity metadata shape and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="run the offline maths checks, no Macrobond needed")
    args = ap.parse_args()

    if args.selftest:
        return 1 if run_selftest() else 0

    try:
        run_scan(args)
    except ImportError:
        print("macrobond-data-api is not installed. Run: pip install macrobond-data-api")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
