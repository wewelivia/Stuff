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

MIN_OBS_FOR_SCORE = 24          # need this many trailing points to compute z
TRAILING_WINDOW = 60            # observations used for mean/sd
TREND_TEST_RATIO = 0.35         # |mean(diff)| > ratio * sd(diff) => trending
DEFAULT_MAX_SERIES_PER_RELEASE = 25
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


def _meta(entity: Any, key: str, default: Any = None) -> Any:
    md = getattr(entity, "metadata", None)
    if not isinstance(md, dict):
        return default
    if key in md:
        return _first(md[key])
    # Macrobond metadata keys are case sensitive but be forgiving anyway
    lowered = {k.lower(): v for k, v in md.items()}
    return _first(lowered.get(key.lower(), default))


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
# Macrobond access
# --------------------------------------------------------------------------

def open_client(mode: str):
    if mode == "com":
        from macrobond_data_api.com import ComClient  # type: ignore
        return ComClient()
    from macrobond_data_api.web import WebClient  # type: ignore
    return WebClient()


def fetch_releases(api, regions: Optional[List[str]], verbose: bool = True) -> List[Any]:
    """All Release entities, optionally narrowed by region."""
    attempts: List[Dict[str, Any]] = []
    if regions:
        attempts.append({"entity_types": ["Release"], "must_have_values": {"Region": regions}})
    attempts.append({"entity_types": ["Release"], "must_have_attributes": ["NextReleaseEventTime"]})
    attempts.append({"entity_types": ["Release"]})

    for i, kwargs in enumerate(attempts, 1):
        try:
            result = api.entity_search(**kwargs)
            entities = list(result)
            if entities:
                if verbose:
                    print(f"  release search #{i} returned {len(entities)} entities "
                          f"({', '.join(kwargs.keys())})")
                return entities
            if verbose:
                print(f"  release search #{i} returned nothing, falling back")
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  release search #{i} failed: {exc}")
    return []


def releases_on_day(releases: Iterable[Any], target: date, tz_offset_hours: float) -> List[Dict[str, Any]]:
    """Filter release entities to those whose last or next event falls on `target`."""
    shift = timedelta(hours=tz_offset_hours)
    day_start = datetime.combine(target, time.min, tzinfo=timezone.utc) - shift
    day_end = day_start + timedelta(days=1)

    hits: List[Dict[str, Any]] = []
    for ent in releases:
        name = getattr(ent, "name", None)
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


def series_for_release(api, release_name: str, regions: Optional[List[str]], limit: int) -> List[str]:
    """Names of time series that belong to a release, headline candidates first."""
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
        name = getattr(ent, "name", None)
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
    return [n for _, n in scored[:limit]]


def load_series_batch(api, names: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for i in range(0, len(names), 150):
        chunk = list(names[i:i + 150])
        try:
            for s in api.get_series(chunk, raise_error=False):
                if getattr(s, "is_error", False):
                    continue
                out[getattr(s, "name", "")] = s
        except Exception as exc:  # noqa: BLE001
            print(f"  series batch failed ({len(chunk)} names): {exc}")
    return out


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

def run_probe(api, regions: Optional[List[str]]) -> None:
    print("\n=== PROBE: Release entities ===")
    releases = fetch_releases(api, regions)
    print(f"total release entities returned: {len(releases)}")
    if not releases:
        print("nothing came back. Check the Data+ licence and entity type name.")
        return

    keys: Dict[str, int] = {}
    for ent in releases[:2000]:
        md = getattr(ent, "metadata", {}) or {}
        for k in md:
            keys[k] = keys.get(k, 0) + 1
    print("\nmetadata attributes present on Release entities (attribute: count):")
    for k, v in sorted(keys.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<34} {v}")

    print("\nfirst 5 release entities in full:")
    for ent in releases[:5]:
        print(f"\n  name: {getattr(ent, 'name', '?')}")
        for k, v in (getattr(ent, "metadata", {}) or {}).items():
            print(f"    {k}: {v!r}")

    sample = getattr(releases[0], "name", None)
    if sample:
        print(f"\n=== PROBE: series attached to release {sample!r} ===")
        names = series_for_release(api, sample, regions, 10)
        print(f"  {len(names)} candidate series: {names}")


# --------------------------------------------------------------------------
# Main scan
# --------------------------------------------------------------------------

def run_scan(args) -> Dict[str, Any]:
    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    regions = None if args.regions.lower() == "all" else [r.strip().lower() for r in args.regions.split(",") if r.strip()]

    print(f"Macrobond release scan for {target.isoformat()}")
    print(f"regions: {'all' if regions is None else ','.join(regions)}")

    with open_client(args.client) as api:
        if args.probe:
            run_probe(api, regions)
            return {}

        print("\n[1/4] fetching release calendar")
        releases = fetch_releases(api, regions)
        todays = releases_on_day(releases, target, args.tz_offset)
        if args.status != "all":
            todays = [r for r in todays if r["status"].lower() == args.status.lower()]
        todays = todays[:args.max_releases]
        print(f"  {len(todays)} releases on {target.isoformat()}")
        if not todays:
            print("  nothing scheduled. Try --status all, a different date, or --regions all.")
            return {"date": target.isoformat(), "releases": [], "rows": []}

        print("\n[2/4] resolving member series")
        release_series: Dict[str, List[str]] = {}
        all_names: List[str] = []
        for rel in todays:
            names = series_for_release(api, rel["release"], regions, args.max_series_per_release)
            release_series[rel["release"]] = names
            all_names.extend(names)
            print(f"  {rel['release']:<40} {len(names):>4} series")
        all_names = list(dict.fromkeys(all_names))
        print(f"  {len(all_names)} unique series to fetch")

        print("\n[3/4] downloading observations")
        loaded = load_series_batch(api, all_names)
        print(f"  {len(loaded)} series returned")

    print("\n[4/4] scoring")
    rows: List[Dict[str, Any]] = []
    for rel in todays:
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

def main() -> int:
    ap = argparse.ArgumentParser(description="Macrobond release calendar scan")
    ap.add_argument("--date", help="YYYY-MM-DD, defaults to today")
    ap.add_argument("--regions", default=",".join(CORE_REGIONS),
                    help="comma separated Macrobond region codes, or 'all'")
    ap.add_argument("--status", default="all", choices=["all", "released", "upcoming"])
    ap.add_argument("--client", default="com", choices=["com", "web"],
                    help="com = Macrobond desktop on Windows, web = Data Web API feed")
    ap.add_argument("--tz-offset", type=float, default=1.0,
                    help="hours to add to UTC for display, 1.0 = BST")
    ap.add_argument("--max-releases", type=int, default=DEFAULT_MAX_RELEASES)
    ap.add_argument("--max-series-per-release", type=int, default=DEFAULT_MAX_SERIES_PER_RELEASE)
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
