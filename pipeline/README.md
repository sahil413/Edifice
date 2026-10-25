# Kansas sites pipeline

Daily: `scrape.py` → `sites.json` (plaintext, temp/private only) → `community.py` (adds annexation + community/regulatory score) → `render.py` → StaticCrypt → `sites/index.html` (encrypted, committed).

Sources (all public, no auth, work from a plain server): Kansas Commerce Certified Sites page; LocationOne/LOIS JSON API for
Kansas Commerce, Evergy and Greater Wichita Partnership; HIFLD transmission lines and EIA gas pipelines (ArcGIS);
OpenStreetMap substations (Overpass; falls back to cache, then HIFLD line end-points).

Private inputs are committed only encrypted (`openssl aes-256-cbc -pbkdf2`): `contacts.enc` (curated local EDO contacts) and
`state.enc` (first-seen dates/hashes for NEW badges). Edit contacts with:

    python3 pipeline/crypt.py dec pipeline/contacts.enc private/contacts.json   # edit, then
    python3 pipeline/crypt.py enc private/contacts.json pipeline/contacts.enc

## Community / regulatory score and annexation (`community.py`)

- **Overall** = 60% infrastructure score (from `scrape.py`) + 40% community score; **community** = 60% regulatory + 40% sentiment.
- **Regulatory** (0–100, starts at 100): moratoriums/bans/ballot measures/data-center rules for the governing jurisdiction
  (city if annexed, else county), the secondary one (county, or nearest city within 3 mi) and Kansas statewide.
- **Sentiment** (0–100, starts at 70): dated news, hearings, petitions, opposition/support and utility rate cases, weighted by recency.
- **Annexation**: listing pin tested against U.S. Census TIGERweb incorporated places (Kansas), plus listing text cues
  ("annexation", "outside city limits", …) → annexed / unincorporated / pending.
- **Sources**: hand-verified items in `issues.enc` (edit like contacts: `crypt.py dec pipeline/issues.enc private/issues.json`,
  then re-`enc`) pin the key regulatory status for each jurisdiction; Bing News + Google News RSS fill in the rest,
  auto-tagged and only kept if the article page was fetched and is about Kansas. `exclude_urls` and `overrides` in issues.json
  fix mis-tags. News queries are cached 6 days (`--news-max-age-h 144`), so sentiment refreshes at least weekly.

Local run (password in EDIFICE_SITES_PASSWORD):

    python3 pipeline/scrape.py --out-dir private/data --geo-cache-dir private/cache --contacts private/contacts.json --state private/state.json
    python3 -W ignore pipeline/community.py --data private/data/sites.json --cache-dir private/cache/community --curated private/issues.json --state private/state.json
    python3 pipeline/render.py --data private/data/sites.json
    python3 pipeline/weekly_report.py --data private/data/sites.json --snapshots private/snapshots --out private/reports

CI (`.github/workflows/refresh.yml`) uses repo secret `SITES_PASSWORD` (same password).
