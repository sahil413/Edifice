#!/usr/bin/env python3
"""Self-storage opportunities in Kansas's six largest cities (Edifice "Storage" view).

Two row types, both from listings fetched live (nothing invented):
  * land      - development land, 4-12 acres (rows outside 5-10 ac are flagged), for sale
  * facility  - existing self-storage facilities (and storage/garage-condo buildings) for sale

Sources (all public):
  Listings   Crexi public search API (api.crexi.com) - Land + Self Storage, per city
             Kansas Commerce LOIS (api.locationone.com) - sites (land) and buildings
  Zoning     City zoning GIS (Wichita, Overland Park, KCK/UG, Topeka, Lawrence); Olathe has no public
             zoning layer we could reach, so the listing's stated zoning is used and flagged.
             District -> by right / conditional / not permitted from each city's code (cited per row).
  Traffic    KDOT AADT (state + non-state routes, 2025 counts); frontage road name from OpenStreetMap
  Supply     OpenStreetMap shop=storage_rental within 3 mi (count, estimated NRSF, nearest)
  Demand     Census ACS 2020-24 5-yr (Census Reporter API) tracts within 3 mi: population, renter share,
             income; city growth 2020->2024 (Census PEP); Census Building Permits Survey (city);
             OSM lakes/reservoirs + marinas (boat/RV demand)

Plaintext output goes only to the --out path (private/ locally, $RUNNER_TEMP in CI). The last good
listing pull is kept in --state (encrypted to pipeline/storage_state.enc by CI) so a blocked listing
source does not empty the table.
"""
import argparse, datetime as dt, io, json, math, os, re, sys, time
import requests

UA = "Mozilla/5.0 (compatible; EdificeSitesBot/1.0; +https://edifice.vc)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
LOIS_API = "https://api.locationone.com"
LOIS_ORGS = ["59eaba35bec80e09b4bbf1d7", "59eaba35bec80e09b4bbf368", "59eaba35bec80e09b4bbefdb"]
KS_ORG = "59eaba35bec80e09b4bbf1d7"
CREXI = "https://api.crexi.com"
KDOT_LAYERS = [("state route", "https://kanplan.ksdot.gov/arcgis_web_adaptor/rest/services/Transportation/AADT_Flow_Map/FeatureServer/0"),
               ("non-state route", "https://kanplan.ksdot.gov/arcgis_web_adaptor/rest/services/Transportation/AADT_NonState/FeatureServer/0")]
KDOT_PAGE = "https://www.ksdot.gov/about/our-organization/transportation-planning/traffic-counts-and-maps"
CENSUS_REPORTER = "https://api.censusreporter.org/1.0/data/show/latest"
GAZ = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_gaz_tracts_{st}.txt"
BPS = "https://www2.census.gov/econ/bps/Place/Midwest%20Region/mw{yr}a.txt"
PEP_URL = "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv"
GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
MI = 1609.344
LAND_MIN, LAND_MAX, FIT_MIN, FIT_MAX = 4.0, 12.0, 5.0, 10.0

S = requests.Session(); S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
STATUS = {}

def log(*a): print("[storage]", *a, file=sys.stderr, flush=True)

# --------------------------------------------------------------------------- cities
# Planning + EDC contacts are the cities' / EDCs' own published contacts (source URL kept per contact).
CITIES = {
 "Wichita": {"bbox": (37.55, -97.58, 37.83, -97.13), "crexi": ["Wichita"], "lois": ["wichita"], "place": "79000",
   "counties": ["20173", "20015"], "zoning_gis": ("https://gismaps.wichita.gov/ageweb/rest/services/COWGIS/Plan_Zoning/MapServer/7", "ZONING", None),
   "contacts": [
     {"name": "Metropolitan Area Planning Dept. (MAPD)", "title": "City of Wichita / Sedgwick County planning", "org": "City of Wichita", "email": "planning@wichita.gov", "phone": "(316) 268-4421", "address": "271 W. 3rd St. N., Wichita", "role": "City planning / zoning", "source": "https://www.wichita.gov/planning"},
     {"name": "Andrew Nave", "title": "Economic development", "org": "Greater Wichita Partnership", "email": "andrew@greaterwichitapartnership.org", "phone": "(316) 500-6650", "role": "EDC", "source": "https://greaterwichitapartnership.org/"}]},
 "Overland Park": {"bbox": (38.80, -94.78, 39.05, -94.60), "crexi": ["Overland Park"], "lois": ["overland park"], "place": "53775",
   "counties": ["20091", "29095"], "zoning_gis": ("https://services1.arcgis.com/YQsWDBr0DjMtoTQo/arcgis/rest/services/Zoning/FeatureServer/3", "District", "DistrictName"),
   "contacts": [
     {"name": "Planning & Development Services", "title": "Planner on duty", "org": "City of Overland Park", "email": "pod@opkansas.org", "phone": "(913) 895-6000", "address": "8500 Santa Fe Dr., Overland Park", "role": "City planning / zoning", "source": "https://www.opkansas.gov/planning-and-development-services", "note": "pod@ email published on the OP Chamber EDC page; phone = City Hall"},
     {"name": "Imani Lemon", "title": "Economic development resources", "org": "Overland Park Chamber EDC", "email": "ilemon@opchamber.org", "phone": "(913) 766-7605", "role": "EDC", "source": "https://www.opchamber.org/edc-staff-assistance/"}]},
 "Kansas City": {"bbox": (39.03, -94.97, 39.20, -94.59), "crexi": ["Kansas City"], "lois": ["kansas city"], "place": "36000",
   "counties": ["20209", "20091", "20103", "29095", "29165"], "zoning_gis": ("https://services1.arcgis.com/Qo2HHQp8vgPs2wg3/arcgis/rest/services/Zoning/FeatureServer/0", "ZONEDIST", "ZONENAME"),
   "contacts": [
     {"name": "Rodney Lucas", "title": "Interim Planning Director, Planning & Urban Design", "org": "Unified Government of Wyandotte County/KCK", "email": "planninginfo@wycokck.org", "phone": "(913) 573-5750", "address": "701 N 7th St, Suite 423, Kansas City, KS", "role": "City planning / zoning", "source": "https://www.wycokck.org/Departments/Planning-and-Urban-Design"},
     {"name": "Jud Knapp", "title": "Interim Director, Economic Development", "org": "Unified Government of Wyandotte County/KCK", "email": "edevelopment@wycokck.org", "phone": "(913) 573-5730", "address": "701 N 7th St, Suite 421", "role": "EDC", "source": "https://www.wycokck.org/Departments/Economic-Development"}]},
 "Olathe": {"bbox": (38.80, -94.93, 38.96, -94.72), "crexi": ["Olathe"], "lois": ["olathe"], "place": "52575",
   "counties": ["20091"], "zoning_gis": None,
   "contacts": [
     {"name": "Planning Division", "title": "Development Services", "org": "City of Olathe", "email": "planningcontact@olatheks.org", "phone": "(913) 971-8750", "address": "100 E. Santa Fe St., Olathe", "role": "City planning / zoning", "source": "https://www.olatheks.gov/government/development-services/planning", "note": "from search results — olatheks.gov blocks automated fetches; confirm"},
     {"name": "Shannon Latham", "title": "Director, Economic Development", "org": "Olathe Chamber / Olathe EDC", "email": "edc@olathe.org", "phone": "(913) 764-1050", "role": "EDC", "source": "https://olathe.org/economic-development-council/about-us/staff/", "note": "name/title from staff page; email/phone from the EDC business-resources page"}]},
 "Topeka": {"bbox": (38.93, -95.83, 39.13, -95.58), "crexi": ["Topeka"], "lois": ["topeka"], "place": "71000",
   "counties": ["20177"], "zoning_gis": ("https://maps.topeka.gov/arcgis/rest/services/OpenData/Open_Data_Base_Layers/FeatureServer/12", "ZONECLASS", None),
   "contacts": [
     {"name": "Planning & Development", "title": "Current planning", "org": "City of Topeka", "email": "planning@topeka.org", "phone": "(785) 368-3728", "role": "City planning / zoning", "source": "https://topeka.gov/business/planning/planning_services/index.php"},
     {"name": "Rhiannon Friedman", "title": "Economic development", "org": "GO Topeka", "email": "rhiannon.friedman@topekapartnership.com", "phone": "(785) 234-2644", "role": "EDC", "source": "https://www.gotopeka.com/"}]},
 "Lawrence": {"bbox": (38.89, -95.36, 39.02, -95.16), "crexi": ["Lawrence"], "lois": ["lawrence"], "place": "38900",
   "counties": ["20045"], "zoning_gis": ("https://services.arcgis.com/8O9UlSTnqjKptoda/arcgis/rest/services/Zoning_Districts/FeatureServer/1", "ZoningDistrict", None),
   "contacts": [
     {"name": "Planning & Development Services", "title": "Planner of the Day (785) 832-3172", "org": "City of Lawrence", "email": "planning@lawrenceks.gov", "phone": "(785) 832-7700", "address": "City Hall, 6 E 6th St., Lawrence", "role": "City planning / zoning", "source": "https://www.lawrenceks.gov/City/PDS"},
     {"name": "Steve Kelly", "title": "Vice President, Economic Development", "org": "EDC of Lawrence & Douglas County", "email": "skelly@lawrencechamber.com", "phone": "(785) 865-4425", "role": "EDC", "source": "https://edclawrence.com/about-us/staff"}]},
}
CITY_LABEL = {"Kansas City": "Kansas City, KS"}
# The owner's own facility (name/address) is private: it lives only in the encrypted state (state["own_facility"]).
OWN_FACILITY = {}

