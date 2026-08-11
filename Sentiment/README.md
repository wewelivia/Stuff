# Sentiment Indicator

A sentiment tab for the House View dashboard, based on the HSBC multi-asset
sentiment indicator (Global Research, *Sentiment shape up*, 5 March 2024).

Twenty-one inputs, twenty carrying a sell rule and thirteen a buy rule, each
firing when it passes a percentile threshold within its own history. Two
aggregations run in parallel off identical inputs so they stay comparable: a
faithful binary count, and a cluster-weighted continuous version.

## Layout

```
Sentiment/
├── sentiment_engine.py        transforms, percentile triggers, aggregation
├── sentiment_stats.py         calibration, lift over base rate, HAC tests
├── sentiment_builder.py       config to engine inputs
├── api_sentiment.py           FastAPI routes
├── sentiment.html             the tab
├── config/
│   ├── sentiment_config.yaml  cache path, benchmark, horizons
│   └── sentiment_tickers.yaml the 21 inputs and their series codes
├── providers/
│   ├── base.py                specs, release lags, staleness
│   ├── cache.py               parquet cache
│   ├── bloomberg_provider.py  blpapi, //blp/refdata
│   └── macrobond_provider.py  COM to the desktop application
└── tests/test_sentiment.py    72 tests, no vendor connection needed
```

## Setup

```
pip install -r requirements.txt
python tests/test_sentiment.py
```

Set `cache_dir` in `config/sentiment_config.yaml` to a local unsynced path.
OneDrive-backed folders corrupt parquet on concurrent write.

## Running

```
uvicorn api_sentiment:app --host 0.0.0.0 --port 8010
```

Open `sentiment.html` from the file system and set the endpoint via the gear
icon if the backend is on another machine. To mount on the existing House View
app instead:

```python
from api_sentiment import router as sentiment_router
app.include_router(sentiment_router)
```

Macrobond requires the desktop application to be running and signed in, since
it hosts the COM server. Bloomberg requires the Terminal logged in on the same
machine.

### Endpoints

| Route | Purpose |
|---|---|
| `GET /api/sentiment/` | current reading, bands, evaluation. `?refresh=true` refetches |
| `GET /api/sentiment/history` | reading series with walk-forward thresholds |
| `GET /api/sentiment/inputs` | input metadata and the build report |
| `GET /api/sentiment/health` | provider status and input count |

## Data sources

Thirteen inputs from Bloomberg, three from Macrobond, three run on partial legs,
two are substitutes.

CFTC series were verified against the CFTC public API: for report date
2026-08-04, `cftc_cme13874a_8o` / `_9o` / `_7o` returned 289,619 / 296,567 /
3,170,383, matching the published figures exactly. `usfund0066` was confirmed as
the total money market universe by additivity, retail plus institutional
summing to the total.

### Substitutes

Three inputs approximate rather than reproduce the published construction, and
are labelled as such in the tab.

- **Inputs 1 and 4** use CFTC Leveraged Funds net positioning in place of a
  rolling CTA beta. Barclay Hedge CTA indices are monthly and were 71 days stale
  at the time of writing, so neither a 20-day beta nor a live reading is
  possible from them. History starts 2006 rather than 1997.
- **Input 9** substitutes a momentum and open-interest composite for HSBC's
  undisclosed positioning model.

Input 10 runs equity-only: no usable Treasury put/call series exists. Input 20
runs without Investors Intelligence, which is a paid feed with no Bloomberg
ticker.

## Departures from the published methodology

Each of these removes a degree of freedom or corrects a measurement problem
rather than adding tuning surface.

**Lift over base rate, not hit rates.** Three-month equity returns are positive
around 70% of the time, so a buy signal hitting 75% is close to uninformative
while a sell signal hitting 45% is not. Every performance figure is reported as
conditional rate minus unconditional base rate.

**Newey-West standard errors.** Overlapping h-period returns sampled every
period share h-1 periods of data. Combined with a persistent signal this
overstates naive t-statistics by a factor of three to five; the test suite
demonstrates a 4.5x case. Effective sample size is reported alongside every
statistic and a non-overlapping subsample cross-check runs in parallel.

**Calibrated bands.** The published 20/30/40/50 thresholds are round numbers.
Thresholds are derived from the reading's own distribution, with bootstrap
intervals, and computed walk-forward so they are not fitted on the data they
judge. Note the replica reading is discrete on a 1/k grid: with 20 inputs it
moves in 5-point steps, so distinctions finer than that are not meaningful.

**Cluster weighting.** Inputs are grouped into nine correlation clusters and
weighted by cluster in the improved mode. An equal-weighted count over twenty
inputs where eight are variations on low equity volatility is not a twenty-input
indicator; the redundancy diagnostic reports the effective count.

**No crisis deletion.** The published method removes 2007-09 and 2020 from
certain inputs. Robust median/MAD scaling with winsorisation achieves the same
goal without discarding the episodes a sentiment indicator exists to handle.

**Hinge firing.** Binary firing encodes a real belief, that sentiment matters
only in the tails, but discards magnitude: an input at the 99.9th percentile
counts the same as one at the 90.1st. The hinge is zero at the threshold and
rises to one at the extreme. The binary count remains available as the replica.

**Release-lag stamping.** Observations carry the date they became public, not
their as-of date. CFTC data is measured Tuesday and published Friday; without
this a three-day look-ahead enters every backtest.

**Explicit denominators.** An unavailable input leaves the denominator rather
than counting as not firing, and the live denominator is shown next to the
reading. Otherwise a data outage looks like falling sentiment.

## Known limitations

- Vendor data is current-vintage, not point-in-time. Revisions to short
  interest, fund assets and CFTC figures are not reproducible as originally
  published.
- The ETF and fund baskets are fixed and screened on current size, so they are
  survivorship-biased by construction.
- Composite weights within inputs 10, 15, 17 and 19 are assumptions. HSBC do
  not publish theirs.
- Signal thresholds and transform horizons are as published, and were chosen by
  HSBC with knowledge of the full sample. Reproducing them faithfully inherits
  that selection. Walk-forward evaluation is the check, not the backtest.
