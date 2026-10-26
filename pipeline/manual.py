#!/usr/bin/env python3
"""Manually added sites ("Add site" in the app, or a spec loaded by edks.py add-manual).

A manual site doc (sites/{m-...} or, for admin-only deals, rsites/{m-...}) starts as a pending stub:
  {view:'land', manual:true, status:'pending', input:<address | listing URL | PLSS legal | lat,lon>,
   edits:{field: value}  (hand-entered values; they always win), extra:{...} (service-account only)}
build() resolves the input from public sources, turns it into the same record shape the scraper
produces (scrape.site_record on a LOIS-shaped dict), adds GIS power/gas proximity and the same
infrastructure score. Anything no source states stays blank and is listed in rec['unknown'].
Nothing is guessed: no owner, parcel, zoning or price unless the listing or a person entered it.
"""
import html as _html, json, re, time, urllib.parse
import requests
import scrape

UA = scrape.UA
EDIT_KEYS = ["name", "address", "city", "county", "zip", "lat", "lon", "acres", "parcels", "owner", "zoning",
             "price_total", "price_per_acre", "electric", "gas", "water", "sewer", "telecom", "description", "url"]
TRACKED = ["address", "city", "county", "lat", "acres", "parcels", "owner", "zoning", "price", "electric", "gas",
           "water", "sewer", "telecom"]
CENSUS = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_COORD = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
BLM_PLSS = "https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer/2/query"
LOIS_ID = re.compile(r"\b([0-9a-f]{24})\b")
PLSS_RX = re.compile(r"(?i)(?:(?P<q>[NS][EW])\s*(?:1/4|¼|quarter)?\s*(?:of\s*)?)?(?:sec(?:tion)?\.?\s*)(?P<s>\d{1,2})"
                     r"[\s,\-]*T(?:wp\.?|ownship)?\s*(?P<t>\d{1,2})\s*(?P<td>[NS])[\s,\-]*R(?:ge\.?|ange)?\s*(?P<r>\d{1,2})\s*(?P<rd>[EW])")
LATLON_RX = re.compile(r"^\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{2,3}\.\d+)\s*$")
S = requests.Session(); S.headers.update({"User-Agent": UA})

def _get(url, **kw):
    for i in range(3):
        try:
            r = S.get(url, timeout=kw.pop("timeout", 30), **kw)
            if r.status_code in (429, 500, 502, 503, 504): time.sleep(3 * (i + 1)); continue
            return r
        except requests.RequestException:
            time.sleep(3 * (i + 1))
    return None

def kind_of(text):
    t = text.strip()
    if re.match(r"(?i)^https?://", t):
        return "lois" if ("locationone" in t or "kansascommerce" in t or "evergyed" in t) and LOIS_ID.search(t) else "url"
    if LATLON_RX.match(t): return "latlon"
    if PLSS_RX.search(t): return "plss"
    return "address"

# ------------------------------------------------------------- geocoding (public sources only)
def census_point(lat, lon):
    """County / incorporated place for a point (Census geographies)."""
    r = _get(CENSUS_COORD, params={"x": lon, "y": lat, "benchmark": "Public_AR_Current", "vintage": "Current_Current",
                                   "layers": "Counties,Incorporated Places", "format": "json"})
    out = {}
    try:
        g = r.json()["result"]["geographies"]
        if g.get("Counties"): out["county"] = re.sub(r"\s+County$", "", g["Counties"][0]["NAME"])
        if g.get("Incorporated Places"): out["city"] = re.sub(r"\s+city$", "", g["Incorporated Places"][0]["NAME"])
    except Exception:
        pass
    return out

