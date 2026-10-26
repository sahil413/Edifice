"""FEMA 100-year floodplain share of a site, from the public FEMA NFHL ArcGIS service.

Layer 28 (Flood Hazard Zones), features with SFHA_TF = 'T' (Special Flood Hazard Area:
zones A, AE, AH, AO, AR, A99, V, VE = the 1%-annual-chance / "100-year" floodplain).
The site shape is, in order: a site polygon (rec["site_geojson"], e.g. the parcels from the
county layer), else a circle around the pin whose area equals the listed acres (20-2,000 ac;
40 ac when acres are unknown). Result goes to rec["floodplain"]; scrape.score() turns it into
the "floodplain" penalty part.

Caching: results are keyed by a hash of the shape and hold only the % and zone codes (no
coordinates), so the cache can sit next to the public GIS layers. Logs carry counts only.
"""
import hashlib, json, math, os, sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests
from shapely.geometry import Point, shape, box
from shapely.ops import unary_union, transform
from pyproj import Transformer

NFHL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
# Esri Living Atlas copy of the same NFHL flood hazard zones; used only when FEMA's server fails
NFHL_MIRROR = "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0"
SOURCE = "FEMA National Flood Hazard Layer (NFHL), Flood Hazard Zones, SFHA (100-yr)"
SOURCE_MIRROR = "FEMA NFHL flood hazard zones via Esri Living Atlas (USA Flood Hazard Areas), SFHA (100-yr)"
AC_M2 = 4046.8564224
_TO_M = Transformer.from_crs(4326, 5070, always_xy=True).transform     # CONUS Albers equal-area
_TO_LL = Transformer.from_crs(5070, 4326, always_xy=True).transform
MAX_AGE_D = 30
UA = {"User-Agent": "edifice-sites/1.0 (public data research)"}

# ---------------------------------------------------------------- scoring
MIN_PCT = 5          # below this the floodplain is treated as an edge/buffer matter: no penalty
PER_PCT = 1.2        # points off the infrastructure score per % of the site in the SFHA
CAP_OVER_HALF = 15   # a site more than half in the floodplain can score at most this (infrastructure)
RULE = (f"Penalty only. FEMA NFHL 100-yr floodplain (SFHA) share of the site polygon (or a circle of the listed acres "
        f"around the pin): under {MIN_PCT}% no penalty; otherwise −{PER_PCT:g} pts per % (e.g. 25% → −30), and a site more "
        f"than 50% in the floodplain is capped at {CAP_OVER_HALF}/100 on infrastructure.")

def penalty(pct):
    """Points to subtract for a floodplain share (0-100)."""
    if pct is None or pct < MIN_PCT: return 0.0
    return round(min(100.0, PER_PCT * pct), 1)

# ---------------------------------------------------------------- geometry
def site_shape(rec):
    """(shapely geometry in EPSG:5070 metres, method text) or (None, reason)."""
    gj = rec.get("site_geojson")
    if gj:
        try:
            g = json.loads(gj) if isinstance(gj, str) else gj
            if g.get("type") == "FeatureCollection": g = {"type": "GeometryCollection", "geometries": [f["geometry"] for f in g["features"]]}
            elif g.get("type") == "Feature": g = g["geometry"]
            geom = unary_union([shape(g).buffer(0)]) if g.get("type") != "GeometryCollection" else unary_union([shape(x).buffer(0) for x in g["geometries"]])
            m = transform(_TO_M, geom)
            if m.area > 0: return m, f"site polygon ({m.area / AC_M2:,.0f} ac)"
        except Exception:
            pass
    if rec.get("lat") is None or rec.get("lon") is None: return None, "site not pinned"
    ac = rec.get("acres")
    try: ac = float(ac) if ac else None
    except (TypeError, ValueError): ac = None
    if ac and rec.get("acres_suspect"): ac = None
    use = 40.0 if not ac else max(20.0, min(2000.0, ac))
    x, y = _TO_M(rec["lon"], rec["lat"])
    r = math.sqrt(use * AC_M2 / math.pi)
    return Point(x, y).buffer(r, 64), f"circle of {use:,.0f} ac around the pin (no site polygon)"

def _key(m):
    return hashlib.sha1(("v1|" + m.simplify(5).wkt).encode()).hexdigest()[:20]

def fetch_sfha(m, timeout=25, tries=2):
    """SFHA polygons intersecting m (EPSG:5070) from FEMA, else the Esri mirror. Returns (union, zones, source, url)."""
    err = None
    for url, src in ((NFHL, SOURCE), (NFHL_MIRROR, SOURCE_MIRROR)):
        try:
            u, z = _fetch(url, m, timeout, tries); return u, z, src, url
        except Exception as e:
            err = e
    raise err

