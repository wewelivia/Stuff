# House View - On-Demand Refresh

The dashboard now runs **on demand** instead of live. Logic and format are
unchanged: `challenge_engine.py`, `house_view.yaml` and the HTML are untouched.
A new `snapshot` provider mode serves the standard `get_releases()` contract from
a local JSON file, so there is no API key, no network call and no paywall.

## How to refresh (the whole workflow)

1. Ask Claude: **"refresh the House View snapshot"**.
2. Claude returns an updated `releases_snapshot.json` - the latest prints with
   `consensus`, `actual`, `previous`, `status`, plus a `source` per release so you
   can see where each number came from.
3. Drop the file in beside `snapshot_provider.py` and refresh the dashboard.

The snapshot carries an `as_of` date. It is exactly as current as your last
refresh - worth surfacing that date on the page so nobody mistakes it for live.

## Wiring (one-time)

**1. Drop in the files**
- `providers/snapshot_provider.py`
- `providers/releases_snapshot.json`  (start from `releases_snapshot.example.json`)

**2. `config.yaml`**
```yaml
provider_mode: snapshot

snapshot:
  path: providers/releases_snapshot.json   # optional; this is the default
```

**3. `app.py` -> `get_provider()`** - add the branch alongside the existing modes:
```python
elif mode == "snapshot":
    from providers.snapshot_provider import SnapshotProvider
    return SnapshotProvider(config)
```

That is the entire change. Flip `provider_mode` back to `fmp`/`eodhd`/`blpapi`
later and nothing else moves.

## Snapshot contract (per release)

`event, region, pillar, theme, impact, assets, higher_is, unit,
date ("%d %b %Y"), date_iso, consensus, actual, previous, status`

- **Released** items carry `actual` + `consensus` + `previous`.
- **Upcoming** items carry the `date` and leave `consensus`/`actual` `null`.
- Optional `source` field records provenance and is ignored by the engine.
- Records are returned sorted by `date_iso` descending.

## To make the first real snapshot match your build

Paste your `config.yaml` (the 45 release entries) and your current
`house_view.yaml`. With those I can:
- generate a first `releases_snapshot.json` populated for *your* exact 45 releases
  (correct event names, themes, `higher_is`, units), verified against current data; and
- pour the "Power Struggle" house view into your exact `house_view.yaml` keys so the
  format you like is preserved byte-for-byte.
