"""
diagnose_bbg.py - find out which Bloomberg fields actually work on YOUR tickers.

WHY THIS VERSION
----------------
Run 1 proved the tickers are fine, and that ACTUAL_RELEASE / PREVIOUS_VALUE /
ECO_RELEASE_PERIOD_END_DATE do not exist as snapshot fields on economic-release
securities. That matches the Phase 1 finding: the actual is not a reference field,
it lives in HISTORY keyed to the period-end date.

So this script now does two things:

  PART 1  Probe CANDIDATE reference fields per role and report which resolve.
          No guessing: the Terminal tells us the answer.

  PART 2  Test the HISTORICAL path (HistoricalDataRequest on PX_LAST and on
          BN_SURVEY_MEDIAN). This is the proposed source of actuals, previous
          and period-end. Part 2 is the one that matters.

RUN
---
    python diagnose_bbg.py

Paste the output back and the provider gets wired to whatever actually works.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))

try:
    import blpapi
except ImportError:
    print("blpapi is not installed in this Python environment.\n")
    print("  pip install --index-url "
          "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi")
    sys.exit(1)


# Candidate reference fields to probe, grouped by the role we need filled.
# BN_SURVEY_MEDIAN and ECO_FUTURE_RELEASE_DATE_LIST are known-good from run 1
# and act as controls: if they fail here, something else is wrong.
CANDIDATES = {
    "actual (reference)": [
        "PX_LAST",
        "LAST_PRICE",
        "BN_SURVEY_ACTUAL",
        "ECO_RELEASE_ACTUAL",
    ],
    "previous (reference)": [
        "PX_YEST_CLOSE",
        "BN_SURVEY_PREVIOUS",
        "ECO_RELEASE_PRIOR",
    ],
    "period end / release date": [
        "ECO_RELEASE_DT",
        "LATEST_ANNOUNCEMENT_DT",
        "LAST_UPDATE_DT",
    ],
    "controls (known good)": [
        "BN_SURVEY_MEDIAN",
        "ECO_FUTURE_RELEASE_DATE_LIST",
    ],
}

# Probing every ticker with ~13 fields is slow and noisy. Three representative
# securities prove the point.
PROBE_LIMIT = 3


def load_config():
    with open(os.path.join(BASE, "config.yaml"), "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def open_session(host, port):
    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(port)
    s = blpapi.Session(opts)
    if not s.start():
        print("FAILED: could not start a blpapi session. Is the Terminal running?")
        return None
    if not s.openService("//blp/refdata"):
        print("FAILED: could not open //blp/refdata.")
        return None
    return s


def drain(session):
    """Collect all messages until RESPONSE."""
    msgs = []
    while True:
        ev = session.nextEvent(10000)
        for m in ev:
            msgs.append(m)
        if ev.eventType() == blpapi.Event.RESPONSE:
            break
    return msgs


def probe_reference(session, svc, tickers):
    print("=" * 70)
    print("PART 1 - which REFERENCE fields resolve?")
    print("=" * 70)

    all_fields = [f for group in CANDIDATES.values() for f in group]

    for ticker in tickers:
        print(f"\n  {ticker}")
        req = svc.createRequest("ReferenceDataRequest")
        req.append("securities", ticker)
        for f in all_fields:
            req.append("fields", f)

        session.sendRequest(req)
        valid, invalid = {}, set()
        for msg in drain(session):
            if not msg.hasElement("securityData"):
                continue
            secs = msg.getElement("securityData")
            for i in range(secs.numValues()):
                sd = secs.getValueAsElement(i)
                if sd.hasElement("securityError"):
                    print("      SECURITY ERROR")
                    continue
                if sd.hasElement("fieldExceptions"):
                    fx = sd.getElement("fieldExceptions")
                    for k in range(fx.numValues()):
                        invalid.add(fx.getValueAsElement(k).getElementAsString("fieldId"))
                fd = sd.getElement("fieldData")
                for j in range(fd.numElements()):
                    el = fd.getElement(j)
                    valid[str(el.name())] = (f"<{el.numValues()} values>"
                                             if el.isArray() else el.getValue())

        for role, fields in CANDIDATES.items():
            print(f"      {role}:")
            for f in fields:
                if f in valid:
                    print(f"          OK      {f:<32} {valid[f]}")
                elif f in invalid:
                    print(f"          invalid {f}")
                else:
                    print(f"          empty   {f}")


def probe_history(session, svc, tickers, back_days):
    print("\n" + "=" * 70)
    print("PART 2 - the HISTORICAL path (this is the one that matters)")
    print("=" * 70)
    print("If PX_LAST history returns rows, that is our source of actuals,")
    print("previous and period-end dates, and 3 broken fields become unnecessary.")
    print("If BN_SURVEY_MEDIAN history returns rows, we can score PAST surprises.")
    print("If it does not, only the NEXT release can be scored against consensus.\n")

    today = dt.date.today()
    start = (today - dt.timedelta(days=back_days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    for ticker in tickers:
        print(f"  {ticker}   ({start} .. {end})")
        for field in ("PX_LAST", "BN_SURVEY_MEDIAN"):
            req = svc.createRequest("HistoricalDataRequest")
            req.append("securities", ticker)
            req.append("fields", field)
            req.set("startDate", start)
            req.set("endDate", end)
            req.set("periodicitySelection", "DAILY")

            session.sendRequest(req)
            rows, err = [], None
            for msg in drain(session):
                if not msg.hasElement("securityData"):
                    continue
                sd = msg.getElement("securityData")
                if sd.hasElement("securityError"):
                    err = "security error"
                    continue
                if sd.hasElement("fieldExceptions") and sd.getElement("fieldExceptions").numValues():
                    err = "FIELD NOT VALID for history"
                    continue
                fdarr = sd.getElement("fieldData")
                for i in range(fdarr.numValues()):
                    row = fdarr.getValueAsElement(i)
                    d = row.getElementAsDatetime("date") if row.hasElement("date") else None
                    v = row.getElement(field).getValue() if row.hasElement(field) else None
                    rows.append((str(d)[:10], v))

            if err:
                print(f"      {field:<20} {err}")
            elif not rows:
                print(f"      {field:<20} no rows returned")
            else:
                print(f"      {field:<20} {len(rows)} rows   e.g. " +
                      ", ".join(f"{d}={v}" for d, v in rows[-3:]))
        print()


def main():
    cfg = load_config()
    blp = cfg.get("blpapi", {}) or {}
    ticker_key = blp.get("ticker_key", "ticker")
    releases = cfg.get("releases", []) or []
    back_days = int(blp.get("window_back_days", 120))

    tickers = []
    for e in releases:
        t = e.get(ticker_key) or e.get("bbg_ticker")
        if t and t not in tickers:
            tickers.append(t)
    tickers = tickers[:PROBE_LIMIT]

    if not tickers:
        print("No tickers found in config.yaml.")
        return

    host, port = blp.get("host", "localhost"), int(blp.get("port", 8194))
    print(f"Connecting to Bloomberg at {host}:{port} ...")
    session = open_session(host, port)
    if session is None:
        return
    print(f"Connected. Probing {len(tickers)} representative tickers.\n")
    svc = session.getService("//blp/refdata")

    try:
        probe_reference(session, svc, tickers)
        probe_history(session, svc, tickers, back_days)
    finally:
        session.stop()

    print("=" * 70)
    print("Paste this output back and the provider gets wired to what works.")
    print("=" * 70)


if __name__ == "__main__":
    main()
