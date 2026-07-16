# Market Monitor + Vol-Adjusted Dashboard — merged

The standalone Vol-Adjusted Market Dashboard (HouseView, port 8030) and the
Market Monitor are now one dashboard. Kept from the Market Monitor: the look,
the macro regime quadrant and the correlation regime panel, both byte-for-byte
unchanged in logic. Replaced: the move standardisation. The old "z of the
latest 1d/5d change vs a 1y distribution of changes" gave way to the
vol-adjusted methodology you preferred:

    z = (h-day move) / (realised daily vol x sqrt(h))

with realised vol over a 63-trading-day window ending before the move starts,
so a large move cannot inflate its own yardstick. The table now shows 1d, 1w,
1m and 3m vol-adjusted moves per series (z in a heat cell, the raw move
beneath); horizons are dynamic via `settings.horizons` in market_universe.yaml.
The outsized-movers cards run on the vol-adjusted 1d score, same 2σ threshold.

## Changed files (drop over the existing MarketMonitor folder)

    market_monitor.py       vol_adjusted_scores() added; analyse_series now
                            emits `horizons`; z_1d/z_5d kept for API
                            compatibility but vol-adjusted since the merge.
                            Fetching, caching, regime, correlations untouched.
    market-monitor.html     horizon columns replace the 1d/z/5d/z columns;
                            movers heading updated; methodology line added.
                            Everything else identical.
    market_universe.yaml    settings gain `vol_window: 63` and `horizons:`;
                            universe, regime and correlation pairs unchanged.
    test_vol_adjusted.py    new offline tests (all passing), including a
                            synthetic pass through regime and correlations to
                            prove the retained logic is undisturbed.

market_monitor_api.py, verify_universe.py and the run modes are unchanged.
Delete market_cache.json (or hit ↻ Refresh once): the cached payload predates
the new fields.

## MonitorHub (two files, also updated)

The vol-adjusted-markets entry (port 8030) is removed from monitors.yaml and
hub.html — the merged monitor supersedes it. The Market Monitor card
description now reflects the merge.

## Now redundant in HouseView (optional cleanup, archive not delete)

    market-dashboard.html, market_engine.py, providers/market_provider.py,
    providers/market_snapshot.json, the market: block in config.yaml and the
    /api/market-dashboard routes in app.py

They keep working if left in place; nothing references them after the hub
update. Per project hygiene, add them to CLEANUP.md as archive candidates.
