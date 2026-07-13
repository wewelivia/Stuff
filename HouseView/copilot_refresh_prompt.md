# House View - Copilot Refresh Prompt

A reusable prompt for **Microsoft 365 Copilot** (work chat) that returns a
`releases_snapshot.json` for the View Challenges dashboard, on contract, with a
source for every figure. Re-run it whenever you want fresh data.

## How to use

1. In Copilot work chat, open the menu (top-right) and make sure the **Web
   content** toggle is **on**. Without web grounding Copilot can't fetch current
   prints and the refresh won't work.
2. Copy everything between the `=== PASTE INTO COPILOT ===` markers below into
   Copilot and send. (One-time: edit the RELEASE REGISTER so it matches the
   releases you track in `config.yaml`. The starter set already covers the majors
   across all six themes.)
3. Copilot returns a single JSON object. Save it as
   `providers/releases_snapshot.json` and refresh the dashboard.

If your tenant blocks web grounding, or DLP blocks the search, the fetch step
won't run - that's the one hard dependency to confirm with IT.

---

=== PASTE INTO COPILOT ===

You are refreshing the data snapshot for a macro "House View" dashboard. Return a
single JSON object and nothing else.

CONTEXT
- The dashboard scores each economic release as challenging, confirming or neutral
  to a house view, across these themes only: growth, consumption, inflation,
  rate_policy, sentiment, labour.
- You are filling in the latest data for a fixed list of releases (the RELEASE
  REGISTER below). Do not add releases that are not in the register.
- Use current web sources (web grounding must be enabled). Prefer official
  statistical agencies (BLS, BEA, ONS, Eurostat) and reputable financial press for
  consensus figures.

FOR EACH RELEASE IN THE REGISTER
- Find its most recent published print and output one record with status
  "Released", filling: actual, consensus (the market forecast/expected figure that
  applied to that print), previous, the release date, and a source.
- If you know the next scheduled date and it has not printed yet, also output one
  record with status "Upcoming", with the date set and consensus/actual = null.
- Keep the static fields (region, pillar, theme, impact, assets, higher_is, unit)
  exactly as given in the register. Do not change them.

RULES (important)
- Never guess a number. If you cannot verify consensus, actual or previous from a
  current source, set that field to null. For a Released record with no consensus,
  add "_note": "consensus not found". A null is always better than a fabricated
  figure - a wrong number silently flips a verdict on the dashboard.
- Every Released record must include "source": the publication name plus the
  headline or date the numbers came from.
- Value fields are plain numbers, no symbols: write 3.2 not "3.2%", and 175 with
  unit "K" rather than "175,000". Put the unit in the existing unit field.
- Dates: "date" as "DD Mon YYYY" (e.g. "11 Jun 2026"); "date_iso" as "YYYY-MM-DD".
- Use British spelling in any text. Output only the JSON, in one code block, with no
  commentary before or after. Set "as_of" to today's date.

OUTPUT SHAPE
{
  "as_of": "YYYY-MM-DD",
  "releases": [
    {
      "event": "...", "region": "...", "pillar": "...", "theme": "...",
      "impact": "...", "assets": ["..."], "higher_is": "...", "unit": "...",
      "date": "DD Mon YYYY", "date_iso": "YYYY-MM-DD",
      "consensus": null, "actual": null, "previous": null,
      "status": "Released or Upcoming", "source": "..."
    }
  ]
}

