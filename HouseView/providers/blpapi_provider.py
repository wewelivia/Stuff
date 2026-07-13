"""
BlpapiProvider - live Bloomberg data source for the View Challenges dashboard.

WHY THIS EXISTS
---------------
Bloomberg carries economic-release consensus natively, which no free calendar API
does. On the work PC (Bloomberg Terminal + blpapi installed) this provider serves
the standard get_releases() contract live off the Terminal. On a machine without
Bloomberg (e.g. the Mac) you run provider_mode: snapshot instead. Same contract,
same engine, same HTML. Nothing downstream changes.

WHAT IT SOLVES (from the Phase 1 diagnosis)
-------------------------------------------
The SNAPSHOT field ACTUAL_RELEASE came back blank; only ECO_FUTURE_RELEASE_DATE
resolved. Historical actuals existed but were keyed to PERIOD-END dates
(e.g. 2026-05-31), not PUBLISH dates. This provider fetches the publish-date list
per ticker over a window (ECO_FUTURE_RELEASE_DATE_LIST with START_DT / END_DT
overrides) and remaps each period-end actual to its publish date: the earliest
calendar date in the release-date list that is on or after the period-end.

CONTRACT (identical to every other provider)
--------------------------------------------
  event, region, pillar, theme, impact, assets, higher_is, unit,
  date ("%d %b %Y"), date_iso, consensus, actual, previous, status
Records are returned sorted by date_iso DESCENDING. Released items carry
actual + consensus + previous; Upcoming items leave consensus/actual null.

STATIC vs LIVE
--------------
The static metadata (event, region, pillar, theme, impact, assets, higher_is,
unit) comes from each release entry in config.yaml. Only the numbers and the
publish date/status are pulled live from Bloomberg and merged in.

FIELD MNEMONICS - VERIFY ON THE TERMINAL
----------------------------------------
Bloomberg field names for economic releases vary by ticker family. The mnemonics
below are the defaults; override any of them in config.yaml under blpapi.fields
after confirming with FLDS<GO> on the actual tickers. Getting these wrong is the
single most likely reason a print shows blank, so treat the defaults as a starting
point, not gospel.

GRACEFUL DEGRADATION
--------------------
If the session cannot start (Terminal closed, blpapi missing, wrong host/port) the
provider does not crash the dashboard. If a local snapshot file is present it
serves that as a fallback; otherwise it returns an empty calendar. This matches the
project's "never break the dashboard" behaviour.
"""

from __future__ import annotations

import datetime as _dt
import json
import os

try:
    from providers.mock_provider import MockProvider
except Exception:  # pragma: no cover - tolerate a different import path in your tree
    MockProvider = object

try:
    import blpapi  # type: ignore
    _HAVE_BLPAPI = True
except Exception:  # pragma: no cover - not installed on the Mac; snapshot mode is used there
    blpapi = None  # type: ignore
    _HAVE_BLPAPI = False


CONTRACT_FIELDS = (
    "event", "region", "pillar", "theme", "impact", "assets",
    "higher_is", "unit", "date", "date_iso", "consensus",
    "actual", "previous", "status",
)

# Default Bloomberg field mnemonics. Override in config.yaml -> blpapi.fields.
# Confirm each against your real tickers with FLDS<GO> before trusting them.
DEFAULT_FIELDS = {
    "release_date_list": "ECO_FUTURE_RELEASE_DATE_LIST",  # bulk: array of publish dates
    "consensus": "BN_SURVEY_MEDIAN",                      # survey median (consensus)
    "actual": "ACTUAL_RELEASE",                           # actual print
    "previous": "PREVIOUS_VALUE",                         # prior print (or *_REVISED_VALUE)
    "period_end": "ECO_RELEASE_PERIOD_END_DATE",          # period the actual refers to
}

# How far back / forward to pull the release-date list, in days, around "today".
DEFAULT_WINDOW_BACK_DAYS = 120
DEFAULT_WINDOW_FWD_DAYS = 45


