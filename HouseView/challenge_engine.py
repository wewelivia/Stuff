"""
challenge_engine.py - score incoming economic releases against the house view.

THE QUESTION IT ANSWERS
-----------------------
For each release: did the print CONFIRM the house view, CHALLENGE it, or land IN
LINE? Then, per theme, does the balance of evidence still support the house stance?

HOW A SURPRISE IS READ
----------------------
1. surprise = actual - consensus. No consensus, or an upcoming print, is not scored.
2. `higher_is` tells us what a higher-than-expected number MEANS:
     - "hawkish": higher = hotter inflation / higher policy rate
     - "good"   : higher = stronger economy (GDP, retail sales, payrolls)
     - "bad"    : higher = weaker economy (unemployment rate)
3. Each theme has a rule (below) saying which direction of surprise CONFIRMS the
   house stance. The house view this cycle is "sticky inflation + hold-to-hawkish
   rates + a softening real economy", so:
     - inflation / rate_policy : a HOTTER / more HAWKISH print confirms
     - growth / consumption / sentiment : a WEAKER print confirms (house expects
       softening, so strength challenges)
   labour is left UNSCORED while its house-view bias is TO_CONFIRM.

The rules are derived from house_view.yaml's bias but pinned here explicitly so the
scoring is transparent and auditable rather than inferred at runtime.
"""

from __future__ import annotations

import datetime as _dt

# axis: which meaning of the surprise matters for this theme.
# confirm_when: "hot" (hawkish/higher confirms) or "weak" (softer confirms).
THEME_RULES = {
    "inflation":   {"axis": "price",    "confirm_when": "hot"},
    "rate_policy": {"axis": "price",    "confirm_when": "hot"},
    "growth":      {"axis": "activity", "confirm_when": "weak"},
    "consumption": {"axis": "activity", "confirm_when": "weak"},
    "sentiment":   {"axis": "activity", "confirm_when": "weak"},
    # "labour" intentionally absent -> unscored while bias is TO_CONFIRM.
}

# Below this absolute surprise (in the indicator's own unit) we call it "In line".
IN_LINE_EPS = 1e-9


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def derive_status(r: dict, today: _dt.date | None = None) -> str:
    """
    Work out a release's status from its DATE and whether a number landed, rather
    than trusting the static `status` field on the record.

    Why: a snapshot taken on 23 Jun labels a 14 Jul print "Upcoming". Read back on
    15 Jul that is simply wrong. Deriving from the date keeps the dashboard honest
    however old the data is.

      Released      - a number is present
      Upcoming      - no number, date is in the future
      Awaiting print- no number, date has passed. Either the snapshot is stale or,
                      in blpapi mode, the actual field came back blank. This state
                      exists to make that visible instead of silently mislabelling
                      a missing number as "Upcoming".
    """
    today = today or _dt.date.today()
    actual = _num(r.get("actual"))
    if actual is not None:
        return "Released"

    iso = r.get("date_iso")
    if not iso:
        return "Upcoming"
    try:
        d = _dt.date.fromisoformat(str(iso)[:10])
    except ValueError:
        return "Upcoming"

    return "Upcoming" if d > today else "Awaiting print"


def filter_window(releases: list[dict], lookback_days: int = 10,
                  lookahead_days: int = 5, today: _dt.date | None = None) -> list[dict]:
    """Keep only releases dated within [today - lookback, today + lookahead]."""
    today = today or _dt.date.today()
    lo = (today - _dt.timedelta(days=lookback_days)).isoformat()
    hi = (today + _dt.timedelta(days=lookahead_days)).isoformat()
    out = []
    for r in releases:
        iso = r.get("date_iso")
        if not iso:
            continue
        if lo <= str(iso)[:10] <= hi:
            out.append(r)
    return out


def _signal(surprise: float, higher_is: str) -> float:
    """
    Convert a raw (actual - consensus) surprise into a signed signal where
    POSITIVE always means 'hotter / stronger than expected' and NEGATIVE means
    'cooler / weaker than expected', regardless of the indicator's polarity.
    """
    hi = (higher_is or "").lower()
    if hi == "bad":          # e.g. unemployment: higher = weaker -> invert
        return -surprise
    # "good" and "hawkish": higher = stronger/hotter -> keep sign
    return surprise


