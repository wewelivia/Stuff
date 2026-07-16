"""
Universe validation gate — run once on your machine before launching
(the market-monitor equivalent of diagnose_bbg.py).

    python verify_universe.py

Fetches every series in market_universe.yaml and reports OK / FAIL with
row counts and the latest observation. Failures are skipped gracefully
by the engine, but this tells you which symbols need correcting
(the Stooq ones marked # VERIFY are the likely candidates).
"""

from datetime import date, timedelta

from market_monitor import fetch_series, load_universe


def main() -> None:
    uni = load_universe()
    start = date.today() - timedelta(days=90)
    ok, fail = 0, 0
    print(f'{"SERIES":<22} {"SOURCE":<7} {"SYMBOL":<18} RESULT')
    print("-" * 70)
    for cfg in uni.get("series", []):
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
            except Exception as exc:  # noqa: BLE001
                result_err = str(exc)[:40]
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
    print("-" * 70)
    print(f"{ok} OK, {fail} failed.")
    if fail:
        print("Failed series will be skipped by the engine. To fix a Stooq symbol,")
        print("search the instrument on stooq.com and copy the symbol from the URL.")


if __name__ == "__main__":
    main()
