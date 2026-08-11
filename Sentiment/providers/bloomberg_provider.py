"""Bloomberg provider. HistoricalDataRequest via blpapi on //blp/refdata."""

from __future__ import annotations

import datetime as dt
from typing import List, Optional, Tuple

import pandas as pd

from .base import FetchResult, Provider, SeriesSpec

try:
    import blpapi
    HAVE_BLPAPI = True
except Exception:
    blpapi = None
    HAVE_BLPAPI = False


class BloombergProvider(Provider):
    name = "bloomberg"

    def __init__(self, host: str = "localhost", port: int = 8194, timeout_ms: int = 30000):
        if not HAVE_BLPAPI:
            raise ImportError("blpapi is not available")
        self.timeout_ms = timeout_ms
        opts = blpapi.SessionOptions()
        opts.setServerHost(host)
        opts.setServerPort(port)
        self._session = blpapi.Session(opts)
        if not self._session.start():
            raise RuntimeError("failed to start blpapi session")
        if not self._session.openService("//blp/refdata"):
            raise RuntimeError("failed to open //blp/refdata")
        self._svc = self._session.getService("//blp/refdata")

    def close(self) -> None:
        try:
            self._session.stop()
        except Exception:
            pass

    def _send(self, request) -> List[object]:
        self._session.sendRequest(request)
        messages = []
        while True:
            ev = self._session.nextEvent(self.timeout_ms)
            for msg in ev:
                messages.append(msg)
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
            if ev.eventType() == blpapi.Event.TIMEOUT:
                raise TimeoutError("blpapi request timed out")
        return messages

    def fetch(self, spec: SeriesSpec, start: dt.date, end: dt.date) -> FetchResult:
        req = self._svc.createRequest("HistoricalDataRequest")
        req.getElement("securities").appendValue(spec.code)
        req.getElement("fields").appendValue(spec.field)
        req.set("startDate", start.strftime("%Y%m%d"))
        req.set("endDate", end.strftime("%Y%m%d"))
        req.set("periodicitySelection", "DAILY")
        req.set("nonTradingDayFillOption", "ACTIVE_DAYS_ONLY")

        points: List[Tuple[dt.date, float]] = []
        error: Optional[str] = None

        try:
            messages = self._send(req)
        except Exception as exc:
            return FetchResult(spec, pd.Series(dtype=float), f"{type(exc).__name__}: {exc}")

        for msg in messages:
            if not msg.hasElement("securityData"):
                continue
            sd = msg.getElement("securityData")

            if sd.hasElement("securityError"):
                se = sd.getElement("securityError")
                error = f"security: {se.getElementAsString('message')}"
                continue

            fx = sd.getElement("fieldExceptions") if sd.hasElement("fieldExceptions") else None
            if fx is not None and fx.numValues() > 0:
                info = fx.getValueAsElement(0).getElement("errorInfo")
                error = f"field: {info.getElementAsString('message')}"

            if not sd.hasElement("fieldData"):
                continue
            fd = sd.getElement("fieldData")
            for i in range(fd.numValues()):
                row = fd.getValueAsElement(i)
                if not row.hasElement(spec.field):
                    continue
                d = row.getElementAsDatetime("date")
                try:
                    v = row.getElementAsFloat(spec.field)
                except Exception:
                    continue
                points.append((dt.date(d.year, d.month, d.day), v))

        if not points:
            return FetchResult(spec, pd.Series(dtype=float), error or "no observations")

        points.sort(key=lambda p: p[0])
        s = pd.Series([v for _, v in points], index=pd.to_datetime([d for d, _ in points]))
        return FetchResult(spec, s, None)
