"""
Market Monitor engine — House View dashboard extension.

Fetches free daily market data (FRED + Stooq), then computes:
  * standardised 1d / 5d moves (z-scores vs a trailing lookback)
  * outsized-move flags
  * rolling cross-asset correlation shifts
  * a growth/inflation regime quadrant (goldilocks / reflation /
    stagflation / disinflation)

No API keys required. Any series that fails to fetch is reported in
`errors` and excluded — never fatal. Follows the House View convention:
this module is a provider-style layer; the API and HTML never need to
change when the universe does.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_FILE = os.path.join(BASE_DIR, "market_universe.yaml")

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sym}&cosd={start}"
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}&i=d&d1={d1}&d2={d2}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_MEM_CACHE: dict = {"payload": None, "ts": 0.0}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_universe(path: str = UNIVERSE_FILE) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is required (already in House View requirements.txt)")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_two_col_csv(text: str) -> list[tuple[str, float]]:
    """FRED fredgraph.csv: date in col 0, value in col 1 ('.' = missing)."""
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        d, v = row[0].strip(), row[1].strip()
        if not d or d[0].isalpha():          # header row
            continue
        try:
            out.append((d, float(v)))
        except ValueError:
            continue                          # '.' missing values
    return out


def _parse_stooq_csv(text: str) -> list[tuple[str, float]]:
    """Stooq daily CSV: Date,Open,High,Low,Close,(Volume). Uses Close."""
    out = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or "Date" not in header[0]:
        return []
    try:
        close_idx = [h.strip().lower() for h in header].index("close")
    except ValueError:
        close_idx = 4
    for row in reader:
        if len(row) <= close_idx:
            continue
        try:
            out.append((row[0].strip(), float(row[close_idx])))
        except ValueError:
            continue
    return out


def fetch_series(source: str, symbol: str, start: date) -> list[tuple[str, float]]:
    if source == "fred":
        text = _http_get(FRED_URL.format(sym=symbol, start=start.isoformat()))
        return _parse_two_col_csv(text)
    if source == "stooq":
        text = _http_get(STOOQ_URL.format(
            sym=symbol.lower(),
            d1=start.strftime("%Y%m%d"),
            d2=date.today().strftime("%Y%m%d")))
        return _parse_stooq_csv(text)
    raise ValueError(f"unknown source: {source}")


# --------------------------------------------------------------------------
# Maths (pure python — no pandas dependency)
# --------------------------------------------------------------------------

def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _zscore(latest: float, history: list[float]) -> float | None:
    s = _std(history)
    if s == 0:
        return None
    return round((latest - _mean(history)) / s, 2)


def _percentile_of(value: float, xs: list[float]) -> float | None:
    if not xs:
        return None
    below = sum(1 for x in xs if x < value)
    return round(100.0 * below / len(xs), 1)


def _changes(values: list[float], step: int, kind: str) -> list[float]:
    """Sequence of `step`-period changes. bp for yields/oas, % for prices."""
    out = []
    for i in range(step, len(values)):
        prev, cur = values[i - step], values[i]
        if kind in ("yield", "oas"):
            out.append((cur - prev) * 100.0)          # percent pts -> bp
        elif kind == "level":
            out.append(cur - prev)                     # points
        else:
            if prev != 0:
                out.append(100.0 * (cur - prev) / prev)  # %
    return out


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    n = min(len(xs), len(ys))
    if n < 20:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = _mean(xs), _mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    if den == 0:
        return None
    return round(num / den, 2)


# --------------------------------------------------------------------------
# Per-series stats
# --------------------------------------------------------------------------

def analyse_series(cfg: dict, data: list[tuple[str, float]], settings: dict) -> dict | None:
    if len(data) < 30:
        return None
    data = sorted(data)
    dates = [d for d, _ in data]
    values = [v for _, v in data]
    kind = cfg.get("kind", "price")
    lb = settings.get("zscore_lookback", 252)

    chg1 = _changes(values, 1, kind)
    chg5 = _changes(values, 5, kind)
    last, last_date = values[-1], dates[-1]

    # YTD change
    year_start = f"{last_date[:4]}-01-01"
    base = next((v for d, v in data if d >= year_start), values[0])
    if kind in ("yield", "oas"):
        chg_ytd = round((last - base) * 100.0, 1)
    elif kind == "level":
        chg_ytd = round(last - base, 2)
    else:
        chg_ytd = round(100.0 * (last - base) / base, 2) if base else None

    z1 = _zscore(chg1[-1], chg1[-lb:]) if chg1 else None
    z5 = _zscore(chg5[-1], chg5[-lb:]) if chg5 else None

    display_last = round(last * 100.0, 0) if kind == "oas" else round(last, 3)
    c1 = round(chg1[-1], 2) if chg1 else None
    c5 = round(chg5[-1], 2) if chg5 else None

    # staleness (calendar-day approximation of business days)
    try:
        age = (date.today() - datetime.strptime(last_date, "%Y-%m-%d").date()).days
    except ValueError:
        age = 0
    stale = age > settings.get("stale_after_bdays", 5) + 2

    spark = values[-60:]

    return {
        "name": cfg["name"],
        "asset_class": cfg.get("asset_class", "other"),
        "region": cfg.get("region", "GL"),
        "kind": kind,
        "unit": cfg.get("unit", ""),
        "last": display_last,
        "date": last_date,
        "chg_1d": c1,
        "chg_5d": c5,
        "chg_ytd": chg_ytd,
        "z_1d": z1,
        "z_5d": z5,
        "pctile_3y": _percentile_of(last, values),
        "stale": stale,
        "spark": [round(v, 4) for v in spark],
        "invert_for_risk": bool(cfg.get("invert_for_risk", False)),
        "_dates": dates,       # stripped before serialisation
        "_values": values,
    }


# --------------------------------------------------------------------------
# Cross-asset correlation shifts
# --------------------------------------------------------------------------

def correlation_shifts(pairs: list[dict], by_name: dict, settings: dict) -> list[dict]:
    win = settings.get("trend_window", 63)
    flag_at = settings.get("corr_shift_flag", 0.40)
    out = []
    for pair in pairs:
        a, b = by_name.get(pair["a"]), by_name.get(pair["b"])
        if not a or not b:
            continue
        common = sorted(set(a["_dates"]) & set(b["_dates"]))
        if len(common) < 2 * win + 10:
            continue
        av = {d: v for d, v in zip(a["_dates"], a["_values"])}
        bv = {d: v for d, v in zip(b["_dates"], b["_values"])}
        xs = [av[d] for d in common]
        ys = [bv[d] for d in common]
        dx = _changes(xs, 1, a["kind"])
        dy = _changes(ys, 1, b["kind"])
        cur = _correlation(dx[-win:], dy[-win:])
        prev = _correlation(dx[-2 * win:-win], dy[-2 * win:-win])
        if cur is None or prev is None:
            continue
        shift = round(cur - prev, 2)
        sign_flip = (cur * prev < 0) and (abs(cur) > 0.15 or abs(prev) > 0.15)
        out.append({
            "label": pair.get("label", f'{pair["a"]} vs {pair["b"]}'),
            "current": cur,
            "previous": prev,
            "shift": shift,
            "flag": abs(shift) >= flag_at or sign_flip,
            "sign_flip": sign_flip,
        })
    return out


# --------------------------------------------------------------------------
# Regime quadrant
# --------------------------------------------------------------------------

def _trend_z(stats: dict, win: int) -> float | None:
    """z-score of the latest `win`-period change vs its own 3y history."""
    ch = _changes(stats["_values"], win, stats["kind"])
    if len(ch) < 60:
        return None
    return _zscore(ch[-1], ch)


def regime_quadrant(regime_cfg: dict, by_name: dict, settings: dict) -> dict:
    win = settings.get("trend_window", 63)
    threshold = regime_cfg.get("threshold", 0.25)

    def composite(members):
        parts, detail = [], []
        for m in members:
            s = by_name.get(m["name"])
            if not s:
                continue
            z = _trend_z(s, win)
            if z is None:
                continue
            w = m.get("weight", 1.0)
            parts.append(z * w)
            detail.append({"name": m["name"], "z_3m": z, "weight": w})
        if not parts:
            return None, detail
        return round(sum(parts) / sum(abs(m["weight"]) for m in detail), 2), detail

    g, g_detail = composite(regime_cfg.get("growth", []))
    i, i_detail = composite(regime_cfg.get("inflation", []))

    quadrant, description = "indeterminate", "Insufficient data to classify the regime."
    if g is not None and i is not None:
        gs = "up" if g > threshold else "down" if g < -threshold else "flat"
        infl = "up" if i > threshold else "down" if i < -threshold else "flat"
        mapping = {
            ("up", "down"): ("goldilocks", "Growth impulse improving while inflation pressure eases — supportive for both bonds and equities."),
            ("up", "up"): ("reflation", "Growth and inflation impulses both rising — pro-cyclical assets favoured, duration challenged."),
            ("down", "up"): ("stagflation", "Growth impulse fading while inflation pressure builds — the most hostile mix for traditional 60/40."),
            ("down", "down"): ("disinflationary slowdown", "Growth and inflation impulses both fading — duration supported, cyclicals challenged."),
        }
        if (gs, infl) in mapping:
            quadrant, description = mapping[(gs, infl)]
        else:
            quadrant = "transitioning"
            description = "Composite signals are near neutral — no decisive regime; watch for confirmation."

    return {
        "quadrant": quadrant,
        "description": description,
        "growth_score": g,
        "inflation_score": i,
        "threshold": threshold,
        "window_days": win,
        "growth_components": g_detail,
        "inflation_components": i_detail,
    }


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def build_monitor(universe_path: str = UNIVERSE_FILE, use_cache: bool = True) -> dict:
    uni = load_universe(universe_path)
    settings = uni.get("settings", {})
    ttl = settings.get("cache_ttl_minutes", 15) * 60
    cache_file = os.path.join(BASE_DIR, settings.get("cache_file", "market_cache.json"))

    if use_cache and _MEM_CACHE["payload"] and time.time() - _MEM_CACHE["ts"] < ttl:
        return _MEM_CACHE["payload"]
    if use_cache and os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < ttl:
            try:
                with open(cache_file, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                _MEM_CACHE.update(payload=payload, ts=time.time())
                return payload
            except (json.JSONDecodeError, OSError):
                pass

    t0 = time.time()
    start = date.today() - timedelta(days=settings.get("history_start_days", 1300))
    stats_list, errors = [], []

    for cfg in uni.get("series", []):
        data = []
        for attempt in ([{"source": cfg["source"], "symbol": cfg["symbol"]}]
                        + ([cfg["fallback"]] if cfg.get("fallback") else [])):
            try:
                data = fetch_series(attempt["source"], str(attempt["symbol"]), start)
                if data:
                    break
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{cfg["name"]} ({attempt["source"]}:{attempt["symbol"]}): {exc}')
        if not data:
            if not any(cfg["name"] in e for e in errors):
                errors.append(f'{cfg["name"]}: no data returned')
            continue
        s = analyse_series(cfg, data, settings)
        if s:
            stats_list.append(s)
        else:
            errors.append(f'{cfg["name"]}: insufficient history')

    by_name = {s["name"]: s for s in stats_list}
    corr = correlation_shifts(uni.get("correlation_pairs", []), by_name, settings)
    regime = regime_quadrant(uni.get("regime", {}), by_name, settings)

    outsized_z = settings.get("outsized_z", 2.0)
    movers = sorted(
        (s for s in stats_list if s["z_1d"] is not None and abs(s["z_1d"]) >= outsized_z),
        key=lambda s: -abs(s["z_1d"]))

    def public(s):
        return {k: v for k, v in s.items() if not k.startswith("_")}

    payload = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "generated_in_s": round(time.time() - t0, 1),
        "outsized_z": outsized_z,
        "regime": regime,
        "correlations": corr,
        "movers": [public(s) for s in movers],
        "series": [public(s) for s in stats_list],
        "errors": errors,
    }

    _MEM_CACHE.update(payload=payload, ts=time.time())
    try:
        with open(cache_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass
    return payload


if __name__ == "__main__":
    result = build_monitor(use_cache=False)
    print(json.dumps({k: v for k, v in result.items() if k != "series"}, indent=2)[:4000])
    print(f'\n{len(result["series"])} series analysed, {len(result["errors"])} errors')
