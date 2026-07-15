"""
app.py - FastAPI backend AND static host for the House View dashboard.

One process, one machine. It serves the two HTML pages and the JSON API from the
same origin, so there is no CORS and no file:// pages pointing at a remote IP.

Endpoints
---------
  GET /                          -> view-challenges.html
  GET /view-challenges.html      -> view-challenges.html
  GET /house-view-dashboard.html -> house-view-dashboard.html
  GET /api/health                -> {ok, provider_mode}
  GET /api/house-view            -> the house view (themes, narrative, meta)
  GET /api/view-challenges       -> scored releases + theme rollup + as_of

Run
---
  uvicorn app:app --host 127.0.0.1 --port 8000
  open http://127.0.0.1:8000/
"""

from __future__ import annotations

import datetime
import os

import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

import challenge_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="House View - View Challenges")


# ---- config + house view -----------------------------------------------------
def load_config() -> dict:
    path = os.path.join(BASE_DIR, "config.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_house_view() -> dict:
    path = os.path.join(BASE_DIR, "house_view.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def get_provider(config: dict):
    """Return the data provider for the configured mode. Env var wins if set."""
    mode = os.environ.get("HV_PROVIDER_MODE") or config.get("provider_mode", "snapshot")
    mode = mode.lower()
    if mode == "blpapi":
        from providers.blpapi_provider import BlpapiProvider
        return BlpapiProvider(config), mode
    elif mode == "snapshot":
        from providers.snapshot_provider import SnapshotProvider
        return SnapshotProvider(config), mode
    else:
        from providers.mock_provider import MockProvider
        return MockProvider(config), mode


def build_challenges() -> dict:
    config = load_config()
    house_view = load_house_view()
    provider, mode = get_provider(config)

    all_releases = provider.get_releases()

    # Scope the dashboard to a rolling window: score the recent past, flag what is due.
    win = config.get("window", {}) or {}
    lookback = int(win.get("lookback_days", 10))
    lookahead = int(win.get("lookahead_days", 5))
    today = datetime.date.today()

    releases = challenge_engine.filter_window(all_releases, lookback, lookahead, today)
    result = challenge_engine.assess_releases(releases, house_view, today)

    as_of = getattr(provider, "as_of", None) or (house_view.get("meta", {}) or {}).get("as_of")
    result["as_of"] = as_of
    result["provider_mode"] = mode
    result["meta"] = house_view.get("meta", {})
    err = getattr(provider, "fetch_error", None)
    if err:
        result["fetch_error"] = err
    result["window"] = {
        "lookback_days": lookback,
        "lookahead_days": lookahead,
        "from": (today - datetime.timedelta(days=lookback)).isoformat(),
        "to": (today + datetime.timedelta(days=lookahead)).isoformat(),
        "today": today.isoformat(),
        "in_window": len(releases),
        "total_available": len(all_releases),
    }
    return result


# ---- HTML -------------------------------------------------------------------
def _page(name: str):
    path = os.path.join(BASE_DIR, name)
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"error": f"{name} not found next to app.py"}, status_code=404)


@app.get("/")
def home():
    return _page("view-challenges.html")


@app.get("/view-challenges.html")
def view_challenges_page():
    return _page("view-challenges.html")


@app.get("/house-view-dashboard.html")
def house_view_page():
    return _page("house-view-dashboard.html")


# ---- API --------------------------------------------------------------------
@app.get("/api/health")
def health():
    config = load_config()
    mode = os.environ.get("HV_PROVIDER_MODE") or config.get("provider_mode", "snapshot")
    return {"ok": True, "provider_mode": mode}


@app.get("/api/house-view")
def api_house_view():
    return load_house_view()


@app.get("/api/view-challenges")
def api_view_challenges():
    try:
        return build_challenges()
    except Exception as exc:  # surface the error in the UI rather than a blank page
        return JSONResponse(
            {"error": str(exc), "releases": [], "themes": {}},
            status_code=500,
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
