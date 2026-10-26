# Kansas sites pipeline

Daily: `scrape.py` → `sites.json` (plaintext, temp/private only) → `community.py` (adds annexation + community score: laws & zoning, local news, EDC) → `render.py` → StaticCrypt → `sites/index.html` (encrypted, committed).

Sources (all public, no auth, work from a plain server): Kansas Commerce Certified Sites page; LocationOne/LOIS JSON API for
Kansas Commerce, Evergy and Greater Wichita Partnership; HIFLD transmission lines and EIA gas pipelines (ArcGIS);
OpenStreetMap substations (Overpass; falls back to cache, then HIFLD line end-points).

Private inputs are committed only encrypted (`openssl aes-256-cbc -pbkdf2`): `contacts.enc` (curated local EDO contacts) and
`state.enc` (first-seen dates/hashes for NEW badges). Edit contacts with:

    python3 pipeline/crypt.py dec pipeline/contacts.enc private/contacts.json   # edit, then
    python3 pipeline/crypt.py enc private/contacts.json pipeline/contacts.enc

## Community score and annexation (`community.py` + `local_review.py`)

- **Overall** = 60% infrastructure score (from `scrape.py`) + 40% community score.
- **Community** = 45% laws & zoning + 30% local news + 25% economic development org (EDC). Tap the score on the page for all three, with sources.
- **Laws & zoning** (0–100, starts at 100): for the governing jurisdiction (city if annexed, else county) and the secondary one
  (county, or a city within 3 mi that could annex): zoning district, whether data centers are allowed by right (0), need a
  CUP/SUP (−10), have rules proposed (−5), are not defined in the code (−5), are prohibited/under moratorium (−50), or unknown (−5);
  plus hand-verified moratoriums, bans and ballot measures (city, county, Kansas statewide) and noise/water/setback rules.
  Every claim is a citation in `local_review.enc` (`laws`) with URL, quote, section/topic and date; quotes are re-checked against
  the live page weekly and labelled verified / page reachable / quote not found / verified via search index.
- **Local news** (0–100, starts at 70): only items about the site's own city or county (headline names the place, or the summary
  does under a data-center/power headline, or a hand-verified item for that jurisdiction). Statewide and national stories are
  dropped. If none qualify the page says "No local news found" and lists the searches run.
- **EDC** (0–100): the local EDC/EDO site (`local_review.enc` → `edc`; homepage + up to 8 linked pages, cached and refreshed weekly).
  Reviewed → base 50; +15 markets to data-center / large-load users; +10 tech / advanced industry; +5 per incentive type
  (IRB, tax abatement, Opportunity Zone, PEAK/HPIP, TIF/STAR, …; max +15); +10 published data-center / large-load policy.
  Blocked or unreachable sites score 40. Findings are verbatim sentences linked to the page they came from.
- **Annexation**: listing pin tested against U.S. Census TIGERweb incorporated places (Kansas), plus listing text cues
  ("annexation", "outside city limits", …) → annexed / unincorporated / pending.
- **Sources**: hand-verified items in `issues.enc` and `local_review.enc` (edit like contacts: `crypt.py dec pipeline/local_review.enc
  private/local_review.json`, then re-`enc`); Bing News + Google News RSS fill in news, auto-tagged and only kept if the article page
  was fetched and is about Kansas. News is cached 6 days (`--news-max-age-h 144`); EDC crawls and law-citation checks 7 days.

Local run (password in EDIFICE_SITES_PASSWORD):

    python3 pipeline/scrape.py --out-dir private/data --geo-cache-dir private/cache --contacts private/contacts.json --state private/state.json
    python3 -W ignore pipeline/community.py --data private/data/sites.json --cache-dir private/cache/community --curated private/issues.json --state private/state.json --local-review private/local_review.json
    python3 pipeline/render.py --data private/data/sites.json
    python3 pipeline/weekly_report.py --data private/data/sites.json --snapshots private/snapshots --out private/reports

CI (`.github/workflows/refresh.yml`) uses repo secret `SITES_PASSWORD` (same password).

## Local-government stance (`tracker.py`) and outreach plans (`outreach.py`)

