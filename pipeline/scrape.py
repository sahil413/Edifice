#!/usr/bin/env python3
"""Edifice / Kansas data-center land pipeline.

Regenerates <out>/sites.json from public sources, then (render.py) the page.
Runs unattended on a plain server: every source is plain HTTP/JSON, no browser.

Sources
  1. Kansas Commerce Certified Sites list (HTML)      kansascommerce.gov
  2. LOIS / LocationOne public JSON API               api.locationone.com
       - Kansas Dept of Commerce org (statewide)       59eaba35bec80e09b4bbf1d7
       - Evergy Economic Development org               59eaba35bec80e09b4bbf368
       - Greater Wichita Partnership org               59eaba35bec80e09b4bbefdb
  3. HIFLD Electric Power Transmission Lines          services1.arcgis.com (ArcGIS REST)
  4. EIA Natural Gas Interstate & Intrastate Pipelines services2.arcgis.com (ArcGIS REST)
  5. OpenStreetMap substations via Overpass API       overpass-api.de (fallback: HIFLD line end-points)
  6. Curated local economic-development contacts      private contacts file (encrypted in repo)
"""
import argparse, datetime as dt, hashlib, html, json, math, os, re, sys, time, urllib.parse
from collections import defaultdict
import requests
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point
from shapely.strtree import STRtree
from pricing import price_info  # asking price / $ per acre (listing-stated only)
import floodplain              # FEMA NFHL 100-yr floodplain share (penalty)

UA = "Mozilla/5.0 (compatible; EdificeSitesBot/1.0; +https://edifice.vc)"
CERT_LIST_URL = "https://www.kansascommerce.gov/program/business-incentives-and-services/certified-sites-program/sites/"
LOIS_API = "https://api.locationone.com"
LOIS_ORGS = {
    "59eaba35bec80e09b4bbf1d7": ("Kansas Commerce LOIS", "https://www.kansascommerce.gov/businesses/sites/"),
    "59eaba35bec80e09b4bbf368": ("Evergy LOIS", "https://www.evergyed.com/real-estate/available-buildings-and-sites/"),
    "59eaba35bec80e09b4bbefdb": ("Greater Wichita Partnership LOIS", "https://greaterwichitapartnership.org/locate-expand/available-properties"),
}
KS_ORG = "59eaba35bec80e09b4bbf1d7"
HIFLD_TX = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0"
EIA_GAS = "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
KS_BBOX = (-102.6, 36.4, -94.0, 40.6)  # Kansas + ~30 mi buffer
MIN_ACRES = 100.0
SUSPECT_ACRES = 20000.0  # listings above this are almost certainly data-entry errors
M_PER_MI = 1609.344

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
STATUS = {}  # source -> {"ok": bool, "detail": str}

def log(*a):
    print("[scrape]", *a, file=sys.stderr, flush=True)

def get(url, *, params=None, data=None, timeout=60, tries=4, accept="*/*"):
    last = None
    for i in range(tries):
        try:
            if data is not None:
                r = S.post(url, data=data, timeout=timeout, headers={"Accept": accept})
            else:
                r = S.get(url, params=params, timeout=timeout, headers={"Accept": accept})
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:  # network error
            last = repr(e)[:200]
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")

def cached(cache_dir, name, fn, max_age_h=20):
    """Cache expensive geodata pulls for a day (CI starts clean each run)."""
    max_age_h = float(os.environ.get("EDKS_GEO_MAX_AGE_H") or max_age_h)   # the enrich job reuses the daily cache
    if cache_dir:
        p = os.path.join(cache_dir, name)
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
            return json.load(open(p))
    try:
        v = fn()
    except Exception:
        # stale-cache fallback: better yesterday's public geodata than none
        p = os.path.join(cache_dir, name) if cache_dir else None
        if p and os.path.exists(p):
            print(f"[scrape] WARN {name}: live fetch failed, using cache from {time.ctime(os.path.getmtime(p))}", file=sys.stderr)
            return json.load(open(p))
        raise
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        json.dump(v, open(os.path.join(cache_dir, name), "w"))
    return v

