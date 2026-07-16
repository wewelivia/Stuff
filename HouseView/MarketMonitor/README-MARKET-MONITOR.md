# Market Monitor — House View extension

A new sub-dashboard for the House View app. It answers three questions at a glance: what's moving markets, where the moves are outsized relative to recent history, and whether the macro regime is shifting.

## What it shows

**Regime quadrant.** Growth and inflation impulses are each a composite z-score of ~3-month changes in their member series (editable in `market_universe.yaml`), standardised against three years of history. The pair maps to goldilocks, reflation, stagflation, or disinflationary slowdown, with a "transitioning" state when signals are near neutral.

**Correlation regime.** Rolling 3-month correlations of daily changes (equity–bond, dollar–gold, equity–credit, rates–dollar) compared with the prior 3 months. Shifts beyond 0.40 or sign flips are flagged — these are often the earliest visible sign of a regime change.

**Outsized moves.** Any series whose latest daily move exceeds 2 sigma versus its own trailing-year distribution of daily moves. Cards are colour-coded by risk direction (spread widening and VIX spikes count as risk-off).

**Full cross-asset table.** 26 series across rates, equities & vol, FX, commodities and credit: last level, 1d/5d/YTD changes (bp for yields and spreads, % for prices), 1d and 5d z-scores with heat colouring, 3-year percentile of the level, and a 60-day sparkline.

## Data

**Bloomberg (primary, `data_source: bbg`).** One batched `HistoricalDataRequest` (PX_LAST, daily, ~3.5y) against the local Terminal — same blpapi setup as `blpapi_provider.py`, no proxy involvement, no keys, no paywall. Tickers are standard: USGG generics for yields, USYC2Y10 for the curve, USGGBE for breakevens, SPX/UKX/SX5E/NKY, DXY and majors, CO1/CL1/XAU/HG1, LUACOAS and LF98OAS for credit. `LP01OAS Index` (Euro HY) is the one ticker to confirm on the Terminal with DES. Units are auto-normalised, so bp-quoted tickers (USYC2Y10, OAS indices) mix safely with percent-quoted ones.

**Web (fallback, `data_source: web`).** FRED + Stooq, no keys. Blocked by the corporate proxy — Python's outbound HTTPS is filtered, which is why every series fails from the Barclays machine. Usable from home, or set `settings.proxy` in the yaml if you have proxy credentials. Three bbg-only series (gilt, bund, Euro Stoxx) are skipped in this mode.

Data is cached for 15 minutes (in memory plus `market_cache.json`), so refreshing the page is cheap. Stale series are dimmed in the table.

## Install

Copy the folder contents into your House View directory (Windows machine with the Terminal running), then:

```
python verify_universe.py        # validation gate, like diagnose_bbg.py
python verify_universe.py web    # optionally test the web fallback
```

Fix any failed tickers in `market_universe.yaml`, then either:

**Integrated** — add two lines to `app.py` and restart uvicorn:

```python
from market_monitor_api import router as market_monitor_router
app.include_router(market_monitor_router)
```

**Standalone** — no change to `app.py`:

```
uvicorn market_monitor_api:app --host 0.0.0.0 --port 8010
```

Open `market-monitor.html` in a browser, click ⚙, and point it at the backend (same convention as the other dashboards; stored in localStorage under `hv-api-base`). On the Windows deployment, this is the machine's IP, exactly as with View Challenges.

## Customising

Everything lives in `market_universe.yaml`: data source (bbg | web), add or remove series, change z-score lookback (252d), trend window (63d), outsized threshold (2.0σ), correlation flag level (0.40), cache TTL, regime composite members and weights. The engine, API and HTML never need touching — same principle as the provider layer.

## Files

- `market_monitor.py` — engine: fetchers (blpapi batch, FRED/Stooq), z-scores, correlation shifts, regime quadrant
- `market_universe.yaml` — editable universe, settings, data source, regime composites, correlation pairs
- `market_monitor_api.py` — APIRouter for app.py, or standalone FastAPI app
- `market-monitor.html` — standalone frontend (light/dark, ⚙ endpoint panel)
- `verify_universe.py` — one-off ticker validation gate (bbg or web mode)
- `test_market_monitor.py` — offline test suite (passes; no network or Terminal needed)

## Interpretation notes

The quadrant classifies the *impulse* (3m change), not the level — a stagflation reading means the mix is deteriorating, not that stagflation has arrived. The 2s10s term carries a half weight in the growth composite and is the most debatable member: re-steepening driven by term premium rather than growth would flatter the score, so cross-check against the correlation panel (rates–dollar and equity–bond behaviour usually reveal which it is). Composites are judgment calls encoded in YAML — adjust the weights if they disagree with your read.