Source: the More Than Just Parks Data Center Tracker, Kansas state page
(https://morethanjustparks.com/data-center-tracker/state/kansas). Every Kansas entry's popup is captured,
followed to its original source, and hand-reviewed (summary, signal, direction, size) in
`tracker_review.enc`; the last good pull is kept in `tracker_state.enc`. The original source is cited first,
the tracker second. Detail pages are refetched when new or older than 7 days (max 15 per run); if the tracker
is unreachable the cached pull is used.

Adjustment to **Laws & zoning** (points; governing jurisdiction / secondary, e.g. county around a city):

| Signal | Governing | Secondary |
|---|---|---|
| Data-center corridor (exact jurisdiction) | +20 | +10 |
| Corridor, approximate jurisdiction match (disclosed) | +12 | +6 |
| Government support — approved (incentive, agreement, rezoning) | +10 | +5 |
| Government support — engaged (study, MOU, talks) | +6 | +3 |
| Precedent (a project exists; 0 under an active moratorium) | +3 | +1 |
| Government refusal | −8 | −4 |
| Organized opposition | −5 | −2 |
| Moratorium active / proposed / lifted | −60 / −30 / −5 | −25 / −12 / −2 |

No double counting: a moratorium already in the hand-verified regulatory items scores 0 here; news
articles credited as a stance source are removed from the News score; per jurisdiction only the largest
positive and the largest negative count; the site total is capped at +20 / −30 and Laws & zoning stays in 0–100.
Each credited item appears in the Laws & zoning panel with its points, summary and links.

`outreach.py` builds a plan for the top 15 sites plus every site in a tracked jurisdiction: stance with
evidence, contact order (EDC → city manager → county commission → utility), lead angle, cited risks with
suggested mitigations, and a next step. The weekly report lists the top 5 plans.

### Draft outbound email
Each site's detail view has **Draft in Gmail** (opens Gmail web compose in a new tab:
`https://mail.google.com/mail/?authuser=<sender>&view=cm&fs=1&to=&cc=&su=&body=`, all values URL-encoded),
**Copy email** (subject + body to the clipboard), and a secondary mail-app (mailto) link. Sender is
selectable (sahil@edifice.vc default, sahil@rattan.org) and remembered in localStorage. Nothing is ever sent.
Powered Land drafts go to the plan's first contact; storage drafts go to the listing broker and ask about
availability, price and terms. Body text uses only the cited data on the page.

## Price per acre (`pricing.py`)

`scrape.py` calls `pricing.price_info()` for each listing. It uses only prices the listing states
(LOIS `salePrice`, `pricingNote`, `leaseRateNote`, or an explicit "Sale/Asking Price: $X" in the
description). $/acre = listed per-acre price (or $/sq ft × 43,560), else total ÷ listed acres.
No price → "—"; "Negotiable" / "Call for price" / "Auction" / "Lease only" echo the listing.
Values outside $1,000–$500,000/acre, totals that disagree with the stated $/acre, sale-price fields
that only make sense per acre, and suspect acreage are flagged (`price.flags`), never corrected.

## 100+ acre rule
`scrape.meets_min_acres()` applies to every source, certified sites included. A listing passes if its
acreage is ≥100. A suspect (>20,000) or missing acreage passes only if the certified-sites list or the
listing text states ≥100 acres; otherwise a suspect value is read as square feet. Dropped listings are
counted under "100+ acre filter" in the data-source status.

## Storage view (`storage.py`)

The page has a **View** dropdown at the top: *Powered Land* (the table above, the default) and
*Storage*. Both views use the same StaticCrypt password. `#st-…` links open the Storage view.

`storage.py` covers Wichita, Overland Park, Kansas City KS, Olathe, Topeka and Lawrence. It produces two
row types:

- **Development land** of 4–12 acres. Anything outside 5–10 acres is flagged.
- **Existing self-storage facilities** for sale.

Listings come from Crexi's public JSON search and the LOIS/CommercialEdge pages run by the local
EDCs. Lease-only, under-contract and sold listings are dropped. Single garage or storage-condo units
are kept apart as format comps and are not scored.

Each row is enriched with these sources:

- **Zoning:** the city's zoning GIS, queried at the listing point. Olathe has no public layer, so
  the zoning stated in the listing is used. Each city's code rules give the self-storage status:
  by right, conditional/SUP or not permitted, with the code section cited.
- **Traffic:** the frontage road from OpenStreetMap and the KDOT AADT within 250 m.
- **Competition:** OSM `shop=storage_rental` facilities within 3 miles, with estimated NRSF.
- **Demand:** ACS 5-year tract data for the 3-mile area, plus Census PEP city growth, BPS permits
  and the nearest lakes and marinas.

Score (0–100) = demand 30 + competition 25 + visibility 15 + zoning 20 + price 10.

Data handling:

- `--cache-dir` holds only area-wide public layers (OSM, Census). It lives in the public
  actions/cache.
- Per-listing-point lookups go to `--point-cache-dir`, which is uncached runner temp.
- The last good listing pull, the first-seen dates and the owner's own facility (counted as
  supply, never shown as a target) live only in `pipeline/storage_state.enc`.
- `STORAGE_REUSE_STATE_H=N` reuses a listing pull younger than N hours. CI uses it as a fallback
  when the live pull fails.

To build locally:

```
python3 pipeline/storage.py --out private/data/storage.json --state private/storage_state.json --cache-dir private/cache/storage
STATICRYPT_PASSWORD="$EDIFICE_SITES_PASSWORD" python3 pipeline/render.py --data private/data/sites.json --storage private/data/storage.json
```
