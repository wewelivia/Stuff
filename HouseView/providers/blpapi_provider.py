"""
BlpapiProvider - live Bloomberg data source, built on what the Terminal actually has.

THE DIAGNOSIS THIS IS BUILT ON
------------------------------
diagnose_bbg.py established, against the real Terminal:

  ACTUAL_RELEASE                 FIELD NOT VALID   (on every ticker)
  PREVIOUS_VALUE                 FIELD NOT VALID   (on every ticker)
  ECO_RELEASE_PERIOD_END_DATE    FIELD NOT VALID   (on every ticker)
  BN_SURVEY_MEDIAN (reference)   empty
  ECO_FUTURE_RELEASE_DATE_LIST   OK, ~36 publish dates
  ECO_RELEASE_DT                 OK, the next scheduled release date

  HISTORY (the important part):
  PX_LAST           4 rows   2026-04-30=3.8, 2026-05-31=4.2, 2026-06-30=3.5
  BN_SURVEY_MEDIAN  4 rows   2026-04-30=3.7, 2026-05-31=4.2, 2026-06-30=3.8

So the actual is NOT a reference field on economic-release securities. It lives in
history, keyed to the PERIOD-END date. Critically, BN_SURVEY_MEDIAN historises too,
on the same period-end key, which is what makes scoring PAST surprises possible.

HOW IT WORKS
------------
Per ticker, two requests:

  1. HistoricalDataRequest  PX_LAST + BN_SURVEY_MEDIAN over the lookback window.
     Each returned row IS a release: date=period end, PX_LAST=actual,
     BN_SURVEY_MEDIAN=consensus. `previous` is simply the prior row's PX_LAST,
     so no separate field is needed.

  2. ReferenceDataRequest   ECO_FUTURE_RELEASE_DATE_LIST + ECO_RELEASE_DT
     for the publish dates and the next scheduled release.

Then the Phase 1 remap joins them: a print for period-end 2026-06-30 is stamped with
the earliest publish date on or after it (mid-July), because the dashboard's window
is about when a number HIT THE SCREEN, not which month it describes.

Finally one Upcoming record per ticker from ECO_RELEASE_DT, carrying the last actual
as `previous`. Forward consensus is usually absent (BN_SURVEY_MEDIAN is empty as a
reference field), so it is left None rather than invented.

CONTRACT (unchanged, as every provider must serve)
--------------------------------------------------
  event, region, pillar, theme, impact, assets, higher_is, unit,
  date ("%d %b %Y"), date_iso, consensus, actual, previous, status
"""

from __future__ import annotations

import datetime as _dt
import json
import os

try:
    from providers.mock_provider import MockProvider
except Exception:  # pragma: no cover
    MockProvider = object

try:
    import blpapi  # type: ignore
    _HAVE_BLPAPI = True
except Exception:  # pragma: no cover - not installed on the Mac
    blpapi = None  # type: ignore
    _HAVE_BLPAPI = False


CONTRACT_FIELDS = (
    "event", "region", "pillar", "theme", "impact", "assets",
    "higher_is", "unit", "date", "date_iso", "consensus",
    "actual", "previous", "status",
)

# Verified against the Terminal by diagnose_bbg.py. Override in config.yaml if a
# particular ticker family needs something different.
DEFAULT_FIELDS = {
    "actual_hist": "PX_LAST",                            # historical: the print
    "consensus_hist": "BN_SURVEY_MEDIAN",                # historical: the survey
    "release_date_list": "ECO_FUTURE_RELEASE_DATE_LIST",  # reference: publish dates
    "next_release": "ECO_RELEASE_DT",                    # reference: next release
}

DEFAULT_WINDOW_BACK_DAYS = 120
DEFAULT_WINDOW_FWD_DAYS = 45


def _to_iso(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, (_dt.datetime, _dt.date)):
        return d.strftime("%Y-%m-%d")
    s = str(d).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10] if len(s) >= 10 else None