# ---------------------------------------------------------------- sources
def fetch_certified_list():
    r = get(CERT_LIST_URL, accept="text/html,application/xhtml+xml")
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for card in soup.select("div.card-block"):
        h4 = card.find("h4"); p = card.find("p"); a = card.find("a", class_="plain_btn") or card.find("a")
        if not (h4 and p and a):
            continue
        lines = [x.strip() for x in p.get_text("\n").split("\n") if x.strip()]
        href = html.unescape(a.get("href", ""))
        if "safelinks.protection.outlook.com" in href:  # unwrap Outlook safelink
            href = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("url", [href])[0]
        m = re.search(r"/sites/([0-9a-f]{24})", href)
        acres = None
        if lines:
            mm = re.search(r"([\d.,]+)\s*Acres", lines[0], re.I)
            acres = float(mm.group(1).replace(",", "")) if mm else None
        out.append({"city": h4.get_text(strip=True), "acres_listed": acres,
                    "name": lines[1] if len(lines) > 1 else lines[0] if lines else "",
                    "url": href, "lois_id": m.group(1) if m else None})
    if not out:
        raise RuntimeError("no certified-site cards parsed (page layout changed?)")
    return out

def fetch_lois_org(org):
    r = get(f"{LOIS_API}/sites/{org}", params={"max_results": 5000})
    return r.json().get("_items", [])

def fetch_lois_site(sid):
    return get(f"{LOIS_API}/site/{sid}", timeout=45).json()

def arcgis_all(layer, fields):
    feats, off = [], 0
    while True:
        params = {"where": "1=1", "geometry": ",".join(map(str, KS_BBOX)), "geometryType": "esriGeometryEnvelope",
                  "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outFields": fields, "outSR": 4326,
                  "returnGeometry": "true", "f": "json", "resultOffset": off, "resultRecordCount": 1000,
                  "maxAllowableOffset": 0.0005}
        d = get(f"{layer}/query", params=params, timeout=120).json()
        if "error" in d:
            raise RuntimeError(str(d["error"])[:200])
        fs = d.get("features", [])
        feats += [{"a": f["attributes"], "paths": f["geometry"].get("paths", [])} for f in fs if f.get("geometry")]
        if not fs or not d.get("exceededTransferLimit"):
            break
        off += len(fs)
    return feats

def fetch_osm_substations():
    q = ('[out:json][timeout:120];area["ISO3166-2"="US-KS"]->.a;'
         '(node["power"="substation"](area.a);way["power"="substation"](area.a);relation["power"="substation"](area.a););out center tags;')
    last = None
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": q}, timeout=240, headers={"User-Agent": "EdificeSitesBot/1.0 (+https://edifice.vc)", "Accept": "application/json"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            els = r.json()["elements"]
            out = []
            for e in els:
                lat = e.get("lat") or (e.get("center") or {}).get("lat"); lon = e.get("lon") or (e.get("center") or {}).get("lon")
                if lat is None:
                    continue
                t = e.get("tags", {})
                out.append({"lat": lat, "lon": lon, "name": t.get("name") or t.get("ref") or "", "operator": t.get("operator", ""),
                            "kv": parse_kv(t.get("voltage", "")), "kind": t.get("substation", ""),
                            "osm": f"https://www.openstreetmap.org/{e['type']}/{e['id']}"})
            return out
        except Exception as ex:
            last = f"{url}: {repr(ex)[:120]}"; time.sleep(10)
    raise RuntimeError(f"Overpass failed: {last}")

def parse_kv(v):
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(v))]
    nums = [n / 1000 if n > 2000 else n for n in nums]
    return max(nums) if nums else None

def line_kv(a):
    v = a.get("VOLTAGE")
    if v is not None and v > 0:
        return float(v)
    c = str(a.get("VOLT_CLASS") or "")
    m = re.match(r"(\d+)", c)
    if "UNDER 100" in c.upper():
        return 69.0  # HIFLD 'UNDER 100' is mostly 69 kV in KS; treated conservatively
    return float(m.group(1)) if m else None

# ---------------------------------------------------------------- geometry
TO_M = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)  # CONUS Albers, metres

def proj_paths(paths):
    parts = []
    for p in paths:
        if len(p) >= 2:
            xs, ys = TO_M.transform([q[0] for q in p], [q[1] for q in p])
            parts.append(list(zip(xs, ys)))
    return MultiLineString(parts) if len(parts) > 1 else LineString(parts[0]) if parts else None

class Nearest:
    def __init__(self, geoms, meta):
        self.geoms, self.meta = geoms, meta
        self.tree = STRtree(geoms) if geoms else None
    def query(self, pt):
        if not self.tree:
            return None, None
        i = self.tree.nearest(pt)
        return self.geoms[i].distance(pt) / M_PER_MI, self.meta[i]

