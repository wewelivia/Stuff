
"""
dump_releases.py - show exactly what the provider produced, per event.
 
WHY
---
When a print you know exists is missing from the dashboard, there are only three
possible causes, and they need different fixes:
 
  1. The ticker produced NO records at all     -> a fetch/ticker problem
  2. It produced records, but none in-window   -> a date/remap problem
  3. It produced an in-window record           -> the dashboard or engine is at fault
 
The dashboard cannot tell you which. This can. It runs the configured provider,
prints every record grouped by event, marks which fall inside the display window,
and lists any configured release that returned nothing.
 
RUN
---
    python dump_releases.py
    python dump_releases.py Euro       # filter to matching events
"""
 
from __future__ import annotations
 
import datetime as dt
import os
import sys
 
import yaml
 
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
 
import challenge_engine  # noqa: E402
 
 
def load_yaml(name):
    path = os.path.join(BASE, name)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
 
 
def main():
    needle = sys.argv[1].lower() if len(sys.argv) > 1 else None
 
    cfg = load_yaml("config.yaml")
    mode = os.environ.get("HV_PROVIDER_MODE") or cfg.get("provider_mode", "snapshot")
 
    if mode == "blpapi":
        from providers.blpapi_provider import BlpapiProvider as P
    elif mode == "snapshot":
        from providers.snapshot_provider import SnapshotProvider as P
    else:
        from providers.mock_provider import MockProvider as P
 
    print(f"provider_mode: {mode}")
    provider = P(cfg)
    rows = provider.get_releases()
 
    err = getattr(provider, "fetch_error", None)
    if err:
        print(f"FETCH ERROR: {err}")
    print(f"as_of: {getattr(provider, 'as_of', None)}")
    print(f"total records returned: {len(rows)}\n")
 
    win = cfg.get("window", {}) or {}
    lookback = int(win.get("lookback_days", 10))
    lookahead = int(win.get("lookahead_days", 5))
    today = dt.date.today()
    lo = (today - dt.timedelta(days=lookback)).isoformat()
    hi = (today + dt.timedelta(days=lookahead)).isoformat()
    print(f"display window: {lo} .. {hi}   ({lookback}d back, {lookahead}d fwd)")
    print(f"today: {today}\n")
 
    # Group what came back by event.
    by_event = {}
    for r in rows:
        by_event.setdefault(r.get("event") or "<no event>", []).append(r)
 
    configured = [e.get("event") for e in (cfg.get("releases") or [])]
 
    shown = 0
    for event in configured:
        if needle and needle not in (event or "").lower():
            continue
        shown += 1
        recs = by_event.get(event, [])
        in_win = [r for r in recs
                  if r.get("date_iso") and lo <= str(r["date_iso"])[:10] <= hi]
 
        if not recs:
            print(f"  {event}")
            print("      NO RECORDS AT ALL -> fetch/ticker problem, not the window")
            print("      Check the [blpapi] lines in the console for this ticker.\n")
            continue
 
        flag = "" if in_win else "   <-- nothing in window"
        print(f"  {event}   ({len(recs)} records, {len(in_win)} in window){flag}")
        for r in sorted(recs, key=lambda x: str(x.get("date_iso") or "")):
            d = str(r.get("date_iso"))
            mark = "IN " if (r.get("date_iso") and lo <= d[:10] <= hi) else "   "
            print(f"      {mark} publish={d:<12} period_end={str(r.get('period_end')):<12} "
                  f"actual={str(r.get('actual')):<9} cons={str(r.get('consensus')):<7} "
                  f"{r.get('status')}")
        print()
 
    if needle and not shown:
        print(f"  No configured event matches '{needle}'.")
        print("  Configured events:")
        for e in configured:
            print(f"      {e}")
        return
 
    # Anything returned that is not in config (shouldn't happen, but worth seeing).
    extra = set(by_event) - set(configured)
    if extra and not needle:
        print("Records for events not in config.yaml:")
        for e in sorted(extra):
            print(f"  {e} ({len(by_event[e])})")
 
 
if __name__ == "__main__":
    main()
 