def _pretty(iso: str | None) -> str | None:
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
        self.ticker_key = blp_cfg.get("ticker_key", "ticker")
        self.window_back = int(blp_cfg.get("window_back_days", DEFAULT_WINDOW_BACK_DAYS))
        self.window_fwd = int(blp_cfg.get("window_fwd_days", DEFAULT_WINDOW_FWD_DAYS))

        self.snapshot_fallback = blp_cfg.get(
            "snapshot_fallback",
            os.path.join(os.path.dirname(__file__), "releases_snapshot.json"),
        )

        self._releases_cfg = self.config.get("releases", []) or []
        self.fetch_error = None   # surfaced to the dashboard when a live pull fails
        self.as_of = None         # set per fetch: live timestamp, or the fallback's date

    # -- public --------------------------------------------------------------
    def get_releases(self):
        self.fetch_error = None
        self.as_of = None
        try:
            live = self._fetch_live()
            if not live:
                # An empty live result is not "calm markets", it is a failure.
                self.fetch_error = ("Bloomberg returned no usable releases. "
                                    "Check the uvicorn console for per-ticker errors, "
                                    "then run: python -m providers.blpapi_provider")
                live = self._snapshot_fallback()
            else:
                # Live data really is current, so say so honestly.
                self.as_of = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        except Exception as exc:
            self.fetch_error = f"Bloomberg fetch failed: {exc}"
            print(f"[blpapi] {self.fetch_error}; attempting snapshot fallback")
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
            end = today.strftime("%Y%m%d")

            out = []
            for entry in self._releases_cfg:
                ticker = entry.get(self.ticker_key) or entry.get("bbg_ticker")
                if not ticker:
                    continue
                try:
                    out.extend(self._fetch_one(session, svc, ticker, entry,
                                               start, end, today))
                except Exception as exc:
                    # One bad ticker must not take down the whole calendar.
                    print(f"[blpapi] {ticker}: {exc}")
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

    def _fetch_one(self, session, svc, ticker, entry, start, end, today):
        f = self.fields

        # 1. History: every row here IS a release (period-end keyed).
        hist = self._history(session, svc, ticker,
                             [f["actual_hist"], f["consensus_hist"]], start, end)

        # 2. Reference: publish dates + next scheduled release. The overrides are
        #    essential: without them the release-date list is future-only.
        fwd_end = (today + _dt.timedelta(days=self.window_fwd)).strftime("%Y%m%d")
        ref = self._reference(session, svc, ticker,
                              [f["release_date_list"], f["next_release"]],
                              overrides={"START_DT": start, "END_DT": fwd_end})

        publish_dates = sorted(
            d for d in (_to_iso(x) for x in (ref.get(f["release_date_list"]) or []))
            if d
        )

        # 3. Dense series (policy rates): PX_LAST daily history returns a row for
        #    EVERY day the rate was in force, not just decision days, so one rate
        #    ticker floods the window with ~120 identical rows. Three defences, in
        #    order, so this cannot depend on config being right:
        #      a) explicit `same_day: true` in config.yaml
        #      b) auto-detect: history far denser than the release calendar
        #      c) if the history dates do not line up with the release calendar,
        #         fall back to keeping only the days the value actually moved
        same_day = entry.get("same_day")
        if same_day is None:
            same_day = bool(publish_dates) and len(hist) > 3 * len(publish_dates)

        if same_day:
            pub_set = set(publish_dates)
            on_calendar = [r for r in hist if r.get("date") in pub_set]
            raw = len(hist)
            if on_calendar:
                hist = on_calendar
                how = f"matched {len(hist)} of {len(publish_dates)} calendar dates"
            else:
                # Decision dates did not match the history index. Rather than drop
                # the ticker or flood the window, keep the days the rate changed.
                hist = self._dedupe_changes(hist, f["actual_hist"])
                how = (f"calendar dates did not match history; "
                       f"kept {len(hist)} value changes")
            pub_map = {r["date"]: r["date"] for r in hist if r.get("date")}
            print(f"[blpapi] {ticker}: dense series, {raw} raw rows -> {how}")
        else:
            period_ends = [r.get("date") for r in hist if r.get("date")]
            pub_map = self._map_publish_dates(period_ends, publish_dates)

        records = []
        prev_actual = None

        for row in hist:                       # ascending by period end
            period_end = row.get("date")
            actual = row.get(f["actual_hist"])
            consensus = row.get(f["consensus_hist"])
            if period_end is None or actual is None:
                continue

            publish_iso = pub_map.get(period_end, period_end)
            records.append(self._record(entry, publish_iso, actual, consensus,
                                        prev_actual, "Released", ticker, period_end))
            prev_actual = actual

        # 3. The next scheduled print. Consensus is normally unavailable ahead of
        #    time on these tickers, so it stays None rather than being invented.
        next_iso = _to_iso(ref.get(f["next_release"]))
        if next_iso and next_iso > today.isoformat():
            records.append(self._record(entry, next_iso, None, None,
                                        prev_actual, "Upcoming", ticker, None))

        return records

    def _record(self, entry, date_iso, actual, consensus, previous, status,
                ticker, period_end):
        return {
            # static metadata from config.yaml
            "event": entry.get("event"),
            "region": entry.get("region"),
            "pillar": entry.get("pillar"),
            "theme": entry.get("theme"),
            "impact": entry.get("impact"),
            "assets": entry.get("assets"),
            "higher_is": entry.get("higher_is"),
            "unit": entry.get("unit"),
            # live values
            "date": _pretty(date_iso),
            "date_iso": date_iso,
            "consensus": consensus,
            "actual": actual,
            "previous": previous,
            "status": status,          # the engine re-derives this from the date
            "period_end": period_end,  # kept for audit: which month the print covers
            "source": f"Bloomberg {ticker}",
        }

    @staticmethod
    def _dedupe_changes(rows, value_field):
        """
        Keep only the rows where the value moved.

        Last-resort defence for continuously-quoted series whose history index does
        not line up with the release calendar. A policy rate held at 3.8% for four
        months is one event, not eighty. This loses "held again" meetings, which is
        why matching the release calendar is preferred, but it beats flooding.
        """
        out = []
        last = object()
        for r in rows:
            v = r.get(value_field)
            if v != last:
                out.append(r)
                last = v
        return out

    @staticmethod
    def _map_publish_dates(period_ends, publish_dates):
        """
        Assign each period-end its publish date, walking both lists forward together.

        Why not just "earliest publish date on or after the period-end" per row: when
        a period-end lands exactly on a release date, that rule hands the SAME publish
        date to two consecutive prints (seen with Core PCE: period-end 31 Mar and
        30 Apr both mapping to 30 Apr). Consuming the list monotonically guarantees
        each print gets its own, strictly later, publish date.

        A print for period-end 30 Jun hits the screen in mid-July. The dashboard's
        window is about when the number LANDED, not which month it describes.

        If the release-date list runs out, the period-end itself is used. That is
        wrong but visible, rather than silently dropping the release.
        """
        out = {}
        cursor = 0
        last = None
        for pe in sorted(period_ends):
            chosen = None
            i = cursor
            while i < len(publish_dates):
                d = publish_dates[i]
                if d >= pe and (last is None or d > last):
                    chosen = d
                    cursor = i + 1
                    break
                i += 1
            out[pe] = chosen if chosen else pe
            last = out[pe]
        return out

    @staticmethod
    def _remap_publish_date(period_end_iso, publish_dates):
        """Single-row version, kept for tests and ad-hoc use."""
        if publish_dates and period_end_iso:
            for d in publish_dates:
                if d >= period_end_iso:
                    return d
            return publish_dates[-1]
        if publish_dates:
            return publish_dates[-1]
        return period_end_iso

    # -- blpapi plumbing -----------------------------------------------------
    @staticmethod
    def _drain(session):
        msgs = []
        while True:
            ev = session.nextEvent(10000)
            for m in ev:
                msgs.append(m)
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
        return msgs

    def _history(self, session, svc, ticker, fields, start, end):
        """HistoricalDataRequest -> [{date, FIELD: value, ...}] ascending."""
        req = svc.createRequest("HistoricalDataRequest")
        req.append("securities", ticker)
        for f in fields:
            req.append("fields", f)
        req.set("startDate", start)
        req.set("endDate", end)
        req.set("periodicitySelection", "DAILY")

        session.sendRequest(req)
        rows = []
        for msg in self._drain(session):
            if not msg.hasElement("securityData"):
                continue
            sd = msg.getElement("securityData")
            if sd.hasElement("securityError"):
                raise RuntimeError("security error in history request")
            fdarr = sd.getElement("fieldData")
            for i in range(fdarr.numValues()):
                el = fdarr.getValueAsElement(i)
                row = {"date": _to_iso(el.getElementAsDatetime("date"))
                       if el.hasElement("date") else None}
                for f in fields:
                    row[f] = el.getElement(f).getValue() if el.hasElement(f) else None
                rows.append(row)
        rows.sort(key=lambda r: r.get("date") or "")
        return rows

    def _reference(self, session, svc, ticker, fields, overrides=None):
        """ReferenceDataRequest -> {FIELD: value}; arrays become Python lists."""
        req = svc.createRequest("ReferenceDataRequest")
        req.append("securities", ticker)
        for f in fields:
            req.append("fields", f)

        # ECO_FUTURE_RELEASE_DATE_LIST lives up to its name: without overrides it
        # returns FUTURE dates only. Historical prints then get stamped with future
        # publish dates and vanish from the window. START_DT/END_DT widen it to cover
        # the lookback too. This is the Phase 1 behaviour; dropping it was a regression.
        if overrides:
            ov_el = req.getElement("overrides")
            for name, val in overrides.items():
                ov = ov_el.appendElement()
                ov.setElement("fieldId", name)
                ov.setElement("value", val)

        session.sendRequest(req)
        out = {}
        for msg in self._drain(session):
            if not msg.hasElement("securityData"):
                continue
            secs = msg.getElement("securityData")
            for i in range(secs.numValues()):
                sd = secs.getValueAsElement(i)
                if sd.hasElement("securityError"):
                    raise RuntimeError("security error in reference request")
                fd = sd.getElement("fieldData")
                for j in range(fd.numElements()):
                    el = fd.getElement(j)
                    name = str(el.name())
                    if el.isArray():
                        vals = []
                        for k in range(el.numValues()):
                            r = el.getValueAsElement(k)
                            if r.numElements():
                                vals.append(r.getElement(0).getValue())
                        out[name] = vals
                    else:
                        out[name] = el.getValue()
        return out

    # -- snapshot fallback ---------------------------------------------------
    def _snapshot_fallback(self):
        path = self.snapshot_fallback
        if path and not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)), path)
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    # Report the fallback's own date, not a stale house-view meta.
                    self.as_of = data.get("as_of")
                    return list(data.get("releases", []))
                return list(data)
            except Exception:
                return []
        return []


if __name__ == "__main__":
    import yaml  # type: ignore
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    prov = BlpapiProvider(cfg)
    rows = prov.get_releases()
    print(f"{len(rows)} releases; fetch_error={prov.fetch_error}")
    for r in rows[:12]:
        print(f"  {r.get('date_iso')}  {str(r.get('event'))[:26]:<26} "
              f"a={r.get('actual')} c={r.get('consensus')} p={r.get('previous')} "
              f"[{r.get('status')}]")
