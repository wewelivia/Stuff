# PATCH — date normalisation fix for providers/blpapi_provider.py
# ----------------------------------------------------------------
# Symptom: `python -m providers.blpapi_provider` shows Released rows with
# slash dates (2026/07/14) while Upcoming rows carry dash dates (2026-07-22).
# Cause: on some blpapi builds, HistoricalDataRequest dates come back as
# Bloomberg's own Datetime type, not datetime.date. _to_iso's isinstance
# check misses it, str() keeps the slashes, and the window filter then
# string-compares "2026/07/14" against "2026-07-22" — the slash sorts above
# the dash, so every Released print falls outside the window and the page
# shows only the odd Upcoming record.
#
# Fix: replace the _to_iso function in providers/blpapi_provider.py with the
# version below (only the two marked lines are new). Nothing else changes.

def _to_iso(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, (_dt.datetime, _dt.date)):
        return d.strftime("%Y-%m-%d")
    # blpapi.Datetime and similar wrappers expose y/m/d but are not datetime
    if hasattr(d, "year") and hasattr(d, "month") and hasattr(d, "day"):    # NEW
        return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"                    # NEW
    s = str(d).strip().replace("/", "-")                                    # CHANGED: normalise slashes
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return _dt.datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10] if len(s) >= 10 else None
