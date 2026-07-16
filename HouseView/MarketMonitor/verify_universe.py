"""
Universe validation gate — run once on your machine before launching
(the market-monitor equivalent of diagnose_bbg.py).

    python verify_universe.py            # uses settings.data_source (bbg by default)
    python verify_universe.py web        # force web mode (FRED/Stooq)
    python verify_universe.py bbg        # force Bloomberg mode

Fetches every series in market_universe.yaml and reports OK / FAIL with
row counts and the latest observation. Failures are skipped gracefully
by the engine, but this tells you which tickers need correcting.
"""

import sys
from datetime import date, timedelta

from market_monitor import (configure_proxy, fetch_bbg_batch, fetch_series,
                            load_universe)


def verify_bbg(uni, start) -> tuple[int, int]:
    settings = uni.get("settings", {})
    cfgs = [c for c in uni.get("series", []) if c.get("bbg")]
    tickers = [c["bbg"] for c in cfgs]
    print(f"Requesting {len(tickers)} tickers from Bloomberg "
          f'({settings.get("bbg_host", "localhost")}:{settings.get("bbg_port", 8194)})…\n')
    data, errors = fetch_bbg_batch(tickers, start,
                                   host=settings.get("bbg_host", "localhost"),
                                   port=settings.get("bbg_port", 8194))
    ok = fail = 0
    print(f'{"SERIES":<22} {"BBG TICKER":<18} RESULT')
    print("-" * 66)
    for cfg in cfgs:
        rows = data.get(cfg["bbg"], [])
        if rows:
            ok += 1
            d, v = rows[-1]
            print(f'{cfg["name"]:<22} {cfg["bbg"]:<18} OK   {len(rows):>4} rows, last {d} = {v}')
        else:
            fail += 1
            err = next((e for e in errors if cfg["bbg"] in e), "no data")
            print(f'{cfg["name"]:<22} {cfg["bbg"]:<18} FAIL {err}')
    return ok, fail


def verify_web(uni, start) -> tuple[int, int]:
    configure_proxy(uni.get("settings", {}).get("proxy"))
    ok = fail = 0
    print(f'{"SERIES":<22} {"SOURCE":<7} {"SYMBOL":<18} RESULT')
    print("-" * 70)
    for cfg in uni.get("series", []):
        if not cfg.get("source"):
            print(f'{cfg["name"]:<22} {"—":<7} {"—":<18} SKIP (bbg-only)')
            continue
        attempts = [{"source": cfg["source"], "symbol": cfg["symbol"]}]
        if cfg.get("fallback"):
            attempts.append(cfg["fallback"])
        result, used = None, attempts[0]
        for attempt in attempts:
            try:
                data = fetch_series(attempt["source"], str(attempt["symbol"]), start)
                if data:
                    result, used = data, attempt
                    break
            except Exception:  # noqa: BLE001
                result = None
        if result:
            ok += 1
            d, v = result[-1]
            note = " (fallback)" if used is not attempts[0] else ""
            print(f'{cfg["name"]:<22} {used["source"]:<7} {str(used["symbol"]):<18} '
                  f'OK   {len(result):>4} rows, last {d} = {v}{note}')
        else:
            fail += 1
            print(f'{cfg["name"]:<22} {cfg["source"]:<7} {str(cfg["symbol"]):<18} FAIL')
    return ok, fail


def main() -> None:
    uni = load_universe()
    mode = (sys.argv[1] if len(sys.argv) > 1
            else uni.get("settings", {}).get("data_source", "bbg"))
    start = date.today() - timedelta(days=90)
    print(f"Mode: {mode}\n")
    try:
        ok, fail = verify_bbg(uni, start) if mode == "bbg" else verify_web(uni, start)
    except Exception as exc:  # noqa: BLE001
        print(f"\nFATAL: {exc}")
        if mode == "bbg":
            print("Check the Terminal is running and blpapi is installed "
                  "(same setup validated by diagnose_bbg.py).")
        else:
            print("If every series fails on the corporate network, the proxy is "
                  "blocking outbound Python HTTPS — use bbg mode, or set "
                  "settings.proxy in market_universe.yaml.")
        raise SystemExit(1)
    print("-" * 66)
    print(f"{ok} OK, {fail} failed.")
    if fail and mode == "bbg":
        print("Correct failed tickers in market_universe.yaml (check on the Terminal "
              "with <ticker> DES). The engine skips failures gracefully.")
    elif fail:
        print("Failed web symbols: search the instrument on stooq.com / fred.stlouisfed.org "
              "and update market_universe.yaml. If ALL failed, it is the corporate proxy — "
              "use bbg mode.")


if __name__ == "__main__":
    main()
