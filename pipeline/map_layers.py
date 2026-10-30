#!/usr/bin/env python3
"""Build Kansas-clipped, simplified GeoJSON overlay layers for the sites map (sites/layers/*.geojson).

Run by hand when the reference layers should be refreshed:  python3 pipeline/map_layers.py
Sources (all public, no keys):
  lines.geojson  HIFLD Electric Power Transmission Lines (public domain, US DHS/HIFLD open data)
  gas.geojson    EIA Natural Gas Interstate and Intrastate Pipelines (public domain, US EIA)
  subs.geojson   OpenStreetMap power=substation via Overpass (ODbL, © OpenStreetMap contributors)
  aadt.geojson   KDOT AADT Flow Map, state system (Kansas DOT public data)
"""
import json, os, time, urllib.parse, urllib.request

BBOX = (-102.06, 36.99, -94.58, 40.01)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sites", "layers")
UA = {"User-Agent": "edifice-sites-map/1.0"}
LINES = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0"
GAS = "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0"
AADT = "https://kanplan.ksdot.gov/arcgis_web_adaptor/rest/services/Transportation/AADT_Flow_Map/FeatureServer/0"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]


def get(url, data=None):
    for i in range(4):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=180))
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def arc(url, fields, where="1=1", offset=0.002, page=1000):
    feats, start = [], 0
    while True:
        q = {"where": where, "outFields": ",".join(fields), "geometry": ",".join(map(str, BBOX)),
             "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326,
             "spatialRel": "esriSpatialRelIntersects", "maxAllowableOffset": offset,
             "geometryPrecision": 4, "resultOffset": start, "resultRecordCount": page, "f": "geojson"}
        d = get(url + "/query?" + urllib.parse.urlencode(q))
        fs = d.get("features", [])
        feats += [f for f in fs if f.get("geometry")]
        if len(fs) < page:
            return feats
        start += page


def slim(feats, keep):
    out = []
    for f in feats:
        p = f.get("properties") or {}
        out.append({"type": "Feature", "geometry": f["geometry"], "properties": {k2: p.get(k) for k, k2 in keep.items()}})
    return out


def subs():
    s, w, n, e = BBOX[1], BBOX[0], BBOX[3], BBOX[2]
    q = f'[out:json][timeout:180];(node["power"="substation"]({s},{w},{n},{e});way["power"="substation"]({s},{w},{n},{e}););out center tags;'
    for u in OVERPASS:
        try:
            d = get(u, urllib.parse.urlencode({"data": q}).encode())
            break
        except Exception:
            d = None
    out = []
    for el in (d or {}).get("elements", []):
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None:
            continue
        t = el.get("tags", {})
        kv = None
        try:
            kv = max(int(x) for x in str(t.get("voltage", "")).split(";") if x.strip().isdigit()) // 1000 or None
        except ValueError:
            pass
        out.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]},
                    "properties": {"n": t.get("name"), "o": t.get("operator"), "kv": kv}})
    return out


def write(name, feats, src):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    with open(p, "w") as fh:
        json.dump({"type": "FeatureCollection", "source": src, "built": time.strftime("%Y-%m-%d"), "features": feats}, fh, separators=(",", ":"))
    print(name, len(feats), os.path.getsize(p) // 1024, "KB")


if __name__ == "__main__":
    write("lines.geojson", slim(arc(LINES, ["VOLTAGE", "VOLT_CLASS", "OWNER", "SUB_1", "SUB_2"]),
          {"VOLTAGE": "kv", "VOLT_CLASS": "c", "OWNER": "o", "SUB_1": "a", "SUB_2": "b"}), "HIFLD Electric Power Transmission Lines")
    write("gas.geojson", slim(arc(GAS, ["TYPEPIPE", "Operator"]), {"TYPEPIPE": "t", "Operator": "o"}), "EIA Natural Gas Pipelines")
    write("subs.geojson", subs(), "OpenStreetMap contributors (ODbL)")
    write("aadt.geojson", slim(arc(AADT, ["AADTCount", "AADTCountYear", "RouteID"], where="AADTCount>0", offset=0.003),
          {"AADTCount": "v", "AADTCountYear": "y", "RouteID": "r"}), "KDOT AADT Flow Map")
