"""FastAPI routes for the sentiment tab.

Mount on the existing House View app:

    from api_sentiment import router as sentiment_router
    app.include_router(sentiment_router)

Or run standalone:

    uvicorn api_sentiment:app --host 0.0.0.0 --port 8010
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yaml
from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import sentiment_engine as eng
import sentiment_stats as stats
from providers import SeriesSpec, SeriesStore
from sentiment_builder import SentimentBuilder, load_config

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sentiment", tags=["sentiment"])

HERE = os.path.dirname(os.path.abspath(__file__))
APP_CONFIG = os.path.join(HERE, "config", "sentiment_config.yaml")
TICKERS = os.path.join(HERE, "config", "sentiment_tickers.yaml")

_lock = threading.Lock()
_state: Dict[str, object] = {}

# A cold build fetches around sixty series and takes minutes. Running that
# inside a request leaves the page on its placeholders with no explanation, so
# it runs on a worker thread and the tab polls /status.
_build: Dict[str, object] = {"running": False, "started": None, "finished": None,
                             "error": None, "stage": "idle"}


def _app_config() -> dict:
    with open(APP_CONFIG, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _benchmark(store: SeriesStore, cfg: dict) -> pd.Series:
    bm = cfg.get("benchmark", {})
    spec = SeriesSpec(source=bm.get("source", "bloomberg"),
                      code=bm.get("code", "SPX Index"),
                      field=bm.get("field", "PX_LAST"))
    return store.get(spec, refresh=True)


def rebuild(refresh: bool = True) -> Dict[str, object]:
    """Fetch, transform, aggregate and evaluate. Cached in module state."""
    cfg = _app_config()
    tickers = load_config(TICKERS)

    start = dt.date.fromisoformat(
        str(tickers.get("defaults", {}).get("history_start", "2004-01-01")))
    store = SeriesStore(
        cache_dir=cfg.get("cache_dir", os.path.join(HERE, "cache")),
        history_start=start,
        enable_bloomberg=cfg.get("enable_bloomberg", True),
        enable_macrobond=cfg.get("enable_macrobond", True))

    builder = SentimentBuilder(tickers, store)
    inputs, report = builder.build(refresh=refresh)
    if not inputs:
        store.close()
        raise RuntimeError("no inputs could be built")

    engine = eng.SentimentEngine(inputs)
    results = engine.compute_all()
    prices = _benchmark(store, cfg)
    provider_status = store.provider_status()
    store.close()

    published = {"sell": 20, "buy": 13}
    payload: Dict[str, object] = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "build": report.as_dict(),
        "providers": provider_status,
        "published_denominator": published,
        "sides": {},
    }

    for key, res in results.items():
        side, mode = key.split("_", 1)
        reading = res.reading.dropna()
        if reading.empty:
            continue

        calibrated = stats.calibrate_bands(reading)
        bands_published = eng.label_bands(res.reading)
        signal = (res.reading >= calibrated.get("Strong", 0.4)).fillna(False)

        entry: Dict[str, object] = {
            "latest": _latest_block(res, reading, bands_published, calibrated,
                                    published.get(side)),
            "bands": {
                "published": {"Mild": 0.20, "Moderate": 0.30, "Strong": 0.40, "Extreme": 0.50},
                "calibrated": calibrated,
                "ci": _frame_to_records(stats.calibrate_bands_ci(reading, n_boot=200)),
            },
        }

        if not prices.empty:
            entry["evaluation"] = _evaluation_block(prices, res, reading, bands_published,
                                                    side, cfg)
        payload["sides"][key] = entry

    payload["_series"] = {k: v.reading for k, v in results.items()}
    payload["_inputs"] = {i.id: {"label": i.label, "cluster": i.cluster,
                                 "is_substitute": i.is_substitute} for i in inputs}
    payload["_results"] = results
    payload["_prices"] = prices
    return payload


def _latest_block(res, reading, bands_published, calibrated, published_denom) -> Dict[str, object]:
    last = reading.index[-1]
    value = float(reading.iloc[-1])
    denom = int(res.denominator.loc[last]) if last in res.denominator.index else None
    dropped = list(res.dropped.loc[last]) if last in res.dropped.index else []

    calibrated_band = "No signal"
    for name in ("Mild", "Moderate", "Strong", "Extreme"):
        threshold = calibrated.get(name)
        if threshold is not None and not pd.isna(threshold) and value >= threshold:
            calibrated_band = name

    return {
        "date": str(last.date()),
        "reading": value,
        "band_published": bands_published.loc[last] if last in bands_published.index else None,
        "band_calibrated": calibrated_band,
        "denominator": denom,
        "published_denominator": published_denom,
        "fired_count": int(res.fired_count.loc[last]) if last in res.fired_count.index else None,
        "dropped_inputs": dropped,
        "inputs": _input_rows(res, last),
    }


def _input_rows(res, last) -> List[Dict[str, object]]:
    rows = []
    for col in res.per_input_rank.columns:
        rank = res.per_input_rank.at[last, col] if last in res.per_input_rank.index else np.nan
        if pd.isna(rank):
            rows.append({"id": col, "available": False})
            continue
        rows.append({
            "id": col, "available": True,
            "percentile": float(rank),
            "hinge": float(res.per_input_hinge.at[last, col]),
            "fired": bool(res.per_input_fired.at[last, col]),
        })
    return sorted(rows, key=lambda r: (-r.get("hinge", -1), r["id"]))


def _evaluation_block(prices, res, reading, bands_published, side, cfg) -> Dict[str, object]:
    horizons = cfg.get("horizons", [21, 63, 126])
    calibrated = stats.calibrate_bands(reading)
    signal = (res.reading >= calibrated.get("Strong", 0.4)).fillna(False)

    by_horizon = {}
    for h in horizons:
        try:
            by_horizon[str(h)] = stats.evaluate_signal(
                prices, signal, side, int(h), n_boot=cfg.get("bootstrap", 200)).to_dict()
        except Exception as exc:
            log.warning("evaluation failed at horizon %s: %s", h, exc)

    headline = int(cfg.get("headline_horizon", 63))
    table = stats.evaluate_by_band(prices, reading, bands_published, side, headline)

    denom = res.per_input_rank.shape[1]
    fire_prob = float(res.fired_count.dropna().mean() / denom) if denom else 0.1
    redundancy = stats.redundancy_ratio(reading, denom, max(fire_prob, 0.01))

    return {
        "by_horizon": by_horizon,
        "band_table": _frame_to_records(table),
        "headline_horizon": headline,
        "redundancy": redundancy,
    }


def _frame_to_records(df: Optional[pd.DataFrame]) -> List[Dict[str, object]]:
    if df is None or df.empty:
        return []
    return df.replace({np.nan: None}).to_dict(orient="records")


def _run_build(refresh: bool) -> None:
    _build.update(running=True, started=dt.datetime.now().isoformat(timespec="seconds"),
                  finished=None, error=None, stage="fetching series")
    try:
        payload = rebuild(refresh=refresh)
        with _lock:
            _state["payload"] = payload
        _build.update(stage="ready", error=None)
    except Exception as exc:
        log.exception("build failed")
        _build.update(stage="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        _build.update(running=False, finished=dt.datetime.now().isoformat(timespec="seconds"))


def start_build(refresh: bool = True) -> bool:
    """Kick off a background build if one is not already running."""
    if _build.get("running"):
        return False
    threading.Thread(target=_run_build, args=(refresh,), daemon=True).start()
    return True


def _payload() -> Optional[Dict[str, object]]:
    with _lock:
        return _state.get("payload")


# --- routes ----------------------------------------------------------------
@router.get("/status")
def get_status() -> Dict[str, object]:
    """Fast, never triggers a build. The tab polls this while waiting."""
    payload = _payload()
    return {
        "ready": payload is not None,
        "building": bool(_build.get("running")),
        "stage": _build.get("stage"),
        "started": _build.get("started"),
        "finished": _build.get("finished"),
        "error": _build.get("error"),
        "generated": payload.get("generated") if payload else None,
        "n_inputs": (payload.get("build", {}) or {}).get("n_built") if payload else None,
    }


@router.get("/")
def get_sentiment(refresh: bool = Query(False)) -> Dict[str, object]:
    payload = _payload()

    if refresh or payload is None:
        started = start_build(refresh=True)
        if payload is None:
            raise HTTPException(
                status_code=202,
                detail=("Building. A cold cache fetches around sixty series and takes "
                        "several minutes. Run warm_cache.py first to avoid this. "
                        + ("Build started." if started else "Build already running.")))

    return {k: v for k, v in payload.items() if not k.startswith("_")}


@router.get("/history")
def get_history(side: str = Query("sell"), mode: str = Query("replica"),
                start: Optional[str] = Query(None)) -> Dict[str, object]:
    payload = _payload()
    if payload is None:
        raise HTTPException(status_code=202, detail="not built yet")
    key = f"{side}_{mode}"
    series = payload.get("_series", {}).get(key)
    if series is None:
        raise HTTPException(status_code=404, detail=f"unknown series {key}")

    s = series.dropna()
    if start:
        s = s.loc[s.index >= pd.Timestamp(start)]

    bands = stats.walk_forward_bands(series).reindex(s.index)
    return {
        "key": key,
        "dates": [str(d.date()) for d in s.index],
        "values": [float(v) for v in s.values],
        "walk_forward_bands": {
            col: [None if pd.isna(v) else float(v) for v in bands[col].values]
            for col in bands.columns},
    }


@router.get("/inputs")
def get_inputs() -> Dict[str, object]:
    payload = _payload()
    if payload is None:
        raise HTTPException(status_code=202, detail="not built yet")
    return {"inputs": payload.get("_inputs", {}), "build": payload.get("build", {})}


@router.get("/health")
def health() -> Dict[str, object]:
    """Fast. Reports state without triggering a build."""
    payload = _payload()
    return {
        "ok": payload is not None,
        "building": bool(_build.get("running")),
        "stage": _build.get("stage"),
        "error": _build.get("error"),
        "generated": payload.get("generated") if payload else None,
        "providers": payload.get("providers") if payload else None,
        "n_inputs": (payload.get("build", {}) or {}).get("n_built") if payload else None,
    }




app = FastAPI(title="House View Sentiment")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
app.include_router(router)


@app.on_event("startup")
def _startup() -> None:
    """Begin building as soon as the server is up, so someone opening the tab
    waits on work already in progress rather than starting it."""
    start_build(refresh=True)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the tab from the backend.

    Lets the hub use `served: true`, which means the page finds its own API at
    window.location.origin with no endpoint configuration, and the hub link
    does not depend on a relative path into this folder.
    """
    page = os.path.join(HERE, "sentiment.html")
    if not os.path.exists(page):
        raise HTTPException(status_code=404, detail="sentiment.html not found")
    # No-store: the page changes far more often than a browser cache expects,
    # and a stale copy is indistinguishable from a broken backend.
    return FileResponse(page, media_type="text/html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })
