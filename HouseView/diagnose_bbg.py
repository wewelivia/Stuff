"""
diagnose_bbg.py - show exactly what Bloomberg returns for each configured ticker.

WHY
---
When every release shows "Awaiting print" in blpapi mode, the cause is almost always
one of: the Terminal is not running, the ticker is wrong, or the field mnemonic does
not exist for that ticker family. The dashboard cannot tell you which. This can.

RUN
---
    python diagnose_bbg.py

It prints, per ticker, whether the security resolved and what each field returned,
then a summary of which fields came back empty across the board (a field that is
blank for EVERY ticker is a wrong mnemonic; a field blank for ONE ticker is usually
a wrong ticker).
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
    print("Install it from Bloomberg's own index (it is not on normal PyPI):")
    print("  pip install --index-url "
          "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi")
    sys.exit(1)


def load_config():
    with open(os.path.join(BASE, "config.yaml"), "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main():
    cfg = load_config()
    blp = cfg.get("blpapi", {}) or {}
    fields = blp.get("fields", {}) or {}
    ticker_key = blp.get("ticker_key", "ticker")
    releases = cfg.get("releases", []) or []

    field_list = [
        fields.get("consensus", "BN_SURVEY_MEDIAN"),
        fields.get("actual", "ACTUAL_RELEASE"),
        fields.get("previous", "PREVIOUS_VALUE"),
        fields.get("period_end", "ECO_RELEASE_PERIOD_END_DATE"),
        fields.get("release_date_list", "ECO_FUTURE_RELEASE_DATE_LIST"),
    ]

    host = blp.get("host", "localhost")
    port = int(blp.get("port", 8194))

    print(f"Connecting to Bloomberg at {host}:{port} ...")
    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        print("\nFAILED: could not start a blpapi session.")
        print("Check the Terminal is running and you are logged in on this machine.")
        return
    if not session.openService("//blp/refdata"):
        print("\nFAILED: could not open //blp/refdata.")
        return
    print("Connected.\n")

    svc = session.getService("//blp/refdata")
    today = dt.date.today()
    start = (today - dt.timedelta(days=int(blp.get("window_back_days", 120)))).strftime("%Y%m%d")
    end = (today + dt.timedelta(days=int(blp.get("window_fwd_days", 45)))).strftime("%Y%m%d")

    empty_counts = {f: 0 for f in field_list}
    bad_tickers = []
    n = 0

    for entry in releases:
        ticker = entry.get(ticker_key) or entry.get("bbg_ticker")
        if not ticker:
            continue
        n += 1
        req = svc.createRequest("ReferenceDataRequest")
        req.append("securities", ticker)
        for f in field_list:
            req.append("fields", f)
        for name, val in (("START_DT", start), ("END_DT", end)):
            ov = req.getElement("overrides").appendElement()
            ov.setElement("fieldId", name)
            ov.setElement("value", val)

        session.sendRequest(req)
        got = {}
        err = None
        while True:
            ev = session.nextEvent(5000)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                secs = msg.getElement("securityData")
                for i in range(secs.numValues()):
                    sd = secs.getValueAsElement(i)
                    if sd.hasElement("securityError"):
                        err = str(sd.getElement("securityError").getElementAsString("message"))
                        continue
                    if sd.hasElement("fieldExceptions"):
                        fx = sd.getElement("fieldExceptions")
                        for k in range(fx.numValues()):
                            fe = fx.getValueAsElement(k)
                            fid = fe.getElementAsString("fieldId")
                            got[fid] = "<FIELD NOT VALID>"
                    fd = sd.getElement("fieldData")
                    for j in range(fd.numElements()):
                        el = fd.getElement(j)
                        name = str(el.name())
                        if el.isArray():
                            got[name] = f"<{el.numValues()} values>"
                        else:
                            got[name] = el.getValue()
            if ev.eventType() == blpapi.Event.RESPONSE:
                break

        label = f"{entry.get('event','?')}  [{ticker}]"
        if err:
            print(f"  {label}\n      SECURITY ERROR: {err}")
            bad_tickers.append(ticker)
            continue
        print(f"  {label}")
        for f in field_list:
            v = got.get(f, None)
            mark = "ok " if v not in (None, "", "<FIELD NOT VALID>") else "-- "
            if v in (None, ""):
                empty_counts[f] += 1
                v = "<empty>"
            print(f"      {mark}{f:<34} {v}")
        print()

    session.stop()

    print("=" * 64)
    print(f"Checked {n} tickers.\n")
    if bad_tickers:
        print("Tickers Bloomberg did not recognise (fix these in config.yaml):")
        for t in bad_tickers:
            print(f"  - {t}")
        print()
    for f, c in empty_counts.items():
        if c == n and n:
            print(f"  {f}: empty for EVERY ticker -> the mnemonic is probably wrong. "
                  f"Check FLDS<GO> on one of these securities.")
        elif c:
            print(f"  {f}: empty for {c} of {n} tickers -> likely those tickers, not the field.")
    if not bad_tickers and not any(empty_counts.values()):
        print("Everything resolved. If the dashboard still looks wrong, the issue is "
              "downstream of Bloomberg.")


if __name__ == "__main__":
    main()