def _to_iso(d) -> str | None:
    """Coerce a blpapi/date/datetime/string into 'YYYY-MM-DD' or None."""
    if d is None:
        return None
    if isinstance(d, (_dt.datetime, _dt.date)):
        return d.strftime("%Y-%m-%d")
    s = str(d).strip()
    if not s:
        return None
    # blpapi often hands back 'YYYY-MM-DD' already; guard a couple of variants.
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10] if len(s) >= 10 else None


def _pretty(iso: str | None) -> str | None:
    """'YYYY-MM-DD' -> '%d %b %Y' to match the contract's display date."""
    if not iso:
        return None
    try:
        return _dt.datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return None


class BlpapiProvider(MockProvider):
    """Serve get_releases() live from the Bloomberg Terminal via blpapi."""

    def __init__(self, config: dict | None = None, *args, **kwargs):
        try:
            super().__init__(config, *args, **kwargs)
        except Exception:
            pass

        self.config = config or {}
        blp_cfg = (self.config.get("blpapi") or {})

        self.host = blp_cfg.get("host", "localhost")
        self.port = int(blp_cfg.get("port", 8194))
        self.fields = {**DEFAULT_FIELDS, **(blp_cfg.get("fields") or {})}
        self.ticker_key = blp_cfg.get("ticker_key", "ticker")   # config release -> Bloomberg id
        self.window_back = int(blp_cfg.get("window_back_days", DEFAULT_WINDOW_BACK_DAYS))
        self.window_fwd = int(blp_cfg.get("window_fwd_days", DEFAULT_WINDOW_FWD_DAYS))

        # Optional snapshot fallback so a closed Terminal never blanks the dashboard.
        self.snapshot_fallback = blp_cfg.get(
            "snapshot_fallback",
            os.path.join(os.path.dirname(__file__), "releases_snapshot.json"),
        )

        self._releases_cfg = self.config.get("releases", []) or []

    # -- the only "live" method ---------------------------------------------
    def get_releases(self):
        try:
            live = self._fetch_live()
        except Exception as exc:  # never crash the dashboard
            print(f"[blpapi] live fetch failed ({exc!r}); attempting snapshot fallback")
            live = self._snapshot_fallback()

        for r in live:
            for f in CONTRACT_FIELDS:
                r.setdefault(f, None)

        live.sort(key=lambda r: (r.get("date_iso") or ""), reverse=True)
        return live

    # -- live path -----------------------------------------------------------
    def _fetch_live(self):
        if not _HAVE_BLPAPI:
            raise RuntimeError("blpapi not installed on this machine")
        if not self._releases_cfg:
            raise RuntimeError("no 'releases' defined in config.yaml")

        session = self._open_session()
        try:
            svc = session.getService("//blp/refdata")

            today = _dt.date.today()
            start = (today - _dt.timedelta(days=self.window_back)).strftime("%Y%m%d")
            end = (today + _dt.timedelta(days=self.window_fwd)).strftime("%Y%m%d")

            out = []
            for entry in self._releases_cfg:
                ticker = entry.get(self.ticker_key) or entry.get("bbg_ticker")
                if not ticker:
                    continue  # a release with no Bloomberg id is skipped, not fatal
                merged = self._fetch_one(session, svc, ticker, entry, start, end)
                if merged:
                    out.append(merged)
            return out
        finally:
            try:
                session.stop()
            except Exception:
                pass

    def _open_session(self):
        opts = blpapi.SessionOptions()
        opts.setServerHost(self.host)
        opts.setServerPort(self.port)
        session = blpapi.Session(opts)
        if not session.start():
            raise RuntimeError(f"could not start blpapi session on {self.host}:{self.port}")
        if not session.openService("//blp/refdata"):
            raise RuntimeError("could not open //blp/refdata")
        return session

    def _fetch_one(self, session, svc, ticker, entry, start_dt, end_dt):
        """Pull the live numbers for one ticker and merge with its static metadata."""
        f = self.fields
        req = svc.createRequest("ReferenceDataRequest")
        req.append("securities", ticker)
        for key in ("consensus", "actual", "previous", "period_end", "release_date_list"):
            req.append("fields", f[key])

        # Override the release-date list window so we get publish dates around now.
        for name, val in (("START_DT", start_dt), ("END_DT", end_dt)):
            ov = req.getElement("overrides").appendElement()
            ov.setElement("fieldId", name)
            ov.setElement("value", val)

        data = self._request(session, req, ticker)
        if data is None:
            return None

        actual = data.get(f["actual"])
        consensus = data.get(f["consensus"])
        previous = data.get(f["previous"])
        period_end = _to_iso(data.get(f["period_end"]))
        publish_dates = [_to_iso(d) for d in (data.get(f["release_date_list"]) or [])]
        publish_dates = sorted(d for d in publish_dates if d)

        publish_iso = self._remap_publish_date(period_end, publish_dates)
        status = "Released" if actual not in (None, "") else "Upcoming"
        if status == "Upcoming":
            actual = None  # do not carry a stale/blank actual on an upcoming print

        return {
            # static metadata straight from config.yaml
            "event": entry.get("event"),
            "region": entry.get("region"),
            "pillar": entry.get("pillar"),
            "theme": entry.get("theme"),
            "impact": entry.get("impact"),
            "assets": entry.get("assets"),
            "higher_is": entry.get("higher_is"),
            "unit": entry.get("unit"),
            # live values from Bloomberg
            "date": _pretty(publish_iso),
            "date_iso": publish_iso,
            "consensus": consensus,
            "actual": actual,
            "previous": previous,
            "status": status,
            "source": f"Bloomberg {ticker} ({f['actual']})",
        }

    @staticmethod
    def _remap_publish_date(period_end_iso, publish_dates):
        """Earliest publish date on or after the period-end. Falls back sensibly."""
        if publish_dates and period_end_iso:
            for d in publish_dates:  # already sorted ascending
                if d >= period_end_iso:
                    return d
            return publish_dates[-1]
        if publish_dates:
            return publish_dates[-1]
        return period_end_iso

    # -- blpapi event-loop plumbing -----------------------------------------
    # Minimal and synchronous: send one request, drain events until RESPONSE,
    # return a flat {field_name: value} dict (arrays become Python lists).
    @staticmethod
    def _request(session, request, ticker):
        session.sendRequest(request)
        out = {}
        while True:
            ev = session.nextEvent(5000)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                secs = msg.getElement("securityData")
                for i in range(secs.numValues()):
                    sd = secs.getValueAsElement(i)
                    if sd.hasElement("securityError"):
                        print(f"[blpapi] securityError for {ticker}")
                        return None
                    fd = sd.getElement("fieldData")
                    for j in range(fd.numElements()):
                        el = fd.getElement(j)
                        name = str(el.name())
                        if el.isArray():
                            vals = []
                            for k in range(el.numValues()):
                                row = el.getValueAsElement(k)
                                if row.numElements():
                                    vals.append(row.getElement(0).getValue())
                            out[name] = vals
                        else:
                            out[name] = el.getValue()
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
        return out

    # -- snapshot fallback ---------------------------------------------------
    def _snapshot_fallback(self):
        path = self.snapshot_fallback
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    return list(data.get("releases", []))
                return list(data)
            except Exception:
                return []
        return []


if __name__ == "__main__":
    # Smoke test on the work PC:  python -m providers.blpapi_provider
    import yaml  # type: ignore
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    prov = BlpapiProvider(cfg)
    rows = prov.get_releases()
    print(f"{len(rows)} releases")
    for r in rows[:5]:
        print(r.get("date_iso"), r.get("event"), "actual=", r.get("actual"),
              "cons=", r.get("consensus"), r.get("status"))