# --------------------------------------------------------------------------- zoning rules
# status: P = permitted by right, S = by right subject to use standards, I = by right on staff
# interpretation only (verify), C = conditional / special use permit, X = not listed (rezoning needed)
ZONING = {
 "Wichita": {"code": "Wichita-Sedgwick County UZC §III-D.1 Use Regulation Schedule (Warehouse, Self-Service Storage); §III-D.6.y",
   "url": "https://www.wichita.gov/DocumentCenter/View/9763/Use-Regulations-Quick-Look-Chart-PDF",
   "P": ["GC", "OW", "IP", "CBD", "LI", "GI"], "C": ["GO", "LC"],
   "C_note": "Conditional Use under §III-D.6.y: arterial access, 35 ft arterial setback, resident manager, ≤45% coverage, indoor storage only (applied in CON2026-00035, Apr 2026)",
   "drive_up": ["GC", "OW", "IP", "LI", "GI"], "rv": ["GC", "IP", "LI", "GI"], "contractor": ["LI", "GI", "IP"]},
 "Overland Park": {"code": "OP UDO §18.370.020.FF / §18.370.040.M (indoor self-storage SUP); §18.300.020.B (M-1/M-2/BP storage of any commodity)",
   "url": "https://opkansas.civicweb.net/document/59658/",
   "I": ["M-1", "MP-1", "M-2", "MP-2", "BP"], "C": ["C-2", "CP-2", "C-3", "CP-3", "MXD"],
   "I_note": "Industrial/BP 'warehousing or storage of any commodity' — staff interpretation; confirm with OP Planning",
   "C_note": "Special Use Permit: fully enclosed, climate-controlled, no exterior overhead doors, convertible to retail",
   "drive_up": ["M-1", "MP-1", "M-2", "MP-2"], "rv": ["M-1", "MP-1", "M-2", "MP-2"], "contractor": ["BP", "M-1", "MP-1", "M-2", "MP-2"]},
 "Kansas City": {"code": "UG Code Ch. 27 §27-467 (C-3 'Self-storage buildings'); §27-469 M-2; §27-470 M-3",
   "url": "https://online.encodeplus.com/regs/kansascity-ks/doc-viewer.aspx?secid=290",
   "P": ["C-3", "CP-3", "M-2", "MP-2", "M-3", "MP-3"], "I": ["M-1", "MP-1"],
   "I_note": "M-1 lists 'warehousing or wholesaling of any commodity' but not self-storage — confirm with UG Planning",
   "drive_up": ["C-3", "CP-3", "M-2", "MP-2", "M-3", "MP-3"], "rv": ["C-3", "CP-3", "M-2", "MP-2", "M-3", "MP-3"], "contractor": ["C-3", "CP-3", "M-1", "MP-1", "M-2", "MP-2", "M-3", "MP-3", "B-P", "BP"]},
 "Olathe": {"code": "Olathe UDO §18.20.500 Use Matrix (Mini-Warehouse)", "url": "https://olathe.municipal.codes/UDO/18.20.500",
   "P": ["C-3", "C-4", "M-1", "M-2", "M-3", "CP-3", "CP-4", "MP-1", "MP-2", "MP-3"],
   "drive_up": ["C-3", "C-4", "M-1", "M-2", "M-3", "CP-3", "CP-4", "MP-1", "MP-2", "MP-3"], "rv": ["M-1", "M-2", "M-3", "MP-1", "MP-2", "MP-3"], "contractor": ["M-1", "M-2", "M-3", "MP-1", "MP-2", "MP-3"]},
 "Topeka": {"code": "Topeka Municipal Code §18.60.010 use table (Self-Storage Type I/II); standards Ch. 18.225", "url": "https://topeka.municipal.codes/TMC/18.60.010",
   "P": ["I-1", "I-2"], "S": ["C-3", "C-4", "X-2", "D-3"], "C": ["C-2", "X-1"],
   "S_note": "Type I (indoor access) permitted subject to Ch. 18.225 standards; Type II (drive-up/boat-RV) needs a CUP in X-2/D-3 and is not allowed in C-3/C-4",
   "C_note": "Conditional Use Permit (Type I)",
   "drive_up": ["I-1", "I-2"], "drive_up_C": ["X-2", "D-3"], "rv": ["I-1", "I-2"], "contractor": ["I-1", "I-2", "C-4"]},
 "Lawrence": {"code": "Lawrence LDC (Apr 14 2026) Table 20-8-1; §20-807(f),(g) self-storage standards", "url": "https://assets.lawrenceks.gov/pds/planning/LawrenceLDCApril142026Clean.pdf",
   "P": ["M-3", "CC", "IL", "IG", "IBP"],
   "P_note": "Interior self-storage P in M-3/CC/IL/IG/IBP; exterior (drive-up) only M-3/IL/IG. Table columns read from the PDF text layer — verify",
   "drive_up": ["M-3", "IL", "IG"], "rv": ["M-3", "CC", "IL", "IG"], "contractor": ["IL", "IG", "IBP", "M-3"]},
}
STATUS_TXT = {"P": "By right", "S": "By right (use standards)", "I": "By right? (interpretation — verify)", "C": "Conditional / SUP", "X": "Not permitted (rezoning)", "?": "Unknown"}

def norm_district(city, z):
    z = re.sub(r"\s+", "", str(z or "").upper())
    if not z: return ""
    if city == "Topeka":
        m = re.match(r"^([A-Z]{1,3})(\d)$", z)
        if m: z = f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"^([A-Z]{1,3})(\d)$", z)
    if m and city in ("Overland Park", "Kansas City", "Olathe"): z = f"{m.group(1)}-{m.group(2)}"
    return z

TEXT_ZONING = [(r"\blimited commercial\b", "LC"), (r"\bgeneral commercial\b", "GC"), (r"\blimited industrial\b", "LI"), (r"\bgeneral industrial\b", "GI"),
               (r"\bindustrial park\b", "IP"), (r"\bgeneral office\b", "GO")]

def zoning_from_text(city, txt):
    t = str(txt or "")
    for rx, code in TEXT_ZONING:
        if city == "Wichita" and re.search(rx, t, re.I): return code
    m = re.search(r"\b(?:zoned|zoning)\s*[:\-]?\s*(?:is\s+)?\(?([A-Z]{1,3}-?\d?[A-Z]?)\b", t)
    if m and re.match(r"^[A-Z]{1,3}-?\d", m.group(1)): return norm_district(city, m.group(1))
    m = re.match(r"^\s*([A-Z]{1,3}-?\d{0,2}[A-Z]?)\b", t)
    if m and len(t) < 40: return norm_district(city, m.group(1))
    return ""

