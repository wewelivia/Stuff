# MonitorHub — run all three dashboards at once

Replaces the stop-uvicorn / change-directory / relaunch cycle with one command. Each backend keeps its own working directory and port, so the three projects never interfere (no shared module names, no config collisions).

## Layout

Place the MonitorHub folder next to your project folders:

```
Dashboards/
├── HouseView/          (view challenges — port 8000)
├── MarketMonitor/      (macro regime — port 8010)
├── OptionImplied/      (option implied — port 8020)
└── MonitorHub/         (this folder)
```

Edit `monitors.yaml` once: set each `dir` to the real folder name and each `app` to the module (uvicorn notation, e.g. `app:app`). For the option-implied monitor, use whatever you currently type after `uvicorn` when launching it.

## Run

```
python run_all.py            # everything
python run_all.py market-monitor view-challenges   # subset by name
```

Or double-click `start_all.bat` on Windows. One console, colour-prefixed logs per backend, Ctrl+C stops all. Ports already in use or missing directories are skipped with a warning rather than aborting the rest. If one backend crashes, the others keep running.

## Hub page

Open `hub.html` for a landing page with one card per dashboard: live status dot (pings each port every 5s), and an Open link. Set the backend host once at the top (127.0.0.1 locally, or the Windows machine's IP from your Mac). Dashboard link paths and ports are set in the `MONITORS` list at the top of the file's script — adjust alongside monitors.yaml.

## One thing to check on your other dashboards

Browsers treat all `file://` pages as one localStorage origin, so dashboards sharing the storage key `hv-api-base` would overwrite each other's endpoint now that each runs on a different port. `market-monitor.html` has been moved to its own key (`hv-mm-api-base`, default port 8010). View Challenges can keep `hv-api-base` (port 8000, unchanged from today). If the option-implied dashboard also uses `hv-api-base`, rename its key the same way — it is a one-line change in its HTML.

## Why not one merged app on a single port?

Considered and rejected: your projects each define modules named `app` (and potentially `providers`), so importing them into one process would collide, and each loads `config.yaml` relative to its own directory. Separate processes with a supervisor is the boring, robust answer. If you ever want a single port anyway (e.g. for firewall reasons), the cleanest route would be renaming modules per project and mounting sub-apps under path prefixes — happy to do that as a follow-up if it matters.
