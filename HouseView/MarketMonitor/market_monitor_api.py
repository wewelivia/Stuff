"""
Market Monitor API — two ways to run it.

1. INTEGRATED into the existing House View app (recommended).
   Add two lines to app.py:

       from market_monitor_api import router as market_monitor_router
       app.include_router(market_monitor_router)

2. STANDALONE, without touching app.py:

       uvicorn market_monitor_api:app --host 0.0.0.0 --port 8010

Either way the endpoint is GET /api/market-monitor
(?refresh=1 forces a bypass of the cache).
"""

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from market_monitor import build_monitor

router = APIRouter()


@router.get("/api/market-monitor")
def market_monitor(refresh: int = 0):
    return build_monitor(use_cache=not bool(refresh))


# ---- standalone app (mirrors House View CORS behaviour for file:// HTML) ----
app = FastAPI(title="House View — Market Monitor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