def classify(city, district):
    R = ZONING[city]; d = district
    if not d: return "?", ""
    base = re.sub(r"^([CM])P-", r"\1-", d)
    for st in ("P", "S", "I", "C"):
        if d in R.get(st, []) or base in R.get(st, []): return st, R.get(st + "_note", "")
    if re.match(r"^(PUD|CUP|DP|PD)", d): return "?", "Planned district — permitted uses follow its plan text"
    return "X", "Not listed for self-storage in this district"

def fmt_allowed(city, district, key):
    R = ZONING[city]; base = re.sub(r"^([CM])P-", r"\1-", district or "")
    if district in R.get(key, []) or base in R.get(key, []): return "P"
    if district in R.get(key + "_C", []): return "C"
    return ""

# --------------------------------------------------------------------------- http helpers
def get(url, *, params=None, timeout=60, tries=3, headers=None, method="GET", js=None):
    last = None
    for i in range(tries):
        try:
            r = S.request(method, url, params=params, json=js, timeout=timeout, headers=headers or {})
            if r.status_code == 200: return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (401, 403, 404): break
        except Exception as e:
            last = repr(e)[:200]
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")

def cached(cache_dir, name, fn, max_age_h=20):
    p = os.path.join(cache_dir, name) if cache_dir else None
    if p and os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
        return json.load(open(p))
    try:
        v = fn()
    except Exception as e:
        if p and os.path.exists(p):
            log(f"WARN {name}: live fetch failed ({str(e)[:120]}), using cache"); return json.load(open(p))
        raise
    if p:
        os.makedirs(cache_dir, exist_ok=True); json.dump(v, open(p, "w"))
    return v

def overpass(q):
    last = None
    for url in OVERPASS:
        for i in range(2):
            try:
                r = requests.post(url, data={"data": q}, timeout=150, headers={"User-Agent": "EdificeSitesBot/1.0 (+https://edifice.vc)", "Accept": "application/json"})
                if r.status_code == 200:
                    j = r.json()
                    if "runtime error" in (j.get("remark") or ""): last = "partial result: " + j["remark"][:120]
                    else: return j.get("elements", [])
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = repr(e)[:150]
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"overpass: {last}")

def hav(a, b, c, d):
    p1, p2 = math.radians(a), math.radians(c); dp = p2 - p1; dl = math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))

def seg_dist(lat, lon, path):
    """metres from point to a polyline [(lat,lon),...] (local equirectangular)."""
    kx = 111320 * math.cos(math.radians(lat)); ky = 110540; best = 1e12
    pts = [((p[1] - lon) * kx, (p[0] - lat) * ky) for p in path]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1; L = dx * dx + dy * dy
        t = 0 if L == 0 else max(0, min(1, -(x1 * dx + y1 * dy) / L))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    if len(pts) == 1: best = math.hypot(*pts[0])
    return best

def poly_area_m2(geom):
    if not geom or len(geom) < 3: return 0
    lat0 = sum(p["lat"] for p in geom) / len(geom); kx = 111320 * math.cos(math.radians(lat0)); ky = 110540
    xy = [(p["lon"] * kx, p["lat"] * ky) for p in geom]
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]))) / 2

def num(x):
    try:
        v = float(str(x).replace(",", "").replace("$", "").strip()); return v if v > 0 else None
    except Exception: return None

def clean(s): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s or ""))).strip()

# --------------------------------------------------------------------------- listings
CREXI_H = {"User-Agent": BROWSER_UA, "Origin": "https://www.crexi.com", "Referer": "https://www.crexi.com/", "Accept": "application/json"}

def crexi_search(types, city, extra=None):
    out, off = [], 0
    while True:
        body = {"types": types, "states": ["KS"], "cities": [city], "count": 60, "offset": off, "includeUnpriced": True, "sortDirection": "Descending", "sortOrder": "rank"}
        body.update(extra or {})
        d = get(f"{CREXI}/assets/search", method="POST", js=body, headers=CREXI_H, timeout=45).json()
        rows = d.get("data") or []; out += rows
        if len(rows) < 60 or off > 600: break
        off += 60; time.sleep(0.6)
    return out

def crexi_detail(aid):
    d = get(f"{CREXI}/assets/{aid}", headers=CREXI_H, timeout=45).json(); time.sleep(0.4)
    try: b = get(f"{CREXI}/assets/{aid}/brokers", headers=CREXI_H, timeout=45).json()
    except Exception: b = []
    keep = {k: d.get(k) for k in ("id", "name", "askingPrice", "isUnpriced", "status", "types", "subtypes", "details", "urlSlug", "updatedOn", "activatedOn", "brokerageName")}
    keep["locations"] = [{k: l.get(k) for k in ("address", "city", "county", "zip", "latitude", "longitude")} | {"state": (l.get("state") or {}).get("code")} for l in (d.get("locations") or [])]
    keep["description"] = clean(d.get("marketingDescription") or d.get("description"))[:2500]
    keep["brokers"] = [{"name": clean(f"{x.get('firstName','')} {x.get('lastName','')}"), "org": clean((x.get("brokerage") or {}).get("name")),
                        "phone": clean(next((l.get("brokerageLicensePhone") for l in (x.get("licenseDetails") or []) if l.get("brokerageLicensePhone")), "") or x.get("phoneNumber") or ""),
                        "title": clean(next((l.get("brokerTitle") for l in (x.get("licenseDetails") or []) if l.get("brokerTitle")), ""))} for x in (b if isinstance(b, list) else [])]
    return keep

def lois_list(kind, org):
    return get(f"{LOIS_API}/{kind}/{org}", params={"max_results": 5000}, timeout=90).json().get("_items", [])

def lois_detail(kind, sid):
    return get(f"{LOIS_API}/{kind}/{sid}", timeout=45).json()

def lois_url(kind, sid, name=""):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    return f"https://app.locationone.com/{kind}s/{sid}/{slug}?organization={KS_ORG}"

def city_key(c):
    c = clean(c).lower()
    for k, v in CITIES.items():
        if c in v["lois"] or c == k.lower(): return k
    return None

UNITS_RX = re.compile(r"([\d,]{2,7})\s*(?:-|\s)?(?:climate[- ]controlled\s+|storage\s+|self[- ]storage\s+|rentable\s+)?units\b", re.I)
NRSF_RX = re.compile(r"([\d,]{4,9})\s*(?:\+\s*)?(?:net rentable square feet|net rentable sq\.?\s*ft|nrsf|rentable square feet|rentable sf)", re.I)
ACRES_RX = re.compile(r"([\d]+(?:\.\d+)?)\s*(?:\+/-|±)?\s*(?:acres?|ac\b)", re.I)

def pull_listings(args):
    """Return list of raw listing dicts (normalised) from Crexi + LOIS. Raises only if every source fails."""
    rows, errs = [], []
    # --- Crexi
    crexi_ok = 0
    seen = set()
    for city, cfg in CITIES.items():
        for types, kind, extra in ((["Land"], "land", {"acreageMin": 3.5, "acreageMax": 12.5}), (["Self Storage"], "facility", None)):
            for c in cfg["crexi"]:
                try:
                    res = crexi_search(types, c, extra); crexi_ok += 1
                except Exception as e:
                    errs.append(f"Crexi {types[0]} {c}: {str(e)[:100]}"); continue
                for a in res:
                    locs = [l for l in (a.get("locations") or []) if ((l.get("state") or {}).get("code") == "KS")]
                    if not locs or a["id"] in seen: continue
                    if (a.get("status") or "").lower() not in ("on-market", "active", ""): continue
                    seen.add(a["id"])
                    try: d = crexi_detail(a["id"])
                    except Exception as e:
                        errs.append(f"Crexi detail {a['id']}: {str(e)[:80]}"); continue
                    rows.append({"src": "crexi", "kind": kind, "crexi": d})
                time.sleep(0.8)
    STATUS["Crexi listings (api.crexi.com)"] = {"ok": crexi_ok > 0, "detail": f"{sum(1 for r in rows if r['src']=='crexi')} KS listings (Land 3.5-12.5 ac + Self Storage) in 6 cities" if crexi_ok else "; ".join(errs[:3]), "url": "https://www.crexi.com/properties?types%5B%5D=Self%20Storage&states%5B%5D=KS"}
    # --- LOIS sites (land) + buildings (storage facilities)
    lois_ok = 0; got = set()
    for org in LOIS_ORGS:
        for kind in ("sites", "buildings"):
            try: items = lois_list(kind, org); lois_ok += 1
            except Exception as e:
                errs.append(f"LOIS {kind} {org}: {str(e)[:80]}"); continue
            for i in items:
                if i.get("state") != "KS" or i["_id"] in got: continue
                ck = city_key(i.get("city"))
                if not ck: continue
                if kind == "sites":
                    a = num(i.get("acres"))
                    if not a or a < 3.5 or a > 12.5: continue
                else:
                    if not re.search(r"self[- ]?storage|mini[- ]?storage|storage facility|garage condo", i.get("name") or "", re.I): continue
                got.add(i["_id"])
                try: d = lois_detail(kind[:-1], i["_id"])
                except Exception as e:
                    errs.append(f"LOIS detail {i['_id']}: {str(e)[:80]}"); continue
                rows.append({"src": "lois", "kind": "land" if kind == "sites" else "facility", "lois": d, "lois_kind": kind[:-1]})
    STATUS["Kansas Commerce LOIS (sites + buildings)"] = {"ok": lois_ok > 0, "detail": f"{sum(1 for r in rows if r['src']=='lois')} listings in the 6 cities (land 3.5-12.5 ac; storage buildings)", "url": "https://app.locationone.com/"}
    if errs: log("listing warnings:", *errs[:12])
    if not crexi_ok and not lois_ok: raise RuntimeError("all listing sources failed")
    return rows, crexi_ok > 0, lois_ok > 0