def geocode(addr):
    """Street address -> {lat, lon, address, city, zip, county, geo_source}. Census first, then OSM Nominatim."""
    q = addr if re.search(r"(?i)\b(KS|Kansas)\b", addr) else addr + ", KS"
    r = _get(CENSUS, params={"address": q, "benchmark": "Public_AR_Current", "vintage": "Current_Current",
                             "layers": "Counties,Incorporated Places", "format": "json"})
    try:
        m = r.json()["result"]["addressMatches"]
        if m:
            m = m[0]; c = m["addressComponents"]; g = m.get("geographies", {})
            street = " ".join(x for x in [c.get("fromAddress") and m["matchedAddress"].split(",")[0]] if x)
            out = {"lat": m["coordinates"]["y"], "lon": m["coordinates"]["x"], "address": street or m["matchedAddress"].split(",")[0],
                   "zip": c.get("zip", ""), "geo_source": f"US Census geocoder (matched “{m['matchedAddress']}”)"}
            if g.get("Counties"): out["county"] = re.sub(r"\s+County$", "", g["Counties"][0]["NAME"])
            out["city"] = re.sub(r"\s+city$", "", g["Incorporated Places"][0]["NAME"]) if g.get("Incorporated Places") else c.get("city", "").title()
            return out
    except Exception:
        pass
    time.sleep(1.1)
    r = _get(NOMINATIM, params={"q": q, "format": "jsonv2", "addressdetails": 1, "countrycodes": "us", "limit": 1})
    try:
        j = r.json()
        if j:
            j = j[0]; a = j.get("address", {})
            if a.get("state") not in (None, "Kansas"): return {}
            out = {"lat": float(j["lat"]), "lon": float(j["lon"]), "zip": a.get("postcode", ""),
                   "address": " ".join(x for x in [a.get("house_number"), a.get("road")] if x),
                   "city": a.get("city") or a.get("town") or a.get("village") or "",
                   "county": re.sub(r"\s+County$", "", a.get("county", "")),
                   "geo_source": f"OpenStreetMap Nominatim ({j.get('addresstype') or j.get('type')}: “{j.get('display_name', '')[:120]}”)"}
            if j.get("addresstype") in ("city", "town", "village", "county", "state", "postcode"):
                out["geo_coarse"] = True   # only a place, not a site: no GIS distances
            return out
    except Exception:
        pass
    return {}

def plss(text):
    """'SE 1/4 Sec 26 T19S R3W' (6th PM) -> quarter/section centre from the BLM PLSS (CadNSDI) layer."""
    m = PLSS_RX.search(text)
    if not m: return {}
    t, r = int(m["t"]), int(m["r"])
    pid = f"KS06{t:03d}0{m['td'].upper()}{r:03d}0{m['rd'].upper()}0"
    resp = _get(BLM_PLSS, params={"where": f"PLSSID='{pid}'", "outFields": "FRSTDIVID,FRSTDIVNO", "returnGeometry": "true", "outSR": 4326, "f": "json"})
    try:
        feats = [f for f in resp.json()["features"] if str(f["attributes"]["FRSTDIVNO"]).lstrip("0") == m["s"].lstrip("0")]
    except Exception:
        return {}
    if not feats: return {}
    pts = [p for ring in feats[0]["geometry"]["rings"] for p in ring]
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts); y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    q = (m["q"] or "").upper()
    if q:
        x0, x1 = ((x0 + x1) / 2, x1) if q[1] == "E" else (x0, (x0 + x1) / 2)
        y0, y1 = ((y0 + y1) / 2, y1) if q[0] == "N" else (y0, (y0 + y1) / 2)
    lab = f"{(q + ' ¼ ') if q else ''}Sec. {m['s']}-T{t}{m['td'].upper()}-R{r}{m['rd'].upper()}"
    out = {"lat": round((y0 + y1) / 2, 6), "lon": round((x0 + x1) / 2, 6), "plss": lab,
           "geo_source": f"BLM PLSS CadNSDI section {feats[0]['attributes']['FRSTDIVID']} ({'quarter of ' if q else ''}section centre)"}
    out.update(census_point(out["lat"], out["lon"]))
    return out

# ------------------------------------------------------------- listing pages
ACRES_RX = re.compile(r"(?i)(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:\+/-|±)?\s*(?:acres|acre|ac)\b")
PRICE_RX = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:M|million)\b)", re.I)
ZONING_RX = re.compile(r"(?i)\bzon(?:ed|ing)\s*[:\-]?\s*([A-Z]{1,3}-?\d{0,2}[A-Z]?(?:\s*\([^)]{0,40}\))?)")

def _jsonld(soup_html):
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', soup_html, re.S | re.I):
        try:
            v = json.loads(m.group(1).strip())
            out += v if isinstance(v, list) else v.get("@graph", [v]) if isinstance(v, dict) else []
        except Exception:
            pass
    return out