def _fetch(url, m, timeout, tries):
    ll = transform(_TO_LL, m.envelope.buffer(30))
    x0, y0, x1, y1 = ll.bounds
    feats, off = [], 0
    while True:
        params = {"geometry": f"{x0},{y0},{x1},{y1}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                  "spatialRel": "esriSpatialRelIntersects", "where": "SFHA_TF='T'", "outFields": "FLD_ZONE",
                  "outSR": 4326, "returnGeometry": "true", "geometryPrecision": 6, "f": "geojson",
                  "resultOffset": off, "resultRecordCount": 750}
        err = None
        for i in range(tries):
            try:
                r = requests.get(url + "/query", params=params, timeout=timeout, headers=UA)
                r.raise_for_status(); d = r.json()
                if "error" in d: raise RuntimeError(str(d["error"])[:120])
                break
            except Exception as e:
                err = e; time.sleep(2 * (i + 1))
        else:
            raise RuntimeError(f"NFHL query failed: {type(err).__name__}")  # noqa
        fs = d.get("features") or []
        feats += fs
        if not (d.get("exceededTransferLimit") or (d.get("properties") or {}).get("exceededTransferLimit")) or not fs: break
        off += len(fs)
    polys, zones = [], set()
    clip = transform(_TO_LL, m.buffer(30))
    for f in feats:
        try:
            g = shape(f["geometry"]).buffer(0)
            if not g.intersects(clip): continue
            polys.append(g.intersection(clip)); zones.add((f.get("properties") or {}).get("FLD_ZONE") or "?")
        except Exception:
            continue
    u = unary_union(polys) if polys else None
    return (transform(_TO_M, u) if u is not None and not u.is_empty else None), sorted(zones)

def measure(rec, cache_dir=None, prev=None, fetch=True):
    """Floodplain dict for one record: {pct, method, zones, source, url, checked} (pct None when unknown)."""
    m, method = site_shape(rec)
    if m is None:
        return {"pct": None, "method": method, "source": SOURCE, "url": NFHL, "note": "Not checked: " + method}
    k = _key(m); p = os.path.join(cache_dir, "floodplain", k + ".json") if cache_dir else None
    if p and os.path.exists(p) and time.time() - os.path.getmtime(p) < MAX_AGE_D * 86400:
        try:
            c = json.load(open(p)); c["method"] = method; return c
        except Exception: pass
    if prev and prev.get("key") == k and prev.get("pct") is not None and not fetch:
        return prev
    if not fetch:
        return {"pct": None, "method": method, "source": SOURCE, "url": NFHL, "key": k, "note": "Not checked yet"}
    try:
        fz, zones, src, url = fetch_sfha(m)
        pct = 0.0 if fz is None else round(min(100.0, 100.0 * fz.intersection(m).area / m.area), 1)
        out = {"pct": pct, "method": method, "zones": zones, "source": src, "url": url, "key": k,
               "checked": dt.date.today().isoformat()}
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True); json.dump({x: out[x] for x in out if x != "method"}, open(p, "w"))
        return out
    except Exception as e:
        if prev and prev.get("key") == k and prev.get("pct") is not None:
            return dict(prev, note=f"FEMA NFHL unreachable this run; last result from {prev.get('checked', 'an earlier run')}")
        return {"pct": None, "method": method, "source": SOURCE, "url": NFHL, "key": k, "note": f"FEMA NFHL unreachable ({type(e).__name__}); not scored"}

def apply_all(recs, cache_dir=None, prev_by_id=None, workers=6, budget_s=900):
    """Set rec["floodplain"] for many records in parallel, within a time budget. Returns (checked, failed)."""
    prev_by_id = prev_by_id or {}; t0 = time.time()
    def one(r):
        live = time.time() - t0 < budget_s
        r["floodplain"] = measure(r, cache_dir, prev_by_id.get(r.get("id")), fetch=live)
        return r["floodplain"].get("pct") is not None
    with ThreadPoolExecutor(workers) as ex:
        ok = list(ex.map(one, recs))
    return sum(ok), len(ok) - sum(ok)

if __name__ == "__main__":
    # ad-hoc check: floodplain.py LAT LON ACRES  |  floodplain.py --geojson FILE
    if sys.argv[1] == "--geojson": rec = {"site_geojson": open(sys.argv[2]).read()}
    else: rec = {"lat": float(sys.argv[1]), "lon": float(sys.argv[2]), "acres": float(sys.argv[3]) if len(sys.argv) > 3 else None}
    print(json.dumps(measure(rec), indent=1))