def normalise(raw):
    """-> record with common fields, or None if it should be skipped (lease-only, wrong city, etc.)."""
    if raw["src"] == "crexi":
        d = raw["crexi"]; det = d.get("details") or {}
        loc = next((l for l in d["locations"] if l.get("state") == "KS" and city_key(l.get("city"))), None)
        if not loc: return None
        ck = city_key(loc["city"]); desc = d.get("description", "")
        acres = num(det.get("Lot Size (acres)"))
        if not acres and raw["kind"] == "land":
            m = ACRES_RX.search(d.get("name", "") + " " + desc); acres = num(m.group(1)) if m else None
        price = None if d.get("isUnpriced") else num(d.get("askingPrice"))
        sf = num(det.get("Square Footage")) if "*" not in str(det.get("Square Footage", "*")) else None
        nrsf = num(det.get("Net Rentable (SqFt)")) if "*" not in str(det.get("Net Rentable (SqFt)", "*")) else None
        units = num(det.get("Units")) if "*" not in str(det.get("Units", "*")) else None
        if not nrsf:
            m = NRSF_RX.search(desc); nrsf = num(m.group(1)) if m else None
        if not units:
            m = UNITS_RX.search(desc); units = num(m.group(1)) if m else None
        url = f"https://www.crexi.com/properties/{d['id']}/{d.get('urlSlug') or ''}"
        contacts = [{"name": b["name"], "title": b.get("title") or "Listing broker", "org": b.get("org") or d.get("brokerageName") or "", "email": "", "phone": b.get("phone", ""), "role": "Listing broker", "source": url} for b in d.get("brokers", []) if b.get("name")]
        if not contacts and d.get("brokerageName"):
            contacts = [{"name": d["brokerageName"], "title": "Listing brokerage", "org": d["brokerageName"], "email": "", "phone": "", "role": "Listing broker", "source": url}]
        sub = ", ".join(x for x in (det.get("Sub Type"),) if x)
        if raw["kind"] == "facility" and not re.search(r"storage|garage condo|mini.?warehouse|\bunits\b", (sub + " " + d.get("name", "") + " " + desc[:800]).lower()):
            return None   # Crexi "Self Storage" search also returns plain retail/warehouse
        return {"id": f"st-cx{d['id']}", "src": "Crexi", "url": url, "kind": raw["kind"], "name": clean(d.get("name")), "address": clean(loc.get("address")),
                "city": ck, "county": clean(loc.get("county")).replace(" County", ""), "zip": clean(loc.get("zip")), "lat": loc.get("latitude"), "lon": loc.get("longitude"),
                "acres": acres, "price": price, "sf": sf, "nrsf": nrsf, "units": units, "subtype": sub, "zoning_listed": clean(det.get("Zoning")),
                "description": desc[:1500], "contacts": contacts, "updated": (d.get("updatedOn") or "")[:10], "multi_loc": len(d["locations"]) > 1,
                "occupancy": clean(det.get("Occupancy")) if "*" not in str(det.get("Occupancy", "*")) else ""}
    d = raw["lois"]; b = d.get("basicInfo") or {}; loc = b.get("location") or {}; ab = (d.get("additionalDetails") or {}).get("about") or {}
    ck = city_key(loc.get("city"))
    if not ck or (loc.get("state") and loc.get("state") != "KS"): return None
    pr = b.get("pricing") or {}; sale = num(pr.get("salePrice")); note = clean(pr.get("pricingNote"))
    if sale is not None and sale <= 1: sale = None
    lease = num(pr.get("leaseRate"))
    desc = clean(ab.get("description") or (d.get("additionalDetails") or {}).get("description"))
    txt = " ".join([clean(b.get("name") or d.get("name")), desc, note]).lower()
    if not sale and lease and not re.search(r"for sale|sale price|asking", txt): return None   # lease-only
    if re.search(r"\b(for lease only|lease only|ground lease only)\b", txt): return None
    if re.search(r"\bunder contract\b|\bsold\b", clean(b.get("name") or "").lower()): return None
    if re.search(r"\bfor lease\b", clean(b.get("name") or "").lower()) and "sale" not in clean(b.get("name") or "").lower(): return None
    if d.get("active") == 0 or re.search(r'"TransactionDetail": "SOLD"', json.dumps(d.get("import") or {})): return None   # inactive / sold (broker feed)
    coords = (loc.get("geoLocation") or {}).get("coordinates") or [None, None]
    kind = raw["lois_kind"]; name = clean(b.get("name") or d.get("name"))
    url = lois_url(kind, d["_id"], name)
    cs = []
    for k in ("broker", "broker2"):
        c = (d.get("contacts") or {}).get(k) or {}
        if c.get("contactName") or c.get("email") or c.get("phone"):
            cs.append({"name": clean(c.get("contactName")), "title": "Listing contact", "org": clean(c.get("contactName2") or c.get("company")), "email": clean(c.get("email")).lower(), "phone": clean(c.get("phone")), "role": "Listing broker", "source": url})
    for c in ((d.get("contacts") or {}).get("economicDevelopmentContacts") or [])[:1]:
        cs.append({"name": clean(c.get("name")), "title": clean(c.get("title")), "org": clean(c.get("organization")), "email": clean(c.get("email")).lower(), "phone": clean(c.get("phone")), "role": "Listing contact (economic development)", "source": url})
    zl = ab.get("zoning")
    zl = ", ".join(zl) if isinstance(zl, list) else clean(zl)
    sf = num(b.get("totalSquareFeet")) or num(b.get("availableSquareFeet"))
    nrsf = units = None
    m = NRSF_RX.search(desc); nrsf = num(m.group(1)) if m else None
    m = UNITS_RX.search(desc); units = num(m.group(1)) if m else None
    return {"id": f"st-lo{d['_id'][-10:]}", "src": "LOIS (Kansas Commerce)", "url": url, "kind": raw["kind"], "name": name, "address": clean(loc.get("street")).split("\r")[0],
            "city": ck, "county": clean(loc.get("countyName")), "zip": clean(loc.get("zip")), "lat": coords[1], "lon": coords[0],
            "acres": num(b.get("acres")), "price": sale, "sf": sf, "nrsf": nrsf, "units": units, "subtype": "", "zoning_listed": zl,
            "description": desc[:1500], "contacts": [c for c in cs if c["name"] or c["email"] or c["phone"]], "updated": (d.get("dateModified") or "")[:10], "multi_loc": False, "occupancy": ""}