def listing(url):
    """Fetch a listing page; take only values the page states in a recognisable form."""
    r = _get(url, timeout=40, headers={"Accept": "text/html,application/xhtml+xml"})
    if r is None or r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return {"listing_note": f"Listing page could not be read ({'no response' if r is None else 'HTTP ' + str(r.status_code)}); fill the fields by hand"}
    h = r.text; out = {"listing_source": url}
    meta = lambda p: (re.search(rf'<meta[^>]+(?:property|name)=["\']{p}["\'][^>]+content=["\']([^"\']+)', h, re.I) or [None, None])[1]
    title = meta("og:title") or (re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I) or [None, ""])[1]
    if title: out["name"] = _html.unescape(re.sub(r"\s+", " ", title)).strip()[:160]
    desc = meta("og:description") or meta("description") or ""
    text = _html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)))
    text = re.sub(r"\s+", " ", text)
    for o in _jsonld(h):
        if not isinstance(o, dict): continue
        a = o.get("address") or (o.get("location") or {}).get("address") if isinstance(o.get("location"), dict) else o.get("address")
        if isinstance(a, dict) and a.get("streetAddress"):
            out.update(address=a.get("streetAddress", ""), city=a.get("addressLocality", ""), zip=a.get("postalCode", ""))
        g = o.get("geo") or {}
        if isinstance(g, dict) and g.get("latitude"):
            out.update(lat=float(g["latitude"]), lon=float(g["longitude"]), geo_source="listing page (schema.org geo)")
    blob = (desc + " " + text)[:20000]
    acres = [float(x.replace(",", "")) for x in ACRES_RX.findall(blob)]
    acres = [a for a in acres if 0.5 <= a < scrape.SUSPECT_ACRES]
    if acres and len({round(a) for a in acres[:3]}) == 1:   # only when the first mentions agree
        out["acres"] = acres[0]
    pm = PRICE_RX.search(desc) or PRICE_RX.search(text[:6000])
    if pm:
        v = pm.group(1).replace(",", ""); mult = 1e6 if re.search(r"(?i)m", v) else 1
        try:
            val = float(re.sub(r"(?i)\s*(m|million)", "", v)) * mult
            tail = blob[pm.end(): pm.end() + 12].lower()
            if "/ac" in tail or "per acre" in tail: out["price_per_acre"] = val
            elif val >= 10000: out["price_total"] = val
        except ValueError:
            pass
    z = ZONING_RX.search(blob)
    if z: out["zoning"] = z.group(1).strip()
    out["description"] = (desc or "")[:1200]
    return out

# ------------------------------------------------------------- record
def _lois_shape(f, sid):
    util = lambda prov: {"supplierName": prov} if prov else {}
    pricing = {}
    if f.get("price_total"): pricing["salePrice"] = f["price_total"]
    if f.get("price_per_acre"): pricing["pricingNote"] = f"${float(f['price_per_acre']):,.0f}/acre"
    if f.get("price_note"): pricing["pricingNote"] = (pricing.get("pricingNote", "") + " " + f["price_note"]).strip()
    return {"_id": sid, "basicInfo": {"name": f.get("name") or "", "acres": f.get("acres"), "pricing": pricing,
            "location": {"city": f.get("city", ""), "countyName": f.get("county", ""), "street": f.get("address", ""), "zip": f.get("zip", ""),
                         "geoLocation": {"coordinates": [f.get("lon"), f.get("lat")]}}},
            "utilities": {"electric": [util(f.get("electric"))] if f.get("electric") else [], "naturalGas": util(f.get("gas")),
                          "water": util(f.get("water")), "wasteWater": util(f.get("sewer")), "telecommunications": util(f.get("telecom"))},
            "additionalDetails": {"about": {"zoning": f.get("zoning", ""), "description": f.get("description", "")}}}

def _num(v):
    try: return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError): return None