def assess_release(r: dict, today: _dt.date | None = None) -> dict:
    """Return the release dict enriched with surprise, verdict and score."""
    out = dict(r)
    theme = r.get("theme")
    actual = _num(r.get("actual"))
    consensus = _num(r.get("consensus"))

    # Status is derived, not taken on trust. See derive_status().
    status = derive_status(r, today)
    out["status"] = status

    out["surprise"] = None
    out["verdict"] = None
    out["score"] = 0          # +1 confirms, -1 challenges, 0 otherwise
    out["verdict_note"] = None

    if status == "Upcoming":
        out["verdict"] = "Upcoming"
        out["verdict_note"] = "Due " + (r.get("date") or r.get("date_iso") or "soon")
        return out

    if status == "Awaiting print":
        out["verdict"] = "Awaiting print"
        out["verdict_note"] = ("Date has passed but no number is in the data. "
                               "Refresh the snapshot, or check the Bloomberg field.")
        return out

    # Released but no consensus to score against.
    if consensus is None:
        out["verdict"] = "No consensus"
        out["verdict_note"] = "Actual printed but no consensus to score against"
        return out

    surprise = actual - consensus
    out["surprise"] = round(surprise, 4)

    rule = THEME_RULES.get(theme)
    if rule is None:
        out["verdict"] = "Unscored"
        out["verdict_note"] = "House view for this theme is not set (TO_CONFIRM)"
        return out

    if abs(surprise) <= IN_LINE_EPS:
        out["verdict"] = "In line"
        out["verdict_note"] = "Print matched consensus"
        return out

    signal = _signal(surprise, r.get("higher_is"))   # + hotter/stronger, - cooler/weaker
    hotter = signal > 0

    # confirm_when "hot": a hotter/stronger-than-expected print confirms.
    # confirm_when "weak": a weaker-than-expected print confirms.
    confirms = hotter if rule["confirm_when"] == "hot" else (not hotter)

    if confirms:
        out["verdict"] = "Confirms"
        out["score"] = 1
    else:
        out["verdict"] = "Challenges"
        out["score"] = -1

    direction = "hotter" if hotter else "cooler"
    if rule["axis"] == "activity":
        direction = "stronger" if hotter else "weaker"
    out["verdict_note"] = f"{direction} than consensus by {abs(surprise):g}"
    return out


def assess_releases(releases: list[dict], house_view: dict | None = None,
                    today: _dt.date | None = None) -> dict:
    """
    Score every release and roll the results up per theme.
    Returns {"releases": [...enriched...], "themes": {theme: {...rollup...}}}.
    house_view is accepted for future weighting; the current rules already encode
    its bias, so it is used here only to surface each theme's stance/summary.
    """
    scored = [assess_release(r, today) for r in releases]

    hv_themes = ((house_view or {}).get("themes") or {})
    rollup: dict[str, dict] = {}

    for r in scored:
        theme = r.get("theme")
        if not theme:
            continue
        t = rollup.setdefault(theme, {
            "theme": theme,
            "confirms": 0, "challenges": 0, "in_line": 0,
            "pending": 0, "unscored": 0, "net": 0,
            "stance": (hv_themes.get(theme, {}) or {}).get("stance"),
            "bias": (hv_themes.get(theme, {}) or {}).get("bias"),
            "summary": (hv_themes.get(theme, {}) or {}).get("summary"),
        })
        v = r.get("verdict")
        if v == "Confirms":
            t["confirms"] += 1; t["net"] += 1
        elif v == "Challenges":
            t["challenges"] += 1; t["net"] -= 1
        elif v == "In line":
            t["in_line"] += 1
        elif v in ("Upcoming", "Awaiting print"):
            t["pending"] += 1
        else:
            t["unscored"] += 1

    for t in rollup.values():
        scored_n = t["confirms"] + t["challenges"]
        if t["bias"] in (None, "TO_CONFIRM"):
            t["verdict"] = "House view TBD"
        elif scored_n == 0:
            t["verdict"] = "Awaiting data"
        elif t["net"] > 0:
            t["verdict"] = "Supported"
        elif t["net"] < 0:
            t["verdict"] = "Under pressure"
        else:
            t["verdict"] = "Mixed"

    return {"releases": scored, "themes": rollup}