def dedupe(recs):
    out = []
    for r in sorted(recs, key=lambda r: (r["src"] != "Crexi", r["price"] is None)):
        dup = next((o for o in out if o["kind"] == r["kind"] and o["lat"] and r["lat"] and hav(o["lat"], o["lon"], r["lat"], r["lon"]) < 250
                    and (not o["acres"] or not r["acres"] or abs(o["acres"] - r["acres"]) / max(o["acres"], r["acres"]) < 0.25)), None)
        if dup:
            dup.setdefault("also_listed", []).append({"src": r["src"], "url": r["url"]})
            if not dup["price"] and r["price"]: dup["price"] = r["price"]
            dup["contacts"] += [c for c in r["contacts"] if c["name"] not in {x["name"] for x in dup["contacts"]}]
            continue
        out.append(r)
    return out

# --------------------------------------------------------------------------- context data
def zoning_lookup(city, lat, lon):
    g = CITIES[city]["zoning_gis"]
    if not g or lat is None: return None
    url, f1, f2 = g
    p = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
         "outFields": ",".join(x for x in (f1, f2) if x), "returnGeometry": "false", "f": "json"}
    d = get(f"{url}/query", params=p, timeout=40).json()
    fs = d.get("features") or []
    if "error" in d: raise RuntimeError(str(d["error"])[:120])
    if not fs: return {"district": "", "name": "", "src": url}
    a = fs[0]["attributes"]
    return {"district": norm_district(city, a.get(f1)), "name": clean(a.get(f2)) if f2 else "", "src": url}

def kdot(lat, lon):
    best = []
    for label, url in KDOT_LAYERS:
        p = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
             "distance": 250, "units": "esriSRUnit_Meter", "outFields": "RouteID,AADTCount,AADTCountYear", "returnGeometry": "true", "outSR": 4326, "f": "json"}
        d = get(f"{url}/query", params=p, timeout=45).json()
        if "error" in d: raise RuntimeError(str(d["error"])[:120])
        for f in d.get("features") or []:
            a = f["attributes"]; paths = (f.get("geometry") or {}).get("paths") or []
            dist = min((seg_dist(lat, lon, [(p[1], p[0]) for p in path]) for path in paths), default=9e9)
            if a.get("AADTCount"):
                best.append({"aadt": int(a["AADTCount"]), "year": a.get("AADTCountYear"), "route": a.get("RouteID"), "dist_m": round(dist), "layer": label})
    return best

def roads_for(bbox):
    s, w, n, e = bbox
    q = f'[out:json][timeout:180];way["highway"~"^(motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link|primary_link)$"]["name"]({s},{w},{n},{e});out geom tags;'
    els = overpass(q)
    return [{"name": e["tags"].get("name"), "ref": e["tags"].get("ref", ""), "hw": e["tags"]["highway"], "g": [(p["lat"], p["lon"]) for p in e.get("geometry", [])]} for e in els if e.get("geometry")]

def storage_osm(bbox):
    s, w, n, e = bbox
    q = f'[out:json][timeout:180];(nwr["shop"="storage_rental"]({s},{w},{n},{e});nwr["amenity"="storage_rental"]({s},{w},{n},{e});'
    # many facilities are mapped only as a named building/landuse; catch those by name too
    q += f'nwr["name"~"self[ -]?stor|mini[ -]?stor|storage (center|centre|units|depot|mart)|storagemart|cubesmart|extra space|public storage|u-?haul moving|life storage",i]({s},{w},{n},{e}););out geom tags;'
    out = []
    for el in overpass(q):
        t = el.get("tags", {})
        if el["type"] == "node": lat, lon, area, geom = el["lat"], el["lon"], 0, None
        else:
            geom = el.get("geometry") or [p for m in el.get("members", []) if m.get("role") == "outer" for p in (m.get("geometry") or [])]
            if not geom: continue
            lat = sum(p["lat"] for p in geom) / len(geom); lon = sum(p["lon"] for p in geom) / len(geom); area = poly_area_m2(geom) if el["type"] == "way" else 0
        sqft = area * 10.7639
        if area and "building" in t: est, basis = sqft * max(1, num(t.get("building:levels")) or 1) * 0.85, "building footprint × levels × 85%"
        elif area: est, basis = sqft * 0.40, "site polygon × 40% coverage"
        else: est, basis = None, ""
        out.append({"name": t.get("name") or t.get("brand") or "Self storage (unnamed in OSM)", "brand": t.get("brand", ""), "lat": lat, "lon": lon,
                    "est_nrsf": round(est, -2) if est else None, "basis": basis, "osm": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
                    "tagged": t.get("shop") == "storage_rental" or t.get("amenity") == "storage_rental"})
    return out

def cluster_storage(out):
    # one facility is often mapped as a site polygon + several tagged buildings + a node: cluster within 150 m.
    # NRSF = max(site-polygon estimate, sum of building estimates); name = first named member.
    keep = []
    for x in sorted(out, key=lambda x: -(x["est_nrsf"] or 0)):
        k = next((k for k in keep if hav(x["lat"], x["lon"], k["lat"], k["lon"]) < 150), None)
        if k is None:
            keep.append(dict(x, _b=(x["est_nrsf"] or 0) if "building" in x["basis"] else 0, _s=(x["est_nrsf"] or 0) if "site" in x["basis"] else 0, parts=1)); continue
        k["parts"] += 1
        if "building" in x["basis"]: k["_b"] += x["est_nrsf"] or 0
        elif "site" in x["basis"]: k["_s"] = max(k["_s"], x["est_nrsf"] or 0)
        if k["name"].startswith("Self storage (unnamed") and not x["name"].startswith("Self storage (unnamed"): k["name"] = x["name"]
    for k in keep:
        est = max(k.pop("_b"), k.pop("_s"))
        if est: k["est_nrsf"] = round(est, -2); k["basis"] = "buildings × levels × 85%" if "building" in k["basis"] or k["parts"] > 1 else k["basis"]
    return keep

def lakes_marinas(bbox):
    s, w, n, e = bbox
    q = (f'[out:json][timeout:180];(nwr["natural"="water"]["water"~"^(reservoir|lake)$"]["wikidata"]["name"]({s},{w},{n},{e});'
         f'nwr["leisure"~"^(marina|slipway)$"]({s},{w},{n},{e}););out center tags;')
    out = []
    for el in overpass(q):
        c = el.get("center") or {"lat": el.get("lat"), "lon": el.get("lon")}
        if c.get("lat") is None: continue
        t = el.get("tags", {})
        out.append({"name": t.get("name", ""), "kind": "marina" if t.get("leisure") else "lake", "lat": c["lat"], "lon": c["lon"], "osm": f"https://www.openstreetmap.org/{el['type']}/{el['id']}"})
    return out

def tracts_demand(states_counties):
    """tract centroid + ACS 2020-24 population / tenure / income for the given counties."""
    cent = {}
    for st in sorted({c[:2] for c in states_counties}):
        txt = get(GAZ.format(st=st), timeout=90).text
        for line in txt.splitlines()[1:]:
            p = line.split("\t")
            if len(p) >= 8 and p[1].strip()[:5] in states_counties:
                cent[p[1].strip()] = (float(p[6]), float(p[7]))
    acs = {}; release = ""
    for c in states_counties:
        d = get(CENSUS_REPORTER, params={"table_ids": "B01003,B25003,B19013", "geo_ids": f"140|05000US{c}"}, timeout=90).json()
        release = (d.get("release") or {}).get("name", release)
        for gid, t in (d.get("data") or {}).items():
            g = gid.split("US")[-1]
            try:
                acs[g] = {"pop": t["B01003"]["estimate"]["B01003001"] or 0, "hh": t["B25003"]["estimate"]["B25003001"] or 0,
                          "rent": t["B25003"]["estimate"]["B25003003"] or 0, "inc": t["B19013"]["estimate"]["B19013001"]}
            except (KeyError, TypeError): pass
    return [{"geoid": g, "lat": cent[g][0], "lon": cent[g][1], **acs[g]} for g in cent if g in acs], release

def city_growth(cache_dir):
    def f():
        txt = get(PEP_URL, timeout=120).text.splitlines(); hdr = txt[0].split(","); out = {}
        for line in txt[1:]:
            p = line.split(",")
            if len(p) < len(hdr) or p[1] != "20" or p[0] != "162": continue
            r = dict(zip(hdr, p)); out[r["PLACE"]] = {"name": r["NAME"], "p2020": int(r["POPESTIMATE2020"]), "p2024": int(r["POPESTIMATE2024"])}
        return out
    return cached(cache_dir, "pep_ks_places.json", f, 24 * 30)