def build(sid, doc, G):
    """doc: the Firestore manual doc. Returns the full record (same keys as scraped land sites)."""
    inp = (doc.get("input") or "").strip(); edits = dict(doc.get("edits") or {}); extra = dict(doc.get("extra") or {})
    kind = kind_of(inp) if inp else "none"
    found, prov, notes = {}, {}, []
    lois = None
    if kind == "lois":
        try:
            lois = scrape.fetch_lois_site(LOIS_ID.search(inp).group(1))
        except Exception as e:
            notes.append(f"LOIS listing could not be fetched ({type(e).__name__})")
    elif kind == "url":
        found = listing(inp); found.pop("listing_source", None)
        if found.get("listing_note"): notes.append(found.pop("listing_note"))
        if not found.get("lat") and (found.get("address") or edits.get("address")):
            g = geocode(", ".join(x for x in [edits.get("address") or found.get("address"), edits.get("city") or found.get("city"), "KS"] if x))
            for k, v in g.items(): found.setdefault(k, v)
    elif kind == "latlon":
        m = LATLON_RX.match(inp); found = {"lat": float(m.group(1)), "lon": float(m.group(2)), "geo_source": "coordinates entered"}
        found.update(census_point(found["lat"], found["lon"]))
    elif kind == "plss":
        found = plss(inp) or {}
        if not found: notes.append("PLSS description could not be located in the BLM PLSS layer")
    elif kind == "address":
        found = geocode(inp)
        if not found: notes.append("Address could not be geocoded (Census, OpenStreetMap)")
    for k, v in found.items():
        if k in EDIT_KEYS: prov[k] = found.get("geo_source", "") if k in ("lat", "lon", "county", "city", "zip", "address") else ("listing page" if kind == "url" else "")
    f = {**found, **{k: v for k, v in edits.items() if v not in (None, "")}}
    if not f.get("name") and doc.get("name"): f["name"] = doc["name"]
    for k in edits:
        if edits[k] not in (None, ""): prov[k] = "entered by hand"
    for k in ("lat", "lon", "acres", "price_total", "price_per_acre"):
        if k in f: f[k] = _num(f[k])
    if found.get("geo_coarse") and not edits.get("lat"):
        notes.append("Input located only to a town or county, not a site; GIS distances left blank")
        f["lat"] = f["lon"] = None
    if lois:
        rec = scrape.site_record(lois, {"Manual (LOIS listing)"}, None)
        for k, v in edits.items():
            if k in ("city", "county", "address", "zip", "zoning", "description", "name"): rec[k] = v
            if k in ("lat", "lon", "acres"): rec[k] = _num(v)
    else:
        rec = scrape.site_record(_lois_shape(f, sid), {"Manual"}, None)
        rec["contacts"] = []
        if isinstance(rec.get("price"), dict) and (rec["price"].get("total") is not None or rec["price"].get("per_acre") is not None):
            rec["price"]["source"] = "entered by hand" if (edits.get("price_total") or edits.get("price_per_acre")) else ("listing page" if kind == "url" else "")
    rec["url"] = f.get("url") or (inp if kind in ("url", "lois") else "") or (f"https://www.google.com/maps/search/?api=1&query={rec['lat']},{rec['lon']}" if rec.get("lat") else "")
    rec["lois_url"] = rec["url"] if kind == "lois" else ""
    rec.update(manual=True, source="manual", input=inp, input_kind=kind, owner=f.get("owner") or "", parcels=f.get("parcels") or [],
               plss=found.get("plss", ""), geo_source=found.get("geo_source", "") if not edits.get("lat") else "entered by hand",
               added_by=doc.get("created_by", ""), provenance=prov, manual_notes=notes, restricted=bool(doc.get("restricted")))
    if isinstance(rec["parcels"], str): rec["parcels"] = [p.strip() for p in re.split(r"[;\n]", rec["parcels"]) if p.strip()]
    for k, v in extra.items():          # service-account-only facts (contacts, risks, sentiment, utilities detail)
        if k == "contacts": rec["contacts"] = v + rec["contacts"]
        elif isinstance(v, dict) and isinstance(rec.get(k), dict): rec[k] = {**rec[k], **v}
        else: rec[k] = v
    if rec["lat"] is None: rec["geo_note"] = "No coordinates" + (" (input was " + kind + ")" if kind != "none" else "")
    if G:
        scrape.geo_apply(rec, G)
    else:
        rec["power"] = {"lines_by_class": [], "subs_by_class": []}; rec["gasline"] = {}; scrape.score(rec)
    if rec.get("acres") is None:          # score() reads 0 acres as "1 pt"; say it is unknown instead
        rec["score_parts"]["scale"]["why"] = "Acreage unknown"
    if rec["lat"] is None:               # nothing was measured; do not report "no line within 10 mi"
        for k in ("transmission", "substation", "gas"):
            if k in rec["score_parts"]: rec["score_parts"][k]["why"] = "Unknown: site not pinned"
        rec["score_summary"] = "Site not pinned to a location yet; power, gas and distance scores are unknown"
        rec["score_partial"] = True
    unk = []
    for k in TRACKED:
        v = rec.get(k)
        if k == "price": v = (rec["price"] or {}).get("total") or (rec["price"] or {}).get("per_acre")
        if k == "electric": v = [e for e in v if e.get("provider")]
        if isinstance(v, dict): v = v.get("provider")
        if v in (None, "", [], {}): unk.append(k)
    rec["unknown"] = unk
    rec["first_seen"] = rec.get("first_seen") or doc.get("first_seen") or time.strftime("%Y-%m-%d")
    rec["is_new"] = rec["is_changed"] = False
    rec["lois_created"] = rec.get("lois_created") or rec["first_seen"]
    return rec
