# House View - View Challenges

A local macro dashboard that scores incoming economic releases against an editable
house view (currently the Barclays Private Bank Mid-Year Outlook 2026, "Power
Struggle") and marks each print as confirming, challenging or in line, per theme.

This is a complete, runnable project. It starts in snapshot mode with real data and
needs no Bloomberg to prove it works. Flip one line to run live off the Terminal.

## Run it (Mac or work PC)

```
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```
Then open http://127.0.0.1:8000/

Or use the launcher: `start.command` on Mac, `start.bat` on Windows.

## The two run modes

The provider abstraction means the same code runs offline or live off Bloomberg with
a one-line change. Every provider serves the identical `get_releases()` contract, so
the engine and the HTML never change.

- **Mac / offline: `provider_mode: snapshot`** (the default).
  Serves a point-in-time `releases_snapshot.json`, refreshed on demand (ask Claude or
  Copilot; see REFRESH.md). No Terminal, no API key, no network.

- **Work PC (Bloomberg running): `provider_mode: blpapi`.**
  Pulls releases, consensus, actual, previous and publish dates live off the Terminal.
  If the Terminal is closed it falls back to the snapshot rather than blanking.

Switch by editing the `provider_mode` line in `config.yaml`, or without editing by
setting the environment variable `HV_PROVIDER_MODE=blpapi`.

## Going live on Bloomberg (one-time on the work PC)

1. Install blpapi (it is NOT a normal PyPI package):
   ```
   pip install --index-url https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
   ```
2. In `config.yaml`, set `provider_mode: blpapi`.
3. Confirm the tickers and field mnemonics in `config.yaml` against your own
   tickers with `SECF<GO>` / `FLDS<GO>`. They ship as best-guess placeholders marked
   `# VERIFY`; a wrong ticker or field is the usual reason a print comes back blank.
4. Smoke-test the provider on its own before launching the app:
   ```
   python -m providers.blpapi_provider
   ```

## How the scoring works

For each released print with a consensus: `surprise = actual - consensus`. The
`higher_is` field (hawkish / good / bad) says what a higher number means, and each
theme's rule (in `challenge_engine.py`) says which direction confirms the house view.
This cycle the house view is sticky inflation plus hold-to-hawkish rates plus a
softening real economy, so a hotter inflation or rates print confirms, while a
stronger growth, consumption or sentiment print challenges. Labour is left unscored
while its house-view bias is `TO_CONFIRM`.

## Layout

```
HouseView/
  app.py                      FastAPI: serves the API and both HTML pages
  challenge_engine.py         scoring logic
  config.yaml                 provider_mode + release universe + Bloomberg wiring
  house_view.yaml             editable house view ("Power Struggle")
  view-challenges.html        main dashboard (scored releases + theme rail)
  house-view-dashboard.html   the house view overview
  requirements.txt
  start.command / start.bat   launchers
  README.md / REFRESH.md / SETUP-GIT.md / copilot_refresh_prompt.md
  providers/
    mock_provider.py          base class (the contract)
    snapshot_provider.py      offline snapshot
    blpapi_provider.py        Bloomberg (live) with publish-date remap
    releases_snapshot.json    current data + offline fallback
```

## Open items

- `labour` in `house_view.yaml` is `TO_CONFIRM`, so labour prints show as Unscored.
  Latest jobs data points to "resilient but cooling"; set the bias to start scoring it.
- `sentiment` has no releases in the current snapshot; add ISM / Ifo on the next
  refresh to light it up.
- The Bloomberg tickers and fields in `config.yaml` are placeholders. Verify before
  a live run.

See SETUP-GIT.md to push this to GitHub and pull it on the work PC.