RELEASE REGISTER  (edit to match your config.yaml; this starter covers the majors)
[
  {"event": "US CPI YoY", "region": "US", "pillar": "Prices", "theme": "inflation", "impact": "high", "assets": ["UST","USD","Equities"], "higher_is": "hawkish", "unit": "%"},
  {"event": "US Core PCE YoY", "region": "US", "pillar": "Prices", "theme": "inflation", "impact": "high", "assets": ["UST","USD"], "higher_is": "hawkish", "unit": "%"},
  {"event": "US Non Farm Payrolls", "region": "US", "pillar": "Labour", "theme": "labour", "impact": "high", "assets": ["UST","USD","Equities"], "higher_is": "good", "unit": "K"},
  {"event": "US Unemployment Rate", "region": "US", "pillar": "Labour", "theme": "labour", "impact": "high", "assets": ["UST","USD"], "higher_is": "bad", "unit": "%"},
  {"event": "US ISM Manufacturing PMI", "region": "US", "pillar": "Sentiment", "theme": "sentiment", "impact": "medium", "assets": ["USD","Equities"], "higher_is": "good", "unit": "index"},
  {"event": "US ISM Services PMI", "region": "US", "pillar": "Sentiment", "theme": "sentiment", "impact": "high", "assets": ["USD","Equities"], "higher_is": "good", "unit": "index"},
  {"event": "US Retail Sales MoM", "region": "US", "pillar": "Consumption", "theme": "consumption", "impact": "high", "assets": ["USD","Equities"], "higher_is": "good", "unit": "%"},
  {"event": "US GDP QoQ Annualised", "region": "US", "pillar": "Growth", "theme": "growth", "impact": "high", "assets": ["UST","USD","Equities"], "higher_is": "good", "unit": "%"},
  {"event": "US Federal Funds Rate", "region": "US", "pillar": "Policy", "theme": "rate_policy", "impact": "high", "assets": ["UST","USD"], "higher_is": "hawkish", "unit": "%"},
  {"event": "UK CPI YoY", "region": "UK", "pillar": "Prices", "theme": "inflation", "impact": "high", "assets": ["Gilts","GBP"], "higher_is": "hawkish", "unit": "%"},
  {"event": "UK GDP MoM", "region": "UK", "pillar": "Growth", "theme": "growth", "impact": "high", "assets": ["Gilts","GBP"], "higher_is": "good", "unit": "%"},
  {"event": "UK Retail Sales MoM", "region": "UK", "pillar": "Consumption", "theme": "consumption", "impact": "medium", "assets": ["GBP"], "higher_is": "good", "unit": "%"},
  {"event": "UK Unemployment Rate", "region": "UK", "pillar": "Labour", "theme": "labour", "impact": "medium", "assets": ["GBP","Gilts"], "higher_is": "bad", "unit": "%"},
  {"event": "Bank of England Bank Rate", "region": "UK", "pillar": "Policy", "theme": "rate_policy", "impact": "high", "assets": ["Gilts","GBP"], "higher_is": "hawkish", "unit": "%"},
  {"event": "Euro Area CPI YoY Flash", "region": "Euro Area", "pillar": "Prices", "theme": "inflation", "impact": "high", "assets": ["Bunds","EUR"], "higher_is": "hawkish", "unit": "%"},
  {"event": "Euro Area GDP QoQ Flash", "region": "Euro Area", "pillar": "Growth", "theme": "growth", "impact": "high", "assets": ["Bunds","EUR"], "higher_is": "good", "unit": "%"},
  {"event": "ECB Deposit Facility Rate", "region": "Euro Area", "pillar": "Policy", "theme": "rate_policy", "impact": "high", "assets": ["Bunds","EUR"], "higher_is": "hawkish", "unit": "%"},
  {"event": "Germany Ifo Business Climate", "region": "Euro Area", "pillar": "Sentiment", "theme": "sentiment", "impact": "medium", "assets": ["Bunds","EUR"], "higher_is": "good", "unit": "index"}
]

=== END PASTE ===

---

## Notes

- The starter register has 18 releases spanning all six themes across US, UK and
  Euro Area. Replace or extend it with your full 45 from `config.yaml` so event
  names, themes and `higher_is` match your build exactly.
- `higher_is` encodes what a higher-than-expected print means for the verdict:
  "hawkish" for inflation and policy rates, "good" for growth/consumption/activity,
  "bad" for unemployment (a higher print is weaker). Adjust to your convention.
- Output lands in the `{ "as_of", "releases": [...] }` shape your SnapshotProvider
  already accepts, so it drops straight into `providers/releases_snapshot.json`.
- Sanity-check a couple of the `source` lines on each refresh. Copilot's web
  grounding is solid for the major prints but thinner on the long tail, where it
  should be returning nulls rather than guesses.