def city_permits(cache_dir):
    def f():
        for yr in (dt.date.today().year - 1, dt.date.today().year - 2):
            try: txt = get(BPS.format(yr=yr), timeout=90).text
            except Exception: continue
            out = {}
            for line in txt.splitlines():
                p = line.split(",")
                if len(p) > 29 and p[0] == str(yr) and p[1] == "20":
                    pl = p[5].strip(); units = sum(int(p[i] or 0) for i in (18, 21, 24, 27))
                    out[pl] = {"year": yr, "units": units, "sf": int(p[18] or 0), "mf": int(p[24] or 0) + int(p[27] or 0), "months": p[15].strip()}
            if out: return {"year": yr, "url": BPS.format(yr=yr), "places": out}
        raise RuntimeError("no BPS file")
    return cached(cache_dir, "bps_ks.json", f, 24 * 7)

def geocode(addr):
    d = get(GEOCODER, params={"address": addr, "benchmark": "Public_AR_Current", "format": "json"}, timeout=40).json()
    m = (d.get("result") or {}).get("addressMatches") or []
    return (m[0]["coordinates"]["y"], m[0]["coordinates"]["x"]) if m else None

# --------------------------------------------------------------------------- scoring
def ramp(v, lo, hi):
    if v is None: return None
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))

WEIGHTS = {"demand": 30, "competition": 25, "visibility": 15, "zoning": 20, "price": 10}

X_CAP = 55             # land that needs a rezoning can't outrank sites where storage is allowed
SPC_BOUNDS = [4, 14]   # replaced at run time by the 20th/80th percentile of mapped SF/person across rows

def score(r):
    dm = r["demand"]; parts = {}
    sub = [(ramp(dm.get("pop3"), 15000, 90000), 11), (ramp(dm.get("growth_pct"), -2, 8), 6), (ramp(dm.get("income"), 45000, 100000), 5),
           (ramp(dm.get("renter_pct"), 25, 50), 3), (ramp(dm.get("permits_per_1k"), 0, 8), 3), (1.0 if (dm.get("lake_mi") or 99) <= 15 else 0.5 if (dm.get("lake_mi") or 99) <= 30 else 0.0, 2)]
    parts["demand"] = round(sum((v if v is not None else 0.4) * w for v, w in sub), 1)
    spc = r["competition"].get("sf_per_capita")
    parts["competition"] = round(WEIGHTS["competition"] * (1 - ramp(spc, *SPC_BOUNDS)) if spc is not None else WEIGHTS["competition"] * 0.4, 1)
    a = r["traffic"].get("aadt")
    parts["visibility"] = round(WEIGHTS["visibility"] * (ramp(math.log10(a), math.log10(2000), math.log10(30000)) if a else 0.2), 1)
    st = r["zoning"]["status"]
    if r["kind"] == "facility": z = {"P": 1, "S": 1, "I": 0.85, "C": 0.75, "X": 0.5, "?": 0.6}[st]
    else: z = {"P": 1, "S": 0.9, "I": 0.7, "C": 0.5, "X": 0.1, "?": 0.35}[st]
    parts["zoning"] = round(WEIGHTS["zoning"] * z, 1)
    if r["kind"] == "land":
        ppa = r.get("price_per_acre"); p = (1 - ramp(math.log10(ppa), math.log10(80000), math.log10(700000))) if ppa else 0.4
    else:
        psf = r.get("price_per_sf"); p = (1 - ramp(psf, 40, 160)) if psf else 0.4
    parts["price"] = round(WEIGHTS["price"] * p, 1)
    tot = round(sum(parts.values()))
    if r["kind"] == "land" and st == "X" and tot > X_CAP:
        r["score_cap"] = f"Capped at {X_CAP} (from {tot}): storage is not permitted in this district, so it needs a rezoning first"; tot = X_CAP
    return tot, parts

def format_fit(r):
    c, d, kind = r["city"], r["zoning"]["district"], r["kind"]; st = r["zoning"]["status"]; dm = r["demand"]; ac = r.get("acres") or 0
    ok_storage = st in ("P", "S", "I", "C")
    out = {}
    if kind == "facility":
        t = (r.get("name", "") + " " + r.get("description", "") + " " + r.get("subtype", "")).lower()
        out["drive_up"] = ("yes", "listing mentions drive-up") if re.search(r"drive[- ]up|roll[- ]up|exterior unit|non[- ]climate", t) else ("?", "not stated")
        out["climate"] = ("yes", "listing mentions climate control") if re.search(r"climate", t) else ("add?", "not stated — conversion/expansion option")
        out["multistory"] = ("yes", "listing mentions multi-story") if re.search(r"multi[- ]?stor(y|ies)|elevator|\b[23]-stor", t) else ("—", "")
        out["boat_rv"] = ("yes", "listing mentions RV/boat") if re.search(r"\brv\b|boat|vehicle storage|parking spaces", t) else (("add?", "zoning allows outdoor RV/boat") if fmt_allowed(c, d, "rv") else ("—", ""))
        out["contractor"] = ("yes", "garage/flex condos") if re.search(r"garage|contractor|flex|warehouse|dock", t) else ("—", "")
        return out
    du = fmt_allowed(c, d, "drive_up")
    out["drive_up"] = ("yes" if du == "P" and ac >= 3 else "CUP" if du == "C" else "no", "zoning allows exterior-access units" if du == "P" else "conditional" if du == "C" else "exterior/drive-up units not allowed in this district" if d else "zoning unknown")
    out["climate"] = ("yes" if ok_storage else "no", ("indoor self-storage " + STATUS_TXT[st].lower()) + ("; strong 3-mi income" if (dm.get("income") or 0) >= 70000 else ""))
    dense = (dm.get("pop3") or 0) >= 60000 or (r["traffic"].get("aadt") or 0) >= 20000 or (r.get("price_per_acre") or 0) >= 350000
    out["multistory"] = ("yes" if ok_storage and dense else "maybe" if ok_storage else "no", "dense trade area / high AADT / land cost favours vertical" if dense else "suburban densities favour single-story")
    rv = fmt_allowed(c, d, "rv")
    out["boat_rv"] = ("yes" if rv and ac >= 5 else "small" if rv else "no", (f"lake/marina {dm.get('lake_mi')} mi" if dm.get("lake_mi") else "") + ("" if rv else "; outdoor RV/boat storage not allowed in district"))
    co = fmt_allowed(c, d, "contractor")
    out["contractor"] = ("yes" if co and ac >= 4 else "no", "contractor/flex garages allowed" if co else "contractor yards/garages need industrial zoning")
    return out

# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--state", help="plaintext last-good listing state (CI decrypts/encrypts it)")
    ap.add_argument("--cache-dir", default=None, help="cache for public area-wide geodata (OSM, Census) — no listing data")
    ap.add_argument("--point-cache-dir", default=None, help="cache for per-listing-point lookups (zoning, AADT); keep out of public caches")
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    state = json.load(open(args.state)) if args.state and os.path.exists(args.state) else {}
    OWN_FACILITY.update(state.get("own_facility") or {})
    reuse = os.environ.get("STORAGE_REUSE_STATE_H")   # dev aid: reuse a state pull younger than N hours
    if reuse and state.get("raw") and (now - dt.datetime.fromisoformat(state["raw_at"])).total_seconds() < float(reuse) * 3600:
        raw = state["raw"]; log("reusing listing pull from", state["raw_at"])
    else:
      try:
        raw, crexi_ok, lois_ok = pull_listings(args)
        old = state.get("raw") or []
        # keep last-good rows for a source that failed this run (up to 10 days old)
        if not crexi_ok: raw += [r for r in old if r["src"] == "crexi"]
        if not lois_ok: raw += [r for r in old if r["src"] == "lois"]
        state["raw"] = raw; state["raw_at"] = now.isoformat()
        if args.state: json.dump(state, open(args.state, "w"))   # save early: later steps are slow
      except Exception as e:
        log("ERROR listings:", e)
        raw = state.get("raw") or []
        STATUS["Listings"] = {"ok": False, "detail": f"live pull failed ({str(e)[:100]}); using last good pull from {state.get('raw_at','?')[:10]}"}
    recs = [x for x in (normalise(r) for r in raw) if x]
    recs = [r for r in recs if r["lat"] and r["lon"]]
    # land: keep 4-12 ac; facilities: drop single condo units < 5,000 SF (kept as format comps)
    comps = [r for r in recs if r["kind"] == "facility" and (r["sf"] or 0) < 5000 and not r["units"] and not r["nrsf"]]
    recs = [r for r in recs if (r["kind"] == "land" and r["acres"] and LAND_MIN <= r["acres"] <= LAND_MAX) or (r["kind"] == "facility" and r not in comps)]
    recs = dedupe(recs)
    log(f"{len(recs)} rows ({sum(r['kind']=='land' for r in recs)} land, {sum(r['kind']=='facility' for r in recs)} facilities), {len(comps)} condo comps")

    cd = args.cache_dir
    # ---- public context layers
    metro = {"Wichita": CITIES["Wichita"]["bbox"], "KC": (38.78, -94.98, 39.21, -94.58), "Topeka": CITIES["Topeka"]["bbox"], "Lawrence": CITIES["Lawrence"]["bbox"]}
    metro_of = {"Wichita": "Wichita", "Overland Park": "KC", "Kansas City": "KC", "Olathe": "KC", "Topeka": "Topeka", "Lawrence": "Lawrence"}
    buf = lambda b, d: (b[0] - d, b[1] - d * 1.3, b[2] + d, b[3] + d * 1.3)
    roads, comp, lakes = {}, {}, {}
    for m, bb in metro.items():
        try: roads[m] = cached(cd, f"st_roads_{m}.json", lambda: roads_for(buf(bb, 0.01)), 24 * 14)
        except Exception as e: roads[m] = []; log("roads", m, e)
        try: comp[m] = cluster_storage(cached(cd, f"st_storage3_{m}.json", lambda: storage_osm(buf(bb, 0.05)), 24 * 3))
        except Exception as e: comp[m] = None; log("osm storage", m, e)
        try: lakes[m] = cached(cd, f"st_lakes_{m}.json", lambda: lakes_marinas(buf(bb, 0.45)), 24 * 30)
        except Exception as e: lakes[m] = []; log("lakes", m, e)
        time.sleep(2)
    STATUS["OpenStreetMap self-storage (shop=storage_rental)"] = {"ok": all(v is not None for v in comp.values()), "detail": f"{sum(len(v or []) for v in comp.values())} facilities mapped across the 4 metros (OSM coverage is incomplete)", "url": "https://wiki.openstreetmap.org/wiki/Tag:shop%3Dstorage_rental"}
    own = None
    pcd = args.point_cache_dir or cd
    try:
        g = (OWN_FACILITY.get("lat"), OWN_FACILITY.get("lon")) if OWN_FACILITY.get("lat") else (geocode(OWN_FACILITY["address"]) if OWN_FACILITY.get("address") else None)
        if g: own = {"lat": g[0], "lon": g[1]}
    except Exception as e: log("geocode own", e)
    if own and comp.get("Topeka") is not None:
        hit = next((c for c in comp["Topeka"] if hav(c["lat"], c["lon"], own["lat"], own["lon"]) < 200), None)
        if hit: hit["own"] = True; hit["name"] = OWN_FACILITY["name"]
        else: comp["Topeka"].append({"name": OWN_FACILITY["name"], "lat": own["lat"], "lon": own["lon"], "est_nrsf": None, "basis": "", "own": True, "osm": "https://geocoding.geo.census.gov/"})
    counties = sorted({c for v in CITIES.values() for c in v["counties"]})
    try:
        tracts, acs_rel = cached(cd, "st_tracts.json", lambda: tracts_demand(counties), 24 * 30)
        STATUS["Census ACS 5-yr (Census Reporter)"] = {"ok": True, "detail": f"{acs_rel}: {len(tracts)} tracts in {len(counties)} counties (3-mi sums by tract centroid)", "url": "https://censusreporter.org/"}
    except Exception as e:
        tracts, acs_rel = [], ""; STATUS["Census ACS 5-yr (Census Reporter)"] = {"ok": False, "detail": str(e)[:150], "url": "https://censusreporter.org/"}
    try: pep = city_growth(cd); STATUS["Census PEP city estimates 2020-2024"] = {"ok": True, "detail": "city population growth", "url": PEP_URL}
    except Exception as e: pep = {}; STATUS["Census PEP city estimates 2020-2024"] = {"ok": False, "detail": str(e)[:150], "url": PEP_URL}
    try: bps = city_permits(cd); STATUS["Census Building Permits Survey"] = {"ok": True, "detail": f"{bps['year']} annual, city level", "url": bps["url"]}
    except Exception as e: bps = {"places": {}}; STATUS["Census Building Permits Survey"] = {"ok": False, "detail": str(e)[:150], "url": BPS.format(yr="YYYY")}

    city_info = {}
    for c, cfg in CITIES.items():
        p = pep.get(cfg["place"]); b = bps.get("places", {}).get(cfg["place"])
        gr = round((p["p2024"] / p["p2020"] - 1) * 100, 2) if p else None
        city_info[c] = {"label": CITY_LABEL.get(c, c), "pop2024": p and p["p2024"], "pop2020": p and p["p2020"], "growth_pct": gr,
                        "permits": b and b["units"], "permits_year": b and b["year"], "permits_sf": b and b["sf"], "permits_mf": b and b["mf"],
                        "permits_per_1k": round(b["units"] / p["p2024"] * 1000, 2) if (b and p) else None,
                        "contacts": cfg["contacts"], "zoning_code": ZONING[c]["code"], "zoning_url": ZONING[c]["url"],
                        "zoning_gis": cfg["zoning_gis"][0] if cfg["zoning_gis"] else None}

    kd_ok = kd_fail = z_ok = z_fail = 0
    for r in recs:
        c = r["city"]; m = metro_of[c]; lat, lon = r["lat"], r["lon"]; srcs = [{"label": f"Listing ({r['src']})", "url": r["url"]}]
        for x in r.get("also_listed", []): srcs.append({"label": f"Also listed ({x['src']})", "url": x["url"]})
        # zoning
        zg = None
        try: zg = cached(pcd, f"st_z_{c}_{lat:.5f}_{lon:.5f}.json".replace(" ", "_"), lambda: zoning_lookup(c, lat, lon), 24 * 14); z_ok += 1 if zg else 0
        except Exception as e: z_fail += 1; log("zoning", r["id"], e)
        dist = (zg or {}).get("district") or ""; zsrc = "city zoning GIS" if dist else ""
        if not dist:
            dist = zoning_from_text(c, r["zoning_listed"]) or zoning_from_text(c, r["description"]); zsrc = "listing (stated zoning — verify)" if dist else ""
        st, note = classify(c, dist)
        r["zoning"] = {"district": dist, "district_name": (zg or {}).get("name", ""), "listed": r["zoning_listed"], "basis": zsrc, "status": st, "status_txt": STATUS_TXT[st],
                       "note": note, "code": ZONING[c]["code"], "code_url": ZONING[c]["url"]}
        if zg: srcs.append({"label": "Zoning GIS", "url": zg["src"]})
        srcs.append({"label": "Zoning code", "url": ZONING[c]["url"]})
        # traffic
        try:
            segs = cached(pcd, f"st_aadt_{lat:.5f}_{lon:.5f}.json", lambda: kdot(lat, lon), 24 * 30); kd_ok += 1
        except Exception as e: segs = None; kd_fail += 1; log("kdot", r["id"], e)
        near_rd = None
        for rd in roads.get(m, []):
            dd = seg_dist(lat, lon, rd["g"])
            if near_rd is None or dd < near_rd[0]: near_rd = (dd, rd)
        tr = {"road": None, "road_m": None, "aadt": None, "aadt_year": None, "aadt_route": None, "aadt_m": None, "max_aadt_250m": None}
        if near_rd and near_rd[0] < 400:
            tr["road"] = near_rd[1]["name"] + (f" ({near_rd[1]['ref']})" if near_rd[1]["ref"] and near_rd[1]["ref"] not in near_rd[1]["name"] else ""); tr["road_m"] = round(near_rd[0])
        if segs:
            n = min(segs, key=lambda s: s["dist_m"]); mx = max(segs, key=lambda s: s["aadt"])
            pick = n if n["dist_m"] <= 120 or n["aadt"] >= mx["aadt"] * 0.6 else mx
            tr.update(aadt=pick["aadt"], aadt_year=pick["year"], aadt_route=pick["route"], aadt_m=pick["dist_m"], max_aadt_250m=mx["aadt"])
        r["traffic"] = tr
        srcs.append({"label": "KDOT AADT", "url": KDOT_PAGE})
        # competition
        cl = comp.get(m)
        if cl is not None:
            near = sorted(((hav(lat, lon, x["lat"], x["lon"]) / MI, x) for x in cl), key=lambda t: t[0])
            near = [(d_, x) for d_, x in near if not (r["kind"] == "facility" and d_ < 0.08)]
            within = [(d_, x) for d_, x in near if d_ <= 3]
            known = [x["est_nrsf"] for x in cl if x.get("est_nrsf")]
            med = sorted(known)[len(known) // 2] if known else 50000
            est = sum(x["est_nrsf"] or med for _, x in within)
            r["competition"] = {"count": len(within), "est_nrsf": round(est, -3), "nearest": near[0][1]["name"] if near else None, "nearest_mi": round(near[0][0], 2) if near else None,
                                "nearest_osm": near[0][1]["osm"] if near else None, "median_fill": round(med, -2), "own_within": any(x.get("own") for _, x in within),
                                "list": [{"name": x["name"], "mi": round(d_, 2), "nrsf": x["est_nrsf"], "own": bool(x.get("own"))} for d_, x in within[:12]]}
        else: r["competition"] = {"count": None}
        srcs.append({"label": "OSM self-storage", "url": "https://overpass-turbo.eu/?Q=nwr%5Bshop%3Dstorage_rental%5D"})
        # demand
        tt = [t for t in tracts if hav(lat, lon, t["lat"], t["lon"]) <= 3 * MI]
        pop3 = sum(t["pop"] for t in tt); hh = sum(t["hh"] for t in tt); rent = sum(t["rent"] for t in tt)
        incw = [(t["inc"], t["hh"]) for t in tt if t["inc"] and t["hh"]]
        lk = sorted(((hav(lat, lon, x["lat"], x["lon"]) / MI, x) for x in lakes.get(m, []) if x["kind"] == "lake"), key=lambda t: t[0])
        mar = [x for x in lakes.get(m, []) if x["kind"] == "marina" and hav(lat, lon, x["lat"], x["lon"]) <= 25 * MI]
        ci = city_info[c]
        r["demand"] = {"pop3": pop3 or None, "tracts": len(tt), "hh3": hh or None, "renter_pct": round(rent / hh * 100, 1) if hh else None,
                       "income": round(sum(a * b for a, b in incw) / sum(b for _, b in incw), -2) if incw else None,
                       "growth_pct": ci["growth_pct"], "permits": ci["permits"], "permits_year": ci["permits_year"], "permits_per_1k": ci["permits_per_1k"],
                       "lake": lk[0][1]["name"] if lk else None, "lake_mi": round(lk[0][0], 1) if lk else None, "marinas_25mi": len(mar)}
        if r["competition"].get("count") is not None and pop3:
            r["competition"]["sf_per_capita"] = round(r["competition"]["est_nrsf"] / pop3, 1)
        srcs += [{"label": "Census ACS 2020-24 (tracts)", "url": "https://censusreporter.org/"}, {"label": "Census PEP 2024", "url": PEP_URL},
                 {"label": "Census BPS permits", "url": bps.get("url", BPS.format(yr="2025"))}]
        # price
        if r["kind"] == "land":
            r["price_per_acre"] = round(r["price"] / r["acres"]) if r["price"] and r["acres"] else None
            r["fit_flag"] = None if FIT_MIN <= r["acres"] <= FIT_MAX else (f"{r['acres']:g} ac — below the 5-10 ac target" if r["acres"] < FIT_MIN else f"{r['acres']:g} ac — above the 5-10 ac target")
        else:
            base = r["nrsf"] or r["sf"]
            r["price_per_sf"] = round(r["price"] / base, 1) if r["price"] and base else None
            r["fit_flag"] = "Portfolio / multiple properties" if r["multi_loc"] else None
        r["formats"] = format_fit(r)
        r["contacts"] = r["contacts"] + [dict(x) for x in CITIES[c]["contacts"]]
        r["sources"] = srcs
        r["city_label"] = CITY_LABEL.get(c, c)
        r.pop("description_full", None)
        r["description"] = r["description"][:600]
    spcs = sorted(r["competition"]["sf_per_capita"] for r in recs if r["competition"].get("sf_per_capita") is not None)
    if len(spcs) >= 10:
        SPC_BOUNDS[:] = [spcs[len(spcs) // 5], max(spcs[len(spcs) * 4 // 5], spcs[len(spcs) // 5] + 0.5)]
    log("competition SF/person bounds", SPC_BOUNDS)
    for r in recs: r["score"], r["score_parts"] = score(r)
    STATUS["KDOT AADT (state + non-state)"] = {"ok": kd_fail == 0, "detail": f"{kd_ok} rows looked up, {kd_fail} failed; 2025 counts", "url": KDOT_PAGE}
    STATUS["City zoning GIS (5 cities; Olathe = listing text)"] = {"ok": z_fail == 0, "detail": f"{z_ok} point lookups, {z_fail} failed", "url": ""}
    recs.sort(key=lambda r: -r["score"])
    prev = state.get("seen", {}); today = now.date().isoformat()
    for r in recs:
        r["first_seen"] = prev.get(r["id"], today); prev[r["id"]] = r["first_seen"]
        r["is_new"] = r["first_seen"] >= (now.date() - dt.timedelta(days=7)).isoformat()
    state["seen"] = prev
    out = {"generated_at": now.isoformat(), "rows": recs, "cities": city_info,
           "comps": [{"name": x["name"], "city": CITY_LABEL.get(x["city"], x["city"]), "price": x["price"], "sf": x["sf"], "url": x["url"], "address": x["address"]} for x in comps],
           "status": STATUS, "weights": WEIGHTS, "zoning_rules": {k: {kk: v[kk] for kk in v} for k, v in ZONING.items()},
           "own_facility": OWN_FACILITY | (own or {}), "acs_release": acs_rel,
           "method": {"demand": "3-mi trade area = Census tracts whose centroid is within 3 mi (ACS 2020-24 5-yr). Population 11 pts (15k→0, 90k→full), city growth 2020-24 6 (−2%→0, +8%→full), HH-weighted tract median income 5 ($45k→$100k), renter share 3 (25%→50%), city permits per 1k residents 3 (0→8), lake within 15 mi 2.",
                      "competition": f"Self-storage mapped in OpenStreetMap within 3 mi (a site polygon, its buildings and node within 150 m count as one facility). NRSF estimated from the mapped footprint (buildings × levels × 85%, or site polygon × 40%); facilities mapped as points get the metro median. OSM maps only part of the real supply (US average is about 7-8 SF/person; mapped KS supply is far lower), so the score is RELATIVE: 25 pts at or below the 20th percentile of rows ({SPC_BOUNDS[0]:g} SF/person), 0 at or above the 80th ({SPC_BOUNDS[1]:g}). Check rent comps and a supply study before an offer.",
                      "visibility": "KDOT AADT on the frontage segment (≤250 m); log scale 2,000→0, 30,000→15 pts.",
                      "zoning": "Land: by right 20, by right w/ standards 18, interpretation 14, conditional/SUP 10, unknown 7, not permitted 2. Existing facilities: zoning risk only (non-conforming = expansion risk). Land that needs a rezoning is capped at 55 overall.",
                      "price": "Land $/acre log scale $80k→10 pts, $700k→0. Facilities $/SF $40→10, $160→0. Unpriced = 4."}}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(out, open(args.out, "w"), indent=1, default=str)
    if args.state:
        json.dump(state, open(args.state, "w"))
    by = {}
    for r in recs: by.setdefault((r["city"], r["kind"]), 0); by[(r["city"], r["kind"])] += 1
    log("rows by city/type:", by)

if __name__ == "__main__":
    main()