# ---------------------------------------------------------------- helpers
def num(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return v if v > 0 else None
    except Exception:
        return None

def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()

def lois_url(sid, name=""):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    return f"https://app.locationone.com/sites/{sid}/{slug}?organization={KS_ORG}"

def norm_name(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())

def norm_phone(p):
    d = re.sub(r"\D", "", str(p or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else clean(p)

def contact(name, title, org, email, phone, source, role):
    c = {"name": clean(name), "title": clean(title), "org": clean(org), "email": clean(email).lower() if email else "",
         "phone": norm_phone(phone) if phone else "", "source": source, "role": role}
    return c if (c["name"] or c["email"] or c["phone"]) else None

ACRES_TEXT_RX = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(?:\+\s*)?(?:-\s*)?(?:acres?|ac\b)", re.I)


def meets_min_acres(rec, d, cert_entry):
    """Enforce the 100+ acre rule for every source, certified sites included.
    Listed acreage >= MIN_ACRES (and not suspect) passes. A suspect (implausibly large) or missing
    acreage passes only if an independent figure -- the certified-sites list or an acreage stated in
    the listing text -- is >= MIN_ACRES; its flags are kept. Returns (keep, note)."""
    a = rec.get("acres")
    if a and not rec.get("acres_suspect"):
        return a >= MIN_ACRES, None
    other = []
    if cert_entry and cert_entry.get("acres_listed"):
        other.append((cert_entry["acres_listed"], "Kansas Commerce certified-sites list"))
    text = " ".join(clean(x) for x in (rec.get("name"), ((d.get("additionalDetails") or {}).get("about") or {}).get("description")) if x)
    for m in ACRES_TEXT_RX.finditer(text):
        try:
            other.append((float(m.group(1).replace(",", "")), "listing text"))
        except ValueError:
            pass
    other = [(v, s) for v, s in other if v < SUSPECT_ACRES]
    if other:
        v, s = max(other)
        if v >= MIN_ACRES:
            return True, f"{'Listed acreage looks wrong' if a else 'No acreage field'}; {s} gives {v:,.0f} acres"
        return False, f"{s} gives {v:,.0f} acres"
    if a:  # suspect value: read as square feet, as these errors usually are
        return a / 43560 >= MIN_ACRES, f"{a:,.0f} read as sq ft = {a/43560:,.1f} acres"
    return False, "no acreage listed"


def site_record(d, src_names, cert_entry):
    b = d.get("basicInfo", {}); loc = b.get("location", {}); u = d.get("utilities", {}) or {}
    tr = d.get("transportation", {}) or {}; ab = (d.get("additionalDetails") or {}).get("about", {}) or {}
    coords = (loc.get("geoLocation") or {}).get("coordinates") or [None, None]
    sid = d["_id"]; name = clean(b.get("name") or d.get("name"))
    url = cert_entry["url"] if cert_entry else lois_url(sid, name)
    elec = [e for e in (u.get("electric") or []) if isinstance(e, dict)]
    e0 = elec[0] if elec else {}
    gas = u.get("naturalGas") or {}; wat = u.get("water") or {}; sew = u.get("wasteWater") or {}; tel = u.get("telecommunications") or {}
    hw = tr.get("highways") or {}; rail = tr.get("rail") or {}
    acres = num(b.get("acres"))
    rec = {
        "id": sid, "name": name, "url": url, "lois_url": lois_url(sid, name), "sources": sorted(src_names),
        "certified": bool(cert_entry), "certified_listing": cert_entry,
        "city": clean(loc.get("city")).title() if clean(loc.get("city")).isupper() else clean(loc.get("city")),
        "county": clean(loc.get("countyName")), "address": clean(loc.get("street")), "zip": clean(loc.get("zip")),
        "lat": coords[1], "lon": coords[0], "acres": acres,
        "acres_suspect": bool(acres and acres > SUSPECT_ACRES),
        "price": dict(price_info(b.get("pricing"), acres, clean(ab.get("description")), bool(acres and acres > SUSPECT_ACRES)),
                      raw={"sale": num((b.get("pricing") or {}).get("salePrice")), "note": clean((b.get("pricing") or {}).get("pricingNote"))}),
        "zoning": ", ".join(ab.get("zoning") or []) if isinstance(ab.get("zoning"), list) else clean(ab.get("zoning")),
        "setting": clean(ab.get("setting")), "features": ab.get("specialtyFeatures") or [],
        "description": clean(ab.get("description"))[:1200],
        "electric": [{"provider": clean(e.get("supplierName")), "phone": norm_phone(e.get("phone")) if e.get("phone") else "",
                      "contact": clean(e.get("contactName")), "substation_mi": num(e.get("distanceToNearestSubstation")),
                      "served_kv": num(e.get("servedByKvLine")), "capability": clean(e.get("descriptionOfCapability"))[:800]} for e in elec],
        "gas": {"provider": clean(gas.get("supplierName")), "line_in": num(gas.get("sizeOfLine")), "main_in": num(gas.get("sizeOfMain")),
                "psi": num(gas.get("pressure")), "capability": clean(gas.get("descriptionOfCapability"))[:600]},
        "water": {"provider": clean(wat.get("supplierName")), "line_in": num(wat.get("line")), "main_in": num(wat.get("main")),
                  "peak_mgd": num(wat.get("peakCapacity")), "capability": clean(wat.get("descriptionOfCapability"))[:600]},
        "sewer": {"provider": clean(sew.get("supplierName")), "line_in": num(sew.get("sewerLine")), "main_in": num(sew.get("sewerMain"))},
        "telecom": {"provider": clean(tel.get("supplierName")), "capability": clean(tel.get("descriptionOfCapability"))[:600],
                    "redundant": bool(tel.get("serviceSpeedAndRedundancy"))},
        "highway": {"interstate": clean(hw.get("nearestInterstate")), "interstate_mi": num(hw.get("nearestInterstateMiles")),
                    "highway": clean(hw.get("nearestHighway")), "highway_mi": num(hw.get("nearestHighwayMiles"))},
        "rail": {"served": bool(rail.get("railServed")), "railroads": [r for r in (rail.get("railroadServicedBy") or []) if r and r != "Other"],
                 "shortline": clean((rail.get("shortLine") or {}).get("lineName"))},
        "lois_created": d.get("dateCreated"), "lois_modified": d.get("dateModified"),
        "percent_complete": d.get("percentComplete"),
    }
    # listing contacts (as published on the LOIS listing)
    cs = []
    for c in ((d.get("contacts") or {}).get("economicDevelopmentContacts") or []):
        cs.append(contact(c.get("name"), c.get("title"), c.get("organization"), c.get("email"), c.get("phone"), rec["lois_url"], "Listing contact (economic development)"))
    for k in ("broker", "broker2"):
        c = (d.get("contacts") or {}).get(k) or {}
        org = c.get("contactName2") or c.get("company") or ""
        cs.append(contact(c.get("contactName"), "", org, c.get("email"), c.get("phone"), rec["lois_url"], "Listing contact (owner/broker)"))
    rec["contacts"] = [c for c in cs if c]
    return rec

# ---------------------------------------------------------------- scoring
def kv_weight(kv):
    if kv is None: return 0.25
    if kv >= 345: return 1.0
    if kv >= 230: return 0.85
    if kv >= 138: return 0.7
    if kv >= 115: return 0.6
    if kv >= 69: return 0.4
    return 0.2

def ramp(d, full, zero):
    if d is None: return 0.0
    if d <= full: return 1.0
    if d >= zero: return 0.0
    return (zero - d) / (zero - full)

SCORING = {
    "transmission": {"max": 30, "rule": "30 × voltage weight × distance factor, best line within 10 mi. Weight: 345 kV+ 1.0, 230 kV 0.85, 138-161 kV 0.7, 115 kV 0.6, 69 kV 0.4. Distance factor: 1.0 at ≤1 mi, falling linearly to 0 at 10 mi."},
    "substation": {"max": 15, "rule": "15 × voltage weight × distance factor, best substation. Same weights; factor 1.0 at ≤2 mi, 0 at 15 mi. OSM voltage tag (unknown voltage = weight 0.25)."},
    "gas": {"max": 20, "rule": "Max of: EIA interstate/intrastate pipeline (20 at ≤1 mi, linear to 0 at 15 mi) and listed on-site gas service (6\"+ line 12, 4\"+ 9, any listed gas provider 5)."},
    "scale": {"max": 15, "rule": "Acres: 500+ 15, 250+ 12, 150+ 9, 100+ 6, 50+ 3, else 1. Acreage over 20,000 is treated as a data-entry error and scored 6."},
    "fiber": {"max": 10, "rule": "Telecom provider listed 5, fiber mentioned in listing +3, redundancy/speed flag +2."},
    "water": {"max": 10, "rule": "Water provider listed 5, water line/main ≥8\" +2, sewer provider listed 3."},
    "floodplain": {"max": 0, "penalty": True, "rule": floodplain.RULE},
}

def score(rec):
    p = rec["power"]; g = rec["gasline"]; parts = {}
    # transmission: best over candidate classes
    best_t, why_t = 0.0, "No HIFLD transmission line within 10 mi"
    for c in p.get("lines_by_class", []):
        v = 30 * kv_weight(c["kv"]) * ramp(c["mi"], 1, 10)
        if v > best_t:
            best_t, why_t = v, f"{c['kv']:.0f} kV line {c['mi']:.1f} mi away" + (f" ({c['owner']})" if c.get("owner") else "")
    parts["transmission"] = (best_t, why_t)
    best_s, why_s = 0.0, "No substation found nearby"
    for c in p.get("subs_by_class", []):
        v = 15 * kv_weight(c["kv"]) * ramp(c["mi"], 2, 15)
        if v > best_s:
            best_s, why_s = v, f"{(str(int(c['kv']))+' kV') if c['kv'] else 'unknown-kV'} substation{(' '+c['name']) if c['name'] else ''} {c['mi']:.1f} mi"
    parts["substation"] = (best_s, why_s)
    pipe = 20 * ramp(g.get("mi"), 1, 15)
    li = rec["gas"]["line_in"] or rec["gas"]["main_in"]
    listed = 12 if (li and li >= 6) else 9 if (li and li >= 4) else 5 if rec["gas"]["provider"] else 0
    if pipe >= listed:
        parts["gas"] = (pipe, f"EIA pipeline {g['mi']:.1f} mi ({g.get('operator') or 'operator n/a'})" if g.get("mi") is not None else "No pipeline data")
    else:
        parts["gas"] = (listed, f"Listed gas service: {rec['gas']['provider']}" + (f", {li:g}\" line" if li else ""))
    a = rec["acres"] or 0
    sc = 6 if rec["acres_suspect"] else 15 if a >= 500 else 12 if a >= 250 else 9 if a >= 150 else 6 if a >= 100 else 3 if a >= 50 else 1
    parts["scale"] = (sc, f"{a:,.0f} acres listed" + (" (acreage looks like a listing error)" if rec["acres_suspect"] else ""))
    t = rec["telecom"]; txt = (t["capability"] + " " + rec["description"]).lower()
    f = (5 if t["provider"] else 0) + (3 if "fiber" in txt or "fibre" in txt else 0) + (2 if t["redundant"] else 0)
    parts["fiber"] = (f, (t["provider"] or "No telecom provider listed") + (" · fiber mentioned" if "fiber" in txt else ""))
    w = (5 if rec["water"]["provider"] else 0) + (2 if max(rec["water"]["line_in"] or 0, rec["water"]["main_in"] or 0) >= 8 else 0) + (3 if rec["sewer"]["provider"] else 0)
    parts["water"] = (w, (rec["water"]["provider"] or "No water provider listed") + (" · sewer listed" if rec["sewer"]["provider"] else ""))
    total = round(sum(v for v, _ in parts.values()))
    rec["score_parts"] = {k: {"pts": round(v, 1), "max": SCORING[k]["max"], "why": why} for k, (v, why) in parts.items()}
    top = sorted(parts.items(), key=lambda kv: -kv[1][0] / SCORING[kv[0]]["max"])
    rec["score_summary"] = "; ".join(why for _, (_, why) in top[:3])
    fp = rec.get("floodplain") or {}
    if fp.get("pct") is not None:      # FEMA 100-yr floodplain: a penalty, never a bonus
        pen = floodplain.penalty(fp["pct"]); pct = fp["pct"]
        total = total - pen
        if pct > 50: total = min(total, floodplain.CAP_OVER_HALF)
        why = (f"{pct:g}% of the site in the FEMA 100-yr floodplain (SFHA{', zone ' + '/'.join(fp.get('zones') or []) if fp.get('zones') else ''})"
               f" · {fp.get('method', '')}" + (" · capped: over half the site floods" if pct > 50 else "" if pen else f" · under {floodplain.MIN_PCT}%: no penalty"))
        rec["score_parts"]["floodplain"] = {"pts": -pen, "max": 0, "penalty": True, "pct": pct, "why": why}
        if pen: rec["score_summary"] = f"{pct:g}% in 100-yr floodplain; " + rec["score_summary"]
    elif fp:
        rec["score_parts"]["floodplain"] = {"pts": 0, "max": 0, "penalty": True, "pct": None, "why": fp.get("note") or "Floodplain not checked"}
    rec["score"] = max(0, min(100, round(total)))

# ---------------------------------------------------------------- contacts
EVERGY_RX = re.compile(r"evergy|westar|kcp&?l|kansas city power|kansas gas (and|&) electric|kg&e", re.I)

def attach_contacts(rec, curated):
    cs = list(rec["contacts"])
    key_c = f"{rec['city'].lower()}|{rec['county'].lower()}"
    loc = curated.get("local", {})
    seen_orgs = set()
    for k in (rec["city"].lower(), f"county:{rec['county'].lower()}"):
        for c in loc.get(k, []):
            if (c["org"], c["name"]) in seen_orgs: continue
            seen_orgs.add((c["org"], c["name"]))
            cs.append(dict(c, role=c.get("role") or "Local economic development"))
    providers = " ".join(e["provider"] for e in rec["electric"])
    if EVERGY_RX.search(providers):
        cs += [dict(c, role="Utility economic development (Evergy)") for c in curated.get("evergy", [])]
    for e in rec["electric"]:
        if e["provider"] and (e["phone"] or e["contact"]):
            cs.append({"name": e["contact"], "title": "", "org": e["provider"], "email": "", "phone": e["phone"],
                       "source": rec["lois_url"], "role": "Electric provider (as listed)"})
    cs += [dict(c, role="State site-selection contact") for c in curated.get("state", [])]
    out, seen = [], set()
    for c in cs:
        k = (c.get("email") or "", c.get("phone") or "", c.get("name") or "")
        if k in seen: continue
        seen.add(k); out.append(c)
    rec["contacts"] = out

# ---------------------------------------------------------------- geodata
def load_geo(cache_dir):
    """Public GIS layers (HIFLD lines, EIA gas, OSM substations) as nearest-feature trees. Updates STATUS."""
    try:
        tx = cached(cache_dir, "hifld_tx.json", lambda: arcgis_all(HIFLD_TX, "VOLTAGE,VOLT_CLASS,OWNER,STATUS,SUB_1,SUB_2"))
        STATUS["HIFLD transmission lines (ArcGIS)"] = {"ok": True, "detail": f"{len(tx)} line features in KS bbox", "url": HIFLD_TX}
    except Exception as e:
        tx = []; STATUS["HIFLD transmission lines (ArcGIS)"] = {"ok": False, "detail": str(e)[:200], "url": HIFLD_TX}
    try:
        gas = cached(cache_dir, "eia_gas.json", lambda: arcgis_all(EIA_GAS, "TYPEPIPE,Operator,Status"))
        STATUS["EIA natural gas pipelines (ArcGIS)"] = {"ok": True, "detail": f"{len(gas)} pipeline features", "url": EIA_GAS}
    except Exception as e:
        gas = []; STATUS["EIA natural gas pipelines (ArcGIS)"] = {"ok": False, "detail": str(e)[:200], "url": EIA_GAS}
    try:
        subs = cached(cache_dir, "osm_subs.json", fetch_osm_substations)
        STATUS["OpenStreetMap substations (Overpass)"] = {"ok": True, "detail": f"{len(subs)} substations, {sum(1 for s in subs if s['kv'])} with voltage", "url": OVERPASS[0]}
    except Exception as e:
        STATUS["OpenStreetMap substations (Overpass)"] = {"ok": False, "detail": f"{str(e)[:150]} — using HIFLD line end-points", "url": OVERPASS[0]}
        subs = []
        for f in tx:  # fallback: named end-points of HIFLD lines approximate substations
            kv = line_kv(f["a"])
            for path, nm in ((f["paths"][0][0], f["a"].get("SUB_1")), (f["paths"][-1][-1], f["a"].get("SUB_2"))):
                if nm and not re.match(r"(?i)^(tap|unknown|not available)", nm):
                    subs.append({"lat": path[1], "lon": path[0], "name": nm, "operator": "", "kv": kv, "kind": "hifld-endpoint", "osm": HIFLD_TX})

    # build nearest-trees by voltage class
    line_classes = [69, 115, 138, 230, 345]
    tx_geo = []
    for f in tx:
        if str(f["a"].get("STATUS", "")).upper() not in ("", "IN SERVICE", "NOT AVAILABLE"):
            continue
        g = proj_paths(f["paths"]); kv = line_kv(f["a"])
        if g is not None: tx_geo.append((g, kv, clean(f["a"].get("OWNER"))))
    line_trees = {c: Nearest([g for g, kv, _ in tx_geo if kv and kv >= c], [(kv, o) for g, kv, o in tx_geo if kv and kv >= c]) for c in line_classes}
    gas_geo = [(proj_paths(f["paths"]), f["a"]) for f in gas]
    gas_tree = Nearest([g for g, _ in gas_geo if g is not None], [a for g, a in gas_geo if g is not None])
    sub_pts = []
    for s in subs:
        if s.get("kind") in ("minor_distribution", "traction", "industrial"):
            continue
        x, y = TO_M.transform(s["lon"], s["lat"]); sub_pts.append((Point(x, y), s))
    sub_trees = {c: Nearest([p for p, s in sub_pts if (s["kv"] or 0) >= c], [s for p, s in sub_pts if (s["kv"] or 0) >= c]) for c in line_classes}
    sub_any = Nearest([p for p, s in sub_pts], [s for p, s in sub_pts])

    return {"line_classes": line_classes, "line_trees": line_trees, "gas_tree": gas_tree, "sub_trees": sub_trees, "sub_any": sub_any,
            "flood": True, "flood_cache": None}

def geo_apply(r, G):
    """Power/gas proximity for one record (needs lat/lon), then the infrastructure score."""
    line_classes, line_trees, gas_tree, sub_trees, sub_any = G["line_classes"], G["line_trees"], G["gas_tree"], G["sub_trees"], G["sub_any"]
    r["power"] = {"lines_by_class": [], "subs_by_class": []}; r["gasline"] = {}
    if G.get("flood", True) and "floodplain" not in r and (r["lat"] is not None or r.get("site_geojson")):
        r["floodplain"] = floodplain.measure(r, G.get("flood_cache"), r.pop("_fp_prev", None))
    r.pop("_fp_prev", None)
    if r["lat"] is None:
        r["geo_note"] = r.get("geo_note") or "No coordinates on listing"; score(r); return
    x, y = TO_M.transform(r["lon"], r["lat"]); pt = Point(x, y)
    for c in line_classes:
        mi, meta = line_trees[c].query(pt)
        if mi is not None:
            r["power"]["lines_by_class"].append({"class": c, "kv": meta[0], "owner": meta[1], "mi": round(mi, 2)})
    for c in line_classes:
        mi, s = sub_trees[c].query(pt)
        if mi is not None:
            r["power"]["subs_by_class"].append({"class": c, "kv": s["kv"], "name": s["name"], "operator": s["operator"], "mi": round(mi, 2), "ref": s["osm"]})
    mi, s = sub_any.query(pt)
    if mi is not None:
        r["power"]["nearest_sub"] = {"kv": s["kv"], "name": s["name"], "mi": round(mi, 2), "ref": s["osm"]}
        r["power"]["subs_by_class"].append({"class": 0, "kv": s["kv"], "name": s["name"], "operator": s["operator"], "mi": round(mi, 2), "ref": s["osm"]})
    lb = r["power"]["lines_by_class"]
    if lb:
        r["power"]["nearest_line"] = min(lb, key=lambda c: c["mi"])
        hv = [c for c in lb if c["class"] >= 230]
        r["power"]["nearest_230"] = min(hv, key=lambda c: c["mi"]) if hv else None
    mi, a = gas_tree.query(pt)
    if mi is not None:
        r["gasline"] = {"mi": round(mi, 2), "operator": clean(a.get("Operator")), "type": clean(a.get("TYPEPIPE"))}
    score(r)

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--geo-cache-dir", default=None, help="cache for public GIS layers only (safe to persist, e.g. actions/cache)")
    ap.add_argument("--contacts", default=None, help="curated contacts JSON (plaintext, decrypted at runtime)")
    ap.add_argument("--state", default=None, help="state JSON (first-seen dates and hashes); updated in place")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)

    cert = []
    try:
        cert = fetch_certified_list(); STATUS["Kansas Commerce Certified Sites page"] = {"ok": True, "detail": f"{len(cert)} sites", "url": CERT_LIST_URL}
    except Exception as e:
        STATUS["Kansas Commerce Certified Sites page"] = {"ok": False, "detail": f"{e} — fell back to LOIS siteCertified flag", "url": CERT_LIST_URL}
    cert_by_id = {c["lois_id"]: c for c in cert if c["lois_id"]}

    srcs = defaultdict(set); lois_items = {}
    for org, (label, page) in LOIS_ORGS.items():
        try:
            items = fetch_lois_org(org)
            ks = [i for i in items if i.get("state") == "KS"]
            big = [i for i in ks if (num(i.get("acres")) or 0) >= MIN_ACRES]
            for i in big:
                srcs[i["_id"]].add(label); lois_items[i["_id"]] = i
                if not cert and i.get("siteCertified") and org == KS_ORG:
                    cert_by_id.setdefault(i["_id"], None)
            STATUS[label] = {"ok": True, "detail": f"{len(items)} listings, {len(ks)} in KS, {len(big)} ≥{MIN_ACRES:.0f} ac", "url": page}
        except Exception as e:
            STATUS[label] = {"ok": False, "detail": str(e)[:200], "url": page}
    for sid in cert_by_id:
        srcs[sid].add("Kansas Certified Sites")

    recs, fails, small = [], [], []
    for n, sid in enumerate(sorted(srcs)):
        try:
            d = cached(args.cache_dir, f"site_{sid}.json", lambda: fetch_lois_site(sid))
            if not d or "basicInfo" not in d:
                raise RuntimeError("empty")
            rec = site_record(d, srcs[sid], cert_by_id.get(sid))
            keep, why = meets_min_acres(rec, d, cert_by_id.get(sid))
            if keep:
                if why: rec["acres_note"] = why
                recs.append(rec)
            else:
                small.append((rec["name"], rec["acres"], why))
        except Exception as e:
            fails.append((sid, str(e)[:120]))
        time.sleep(0.15)
    STATUS["100+ acre filter"] = {"ok": True, "detail": f"{len(small)} listings dropped as under {MIN_ACRES:.0f} acres (certified sites included)", "url": ""}
    if small:
        log("dropped <100 ac: " + "; ".join(f"{n} ({a})" for n, a, _ in small[:40]))
    STATUS["LOIS site detail API"] = {"ok": not fails or len(fails) < len(srcs) / 2, "detail": f"{len(recs)} fetched, {len(fails)} failed", "url": f"{LOIS_API}/site/<id>"}

    # de-duplicate the same parcel listed twice (same name, <0.5 mi apart)
    recs.sort(key=lambda r: (not r["certified"], -(r["percent_complete"] or 0)))
    kept = []
    for r in recs:
        dup = None
        for k in kept:
            if norm_name(k["name"]) == norm_name(r["name"]) and r["lat"] and k["lat"] and \
               abs(k["lat"] - r["lat"]) < 0.007 and abs(k["lon"] - r["lon"]) < 0.009:
                dup = k; break
        if dup:
            dup.setdefault("duplicate_ids", []).append(r["id"]); dup["sources"] = sorted(set(dup["sources"]) | set(r["sources"]))
        else:
            kept.append(r)
    recs = kept

    G = load_geo(args.geo_cache_dir or args.cache_dir)
    # FEMA floodplain, in parallel and time-boxed; public listings only, cache holds hashes + % (no coordinates)
    G["flood_cache"] = args.geo_cache_dir or args.cache_dir
    t0 = time.time()
    ok, bad = floodplain.apply_all(recs, G["flood_cache"])
    STATUS["FEMA NFHL floodplain (ArcGIS)"] = {"ok": ok >= bad, "detail": f"{ok} sites checked, {bad} not checked ({time.time() - t0:.0f} s)", "url": floodplain.NFHL}
    for r in recs:
        geo_apply(r, G)

    # contacts
    curated = {}
    if args.contacts and os.path.exists(args.contacts):
        curated = json.load(open(args.contacts))
        STATUS["Curated local EDO contacts"] = {"ok": True, "detail": f"{sum(len(v) for v in curated.get('local', {}).values())} local contacts", "url": ""}
    else:
        STATUS["Curated local EDO contacts"] = {"ok": False, "detail": "contacts file not available", "url": ""}
    for r in recs:
        attach_contacts(r, curated)

    # change tracking (first seen / content hash)
    state = {}
    if args.state and os.path.exists(args.state):
        state = json.load(open(args.state))
    baseline = state.get("baseline") or now.date().isoformat()
    state["baseline"] = baseline
    seen = state.setdefault("sites", {})
    for r in recs:
        h = hashlib.sha1(json.dumps({k: (r[k].get("raw", r[k]) if k == "price" and isinstance(r[k], dict) else r[k]) for k in ("name", "acres", "price", "zoning", "electric", "gas", "water", "telecom")}, sort_keys=True).encode()).hexdigest()[:12]
        s = seen.get(r["id"])
        if not s:
            s = seen[r["id"]] = {"first_seen": now.date().isoformat(), "hash": h, "changed": None}
        elif s["hash"] != h:
            s["hash"], s["changed"] = h, now.date().isoformat()
        s["last_seen"] = now.date().isoformat()
        created = (r["lois_created"] or "")[:10]
        week_ago = (now - dt.timedelta(days=7)).date().isoformat()
        r["first_seen"] = s["first_seen"]
        r["is_new"] = (s["first_seen"] >= week_ago and s["first_seen"] != baseline) or created >= week_ago
        r["is_changed"] = bool(s.get("changed") and s["changed"] >= week_ago) or (r["lois_modified"] or "")[:10] >= week_ago
    if args.state:
        json.dump(state, open(args.state, "w"), indent=1)

    recs.sort(key=lambda r: -r["score"])
    out = {"generated_at": now.isoformat(timespec="seconds"), "min_acres": MIN_ACRES, "count": len(recs),
           "sources": STATUS, "scoring": SCORING, "baseline": baseline, "sites": recs,
           "counts_by_source": {k: sum(1 for r in recs if k in r["sources"]) for k in ["Kansas Certified Sites"] + [v[0] for v in LOIS_ORGS.values()]}}
    p = os.path.join(args.out_dir, "sites.json")
    json.dump(out, open(p, "w"), indent=1)
    log(f"wrote {p}: {len(recs)} sites")
    for k, v in STATUS.items():
        log(("OK  " if v["ok"] else "FAIL"), k, "-", v["detail"])
    if not recs:
        sys.exit(2)

if __name__ == "__main__":
    main()
