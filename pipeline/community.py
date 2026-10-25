#!/usr/bin/env python3
"""Edifice / Kansas sites: annexation status + Community/Regulatory score.

Post-processes <data>/sites.json written by scrape.py (in place):
  * annexation  - point-in-polygon of the listing pin against U.S. Census TIGERweb
                  Incorporated Places (current vintage, Jan 1 of this year, from the
                  Boundary & Annexation Survey), plus annexation language in the listing text.
  * community   - Regulatory sub-score (moratoriums / bans / restrictive rules at the
                  city, county and state level) + Sentiment sub-score (news coverage,
                  hearings, petitions, lawsuits, support), from
                    - Bing News RSS searches (publisher URLs) per city, per county and statewide
                    - each article page is fetched to confirm it exists and is about Kansas
                    - a curated, hand-verified issues file (private, encrypted in repo)
  * overall     - weighted blend of the infrastructure score and the community score.
News is refreshed weekly (cache max-age 6 days); annexation daily.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse, datetime as dt, hashlib, html, json, os, re, sys, time, urllib.parse
import xml.etree.ElementTree as ET
import requests
from shapely.geometry import shape, Point
from shapely.ops import transform as shp_transform  # noqa
from pyproj import Transformer

UA_BOT = "EdificeSitesBot/1.0 (+https://edifice.vc)"
UA_WEB = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/4"
BING = "https://www.bing.com/news/search"
TO_M = Transformer.from_crs(4326, 5070, always_xy=True).transform
M_PER_MI = 1609.344

WEIGHTS = {"infrastructure": 0.6, "community": 0.4}
COMMUNITY_WEIGHTS = {"regulatory": 0.6, "sentiment": 0.4}
COMMUNITY_RULES = {
    "regulatory": ("Starts at 100. The governing jurisdiction is the city if the pin is inside city limits, "
                   "otherwise the county (the nearby city counts as secondary if within 3 mi, since it could annex). "
                   "Governing jurisdiction, latest status: active/extended moratorium or ban −60; proposed/drafting "
                   "moratorium, ban or ballot measure −30; moratorium lifted/expired −5; data-center zoning rules adopted −10 "
                   "(proposed −5). Secondary jurisdiction: −25 / −12 / −2; rules −2 (proposed −1). A failed moratorium "
                   "motion scores 0. Statewide: enacted restriction −15, pending bill/proposal −5 (curated). Floor 0. "
                   "Hand-verified items marked final set a jurisdiction's status; otherwise the latest dated news item does."),
    "sentiment": ("Starts at 70. Each article/hearing/petition about data centers, large loads or utility rate cases in "
                  "the site's city or county: negative −10, positive +6, neutral 0, × recency (≤6 mo 1.0, ≤12 mo 0.6, "
                  "≤24 mo 0.3, older 0) × jurisdiction (governing 1.0, secondary 0.5). Negative total capped at −60. "
                  "Tags come from the hand-verified file where present, otherwise keyword rules on the headline/summary."),
    "community": "Community = 0.6 × Regulatory + 0.4 × Sentiment.",
    "overall": "Overall = 0.6 × Infrastructure + 0.4 × Community.",
}
STATUS = {}

S = requests.Session()

def log(*a):
    print("[community]", *a, file=sys.stderr, flush=True)

def http(url, params=None, ua=UA_BOT, timeout=30, tries=3):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout, headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"})
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (401, 403, 404, 410, 451):
                break
        except Exception as e:
            last = repr(e)[:160]
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url[:120]}: {last}")

def cached(cdir, name, fn, max_age_h):
    p = os.path.join(cdir, name) if cdir else None
    if p and os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
        return json.load(open(p))
    try:
        v = fn()
    except Exception:
        if p and os.path.exists(p):
            log(f"WARN {name}: live fetch failed, using cache from {time.ctime(os.path.getmtime(p))}")
            return json.load(open(p))
        raise
    if p:
        os.makedirs(cdir, exist_ok=True); json.dump(v, open(p, "w"))
    return v

# ------------------------------------------------------------------ annexation
def fetch_places():
    feats, off = [], 0
    while True:
        d = http(f"{TIGER}/query", params={"where": "STATE='20'", "outFields": "NAME,BASENAME,GEOID,LSADC,FUNCSTAT",
                                            "returnGeometry": "true", "outSR": 4326, "f": "geojson",
                                            "resultOffset": off, "resultRecordCount": 200, "geometryPrecision": 5},
                 timeout=120).json()
        fs = d.get("features", [])
        feats += fs
        if len(fs) < 200:
            break
        off += len(fs)
    if len(feats) < 500:
        raise RuntimeError(f"only {len(feats)} Kansas places returned")
    return feats

ANNEX_RX = re.compile(r"[^.;\n]*\b(annex\w*|city limits|unincorporated|corporate limits|county zoning)\b[^.;\n]*", re.I)
PENDING_RX = re.compile(r"annex\w*\s+(is\s+)?(pending|in process|underway|proposed|requested|scheduled)|"
                        r"(pending|proposed|in-process|requested)\s+annex|annexation\s+(petition|request|application)", re.I)
INSIDE_RX = re.compile(r"(within|inside|in)\s+(the\s+)?(city|corporate)\s+limits|already annexed|has been annexed|annexed into", re.I)
OUTSIDE_RX = re.compile(r"unincorporated|outside\s+(the\s+)?(city|corporate)\s+limits|not\s+(yet\s+)?annexed", re.I)
POSSIBLE_RX = re.compile(r"(can|could|will|would)\s+be\s+annexed|eligible\s+for\s+annex|annexation\s+(is\s+)?(available|possible)|willing to annex|upon annexation", re.I)

def annexation(site, places):
    txt = " ".join(str(site.get(k) or "") for k in ("description", "features", "zoning", "setting", "address", "city"))
    snippets = [m.group(0).strip()[:220] for m in ANNEX_RX.finditer(txt)][:4]
    out = {"status": "unknown", "place": None, "geoid": None, "edge_mi": None, "nearest_city": None,
           "nearest_city_mi": None, "pending": False, "listing_text": snippets, "sources": [], "note": ""}
    tiger_src = {"label": "U.S. Census TIGERweb Incorporated Places (Jan 1, current-year vintage)",
                 "url": f"{TIGER}/query?geometry={site.get('lon')},{site.get('lat')}&geometryType=esriGeometryPoint&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=NAME,GEOID,FUNCSTAT&returnGeometry=false&f=json"}
    if site.get("lat") is not None and places:
        x, y = TO_M(site["lon"], site["lat"]); pt = Point(x, y)
        inside = None
        for p in places:
            if p["geom"].contains(pt):
                inside = p
        out["sources"].append(tiger_src)
        if inside:
            out.update(status="annexed", place=inside["name"], geoid=inside["geoid"], edge_mi=round(inside["geom"].boundary.distance(pt) / M_PER_MI, 2))
        else:
            near = min(places, key=lambda p: p["geom"].distance(pt))
            out.update(status="unincorporated", nearest_city=near["name"], nearest_city_mi=round(near["geom"].distance(pt) / M_PER_MI, 2))
        if (out["edge_mi"] is not None and out["edge_mi"] < 0.15) or (out["nearest_city_mi"] is not None and out["nearest_city_mi"] < 0.15):
            out["note"] = "Listing pin is within ~0.15 mi of a city-limit line; a large parcel may straddle the boundary. Verify with the county appraiser/GIS."
    # listing text overrides / adds
    if snippets:
        out["sources"].append({"label": "Listing text (LOIS)", "url": site.get("lois_url") or site.get("url")})
    if PENDING_RX.search(txt):
        out["pending"] = True
    if out["status"] == "unknown":
        if INSIDE_RX.search(txt): out["status"] = "annexed"; out["note"] = "From listing text only (no coordinates)."
        elif OUTSIDE_RX.search(txt): out["status"] = "unincorporated"; out["note"] = "From listing text only (no coordinates)."
    elif out["status"] == "unincorporated" and INSIDE_RX.search(txt):
        out["note"] = (out["note"] + " " if out["note"] else "") + "Listing text says the site is within city limits, but the Census boundary disagrees; annexation may be newer than the Jan 1 boundary file."
        out["listing_says_annexed"] = True
    if out["status"] == "unincorporated" and POSSIBLE_RX.search(txt):
        out["annex_possible"] = True
    return out

def load_places(cdir):
    raw = cached(cdir, "tiger_places_ks.json", fetch_places, max_age_h=24 * 7)
    out = []
    for f in raw:
        try:
            g = shp_transform(TO_M, shape(f["geometry"])).buffer(0)
        except Exception:
            continue
        a = f["properties"]
        out.append({"name": a.get("NAME"), "base": a.get("BASENAME"), "geoid": a.get("GEOID"), "geom": g})
    return raw, out

# ------------------------------------------------------------------ news
TOPIC_RX = re.compile(r"data[- ]?cent(er|re)s?|large[- ]load|hyperscale|\bAI campus|server farm|rate (case|increase|hike|review)|large power|annex", re.I)
AGGREGATORS = ("msn.com", "yahoo.com", "news.google.com", "newsbreak.com", "ground.news", "aol.com", "flipboard.com")
KS_OUTLETS = ("kansasreflector.com", "ksn.com", "kwch.com", "kake.com", "wibw.com", "ksnt.com", "kvoe.com", "lawrencekstimes.com",
              "ljworld.com", "hutchnews.com", "salinapost.com", "salina.com", "kansas.com", "cjonline.com", "emporiagazette.com",
              "gctelegram.com", "dodgeglobe.com", "parsonssun.com", "ksal.com", "kmuw.org", "tonganoxiemirror.com",
              "leavenworthtimes.com", "butlercountytimesgazette.com", "ottawaherald.com", "kansaspublicradio.org", "kcur.org",
              "kansascommerce.gov", "kcc.ks.gov", "kslegislature.gov", "kslegislature.org", "kpr.org", "ktka.com", "hppr.org",
              "themercury.com", "newtonkansan.com", "mcphersonsentinel.com", "kansasagland.com", "ksradio.net", "kfdi.com",
              "montgomery-county-chronicle.com", "coffeyvillejournal.com", "independencedailyreporter.com", "ksoutlook.com",
              "theottawaherald.com", "jocopost.org", "sunflowerstatejournal.com", "kansasleadershipcenter.org")
MO_RX = re.compile(r",\s*Mo\.|\bMissouri\b|\bMO\b|Jackson County|Independence, Mo|Clay County|Platte County|Lee'?s Summit", re.I)

def ks_cue(text, county):
    t = re.sub(r"Kansas City(,\s*Mo\.?|,\s*Missouri)?", "KC", text)
    return bool(re.search(rf"\b{re.escape(county)} County\b", text) or re.search(r"\bKansas\b|\bKan\.|,\s*KS\b|\bKCK\b|Kansas City, Kan", t))

def bing_news(q):
    r = http(BING, params={"q": q, "format": "rss", "count": 50, "setlang": "en-US", "cc": "US"}, ua=UA_WEB)
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        link = it.findtext("link") or ""
        u = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        try:
            d = dt.datetime.strptime((it.findtext("pubDate") or "")[:25].strip(), "%a, %d %b %Y %H:%M:%S").date().isoformat()
        except Exception:
            d = None
        items.append({"title": html.unescape(it.findtext("title") or "").strip(), "summary": html.unescape(re.sub("<[^>]+>", "", it.findtext("description") or "")).strip(),
                      "url": u, "date": d, "outlet": urllib.parse.urlparse(u).netloc.replace("www.", "")})
    return items

GNEWS = "https://news.google.com/rss/search"

def google_news(q):
    """Google News RSS: discovery only (links are Google redirects); publisher URL resolved via Bing title search."""
    r = http(GNEWS, params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    root = ET.fromstring(r.content); items = []
    for it in root.iter("item"):
        src = it.find("source")
        try:
            d = dt.datetime.strptime((it.findtext("pubDate") or "")[:25].strip(), "%a, %d %b %Y %H:%M:%S").date().isoformat()
        except Exception:
            d = None
        title = html.unescape(it.findtext("title") or "").strip()
        items.append({"title": title, "summary": "", "gnews": it.findtext("link"), "date": d,
                      "src_url": src.get("url") if src is not None else "", "src_name": src.text if src is not None else ""})
    return items

def resolve_gnews(it, cdir, max_age_h):
    """Find the publisher URL for a Google News headline by searching Bing for the exact title on the outlet's site."""
    t = re.sub(r"\s[-|–—]\s[^-|–—]{2,60}$", "", it["title"]).strip()
    dom = urllib.parse.urlparse(it.get("src_url") or "").netloc.replace("www.", "")
    for q in ([f'site:{dom} "{t[:90]}"'] if dom else []) + [f'"{t[:90]}"']:
        key = "res_" + hashlib.sha1(q.encode()).hexdigest()[:16] + ".json"
        try:
            res = cached(cdir, key, lambda: bing_news(q), max_age_h * 4); time.sleep(0.3)
        except Exception:
            continue
        for r in res:
            if norm_title(r["title"])[:60] == norm_title(it["title"])[:60] or norm_title(t)[:50] in norm_title(r["title"]):
                return dict(r, date=it["date"] or r["date"])
    return None

def fetch_article(url):
    r = S.get(url, timeout=20, headers={"User-Agent": UA_WEB, "Accept": "text/html"}, allow_redirects=True)
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code}
    t = r.text[:600000]
    def meta(*names):
        for n in names:
            m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]+content=["\']([^"\']+)', t, re.I) or \
                re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(n)}["\']', t, re.I)
            if m: return html.unescape(m.group(1)).strip()
        return ""
    body = re.sub(r"(?s)<(script|style|noscript)[^>]*>.*?</\1>", " ", t)
    body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    body = re.sub(r"\s+", " ", body)
    return {"ok": True, "status": 200, "final_url": r.url, "og_title": meta("og:title", "twitter:title"),
            "og_desc": meta("og:description", "description", "twitter:description"),
            "published": meta("article:published_time", "datePublished", "pubdate", "date")[:10],
            "kansas_text": body[:40000]}

def near_cue(body, name, county):
    """True if `name` appears within ~400 chars of a Kansas or '<County> County' cue (beats sidebars/footers)."""
    if not name: return True
    t = re.sub(r"Kansas City(,\s*Mo\.?|,\s*Missouri)?", "KC", body)
    cues = [m.start() for m in re.finditer(rf"\bKansas\b|\bKan\.|,\s*KS\b|\b{re.escape(county or 'zzzz')} County\b", t)]
    names = [m.start() for m in re.finditer(rf"\b{re.escape(name)}\b", t)]
    return any(abs(a - b) < 400 for a in names[:60] for b in cues[:200])

NEG_RX = re.compile(r"\b(oppos\w*|protest\w*|concern\w*|lawsuit|sues?|sued|petition\w*|outcry|fight\w*|against|push ?back|halt\w*|"
                    r"reject\w*|den(y|ied|ies)|block\w*|ban\w*|moratorium|pause\w*|stop\w*|worr\w*|anger|angry|backlash|"
                    r"recall|frustrat\w*|skeptic\w*|noise|strain|higher (bills|rates)|rate (hike|increase)|shut down|criticiz\w*)\b", re.I)
POS_RX = re.compile(r"\b(approv\w*|welcom\w*|incentive\w*|announc\w*|invest\w*|groundbreak\w*|break(s)? ground|jobs|support\w*|"
                    r"agree\w*|deal|select\w*|boost\w*|lift\w*|expand\w*|partnership|win|wins|award\w*|benefit\w*|revenue|"
                    r"opens?|launch\w*|lower (bills|rates)|rate (cut|decrease|reduction)|settlement)\b", re.I)

def classify(title, summary):
    t = f"{title}. {summary}"
    tl = title.lower()
    kind, reg = "sentiment", None
    if re.search(r"annex", t, re.I) and not re.search(r"data[- ]?cent", tl):
        kind = "annexation"
    elif re.search(r"moratori|\bban(s|ned|ning)?\b|ballot|referend|\bpause", t, re.I):
        kind = "regulatory"
        if re.search(r"\b(lift\w*|expir\w*|ends?|ended|rescind\w*|repeal\w*|let .* lapse|lapse\w*|end to)\b", tl): reg = "lifted"
        elif re.search(r"\b(approv\w*|pass\w*|adopt\w*|extend\w*|enact\w*|impos\w*|unanimous\w*|votes? (for|to (approve|adopt|extend|pause|impose))|ok'?d|ok's|hits pause|pause[sd]? |in effect|amid moratorium)\b", tl): reg = "active"
        else: reg = "proposed"
    elif re.search(r"\b(zoning|ordinance|regulations?|rules|code|setback|text amendment|conditional use|special use)\b", t, re.I) and re.search(r"data[- ]?cent", t, re.I):
        kind = "regulatory"; reg = "rules" if re.search(r"\b(approv\w*|pass\w*|adopt\w*)\b", tl) else "proposed_rules"
    elif re.search(r"rate (case|increase|hike|review|request)|large[- ]load (tariff|rate)|KCC", t, re.I):
        kind = "rate case"
    n, p = len(NEG_RX.findall(t)), len(POS_RX.findall(t))
    if kind == "regulatory":
        tag = "positive" if reg == "lifted" else "neutral" if reg in ("rules", "proposed_rules") else "negative"
    else:
        tag = "negative" if n > p else "positive" if p > n else "neutral"
    return kind, reg, tag

def norm_title(t):
    t = re.sub(r"\s[-|–—]\s[^-|–—]{2,40}$", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()[:90]

def mentions(text, name, is_city):
    """City matches must not be only '<Name> County' (Shawnee city vs Shawnee County, Leavenworth...)."""
    if not name: return True
    if is_city:
        text = re.sub(rf"\b{re.escape(name)}\s+(County|Co\.?)\b", " ", text, flags=re.I)
        return bool(re.search(rf"\b{re.escape(name)}\b", text, re.I))
    return bool(re.search(rf"\b{re.escape(name)}\s+(County|Co\b\.?)", text, re.I))

ART_PATH = None
def save_json_atomic(path, obj):
    tmp = path + ".tmp"; json.dump(obj, open(tmp, "w")); os.replace(tmp, path)

def gather_news(cdir, places_by_county, max_age_h, art_cache):
    """places_by_county: {county: set(cities)} -> (issues, searched queries by jurisdiction)."""
    Q = []  # (jur, county, city, engine, query)
    for county, cities in sorted(places_by_county.items()):
        j = f"county:{county.lower()}"
        Q += [(j, county, None, "bing", f'"{county} County" Kansas "data center"'),
              (j, county, None, "bing", f'"{county} County" Kansas moratorium OR "large load" OR hyperscale'),
              (j, county, None, "gnews", f'"{county} County" Kansas "data center"')]
        for c in sorted(cities):
            jc = f"city:{c.lower()}"
            Q += [(jc, county, c, "bing", f'"{c}" Kansas "data center"'),
                  (jc, county, c, "bing", f'"{c}" Kansas "data center" ban OR moratorium OR petition OR ballot OR lawsuit'),
                  (jc, county, c, "gnews", f'"{c}" Kansas "data center"'),
                  (jc, county, c, "bing", f'"{c}" Kansas annexation')]
    for q in ['Kansas "data center" moratorium', 'Kansas legislature "data center" bill', 'Kansas "data center" ballot',
              'Kansas Corporation Commission "large load"', 'Evergy rate case Kansas', 'Kansas "data center" sales tax exemption',
              'Kansas "data center" water', 'Kansas governor "data center"']:
        Q += [("state", None, None, "bing", q), ("state", None, None, "gnews", q)]
    known_cities = {c: county for county, cs in places_by_county.items() for c in cs}
    cand, searched, qfail = {}, {}, 0
    for jur, county, city, eng, q in Q:
        key = f"{eng}_" + hashlib.sha1(q.encode()).hexdigest()[:16] + ".json"
        try:
            items = cached(cdir, key, (lambda: bing_news(q)) if eng == "bing" else (lambda: google_news(q)), max_age_h)
            time.sleep(0.4)
        except Exception as e:
            qfail += 1; items = []; log("WARN news query failed:", eng, q, str(e)[:100])
        searched.setdefault(jur, []).append(f"{'Bing News' if eng == 'bing' else 'Google News'}: {q}")
        for it in items:
            text = f"{it['title']} {it['summary']}"
            if not TOPIC_RX.search(text): continue
            if jur != "state" and not mentions(text, city or county, bool(city)): continue
            k = (jur, norm_title(it["title"]))
            if k in cand:
                c = cand[k]
                if it.get("url") and not c.get("url"): c.update({kk: it[kk] for kk in ("url", "summary", "outlet")})
                elif it.get("url"): c.setdefault("also", []).append(it["url"])
                continue
            cand[k] = dict(it, jurisdiction=jur, county=county, city=city, query=q)
    # re-home statewide headlines that are really about one of our cities/counties
    for (jur, nt), it in list(cand.items()):
        if jur != "state": continue
        text = f"{it['title']} {it['summary']}"
        for c, county in known_cities.items():
            if mentions(text, c, True) and len(c) > 4:
                nk = (f"city:{c.lower()}", nt)
                if nk not in cand: cand[nk] = dict(it, jurisdiction=nk[0], county=county, city=c)
                del cand[(jur, nt)]; break
    out, dropped = [], {"unfetchable": 0, "not_kansas": 0, "unresolved": 0, "older_than_24mo": 0}
    cutoff = (dt.date.today() - dt.timedelta(days=730)).isoformat()

    def one(jur, it):
        """-> (issue dict, None) or (None, drop reason). Runs in a thread pool."""
        if it.get("date") and it["date"] < cutoff:
            return None, "older_than_24mo"   # scores 0 anyway; skip the network work
        if not it.get("url"):
            r = resolve_gnews(it, cdir, max_age_h)
            if not r:
                return None, "unresolved"
            it.update(url=r["url"], summary=r["summary"], outlet=r["outlet"])
        urls = [it["url"]] + [u for u in it.get("also", []) if u != it["url"]]
        urls.sort(key=lambda u: any(a in u for a in AGGREGATORS))  # publisher first
        name = it["city"] or it["county"]
        art = None
        for u in urls[:3]:
            h = hashlib.sha1(f"{u}|{name}|{it['county']}".encode()).hexdigest()[:16]
            if h in art_cache:
                if art_cache[h].get("ok"): art = art_cache[h]; it["url"] = u; break
                continue
            try:
                a = fetch_article(u)
            except Exception as e:
                a = {"ok": False, "status": repr(e)[:80]}
            if a.get("ok"):
                body = a.pop("kansas_text")
                a["near"] = near_cue(body, name, it["county"]) if jur != "state" else bool(re.search(r"\bKansas\b|\bKan\.", body))
                a["mo"] = len(MO_RX.findall(body[:8000]))
                a["fetched_at"] = dt.date.today().isoformat()
            art_cache[h] = a
            if a.get("ok"): art = a; it["url"] = u; break
            time.sleep(0.2)
        if not art:
            return None, "unfetchable"
        outlet = urllib.parse.urlparse(it["url"]).netloc.replace("www.", "")
        text = f"{it['title']} {it['summary']} {art.get('og_desc','')}"
        ks_outlet = any(o in outlet for o in KS_OUTLETS)
        if jur != "state":
            ok = (re.search(rf"\b{re.escape(it['county'])} County\b", text) or ks_cue(text, it["county"]) or
                  (art.get("near") and art.get("mo", 0) < 3) or (ks_outlet and not MO_RX.search(text)))
            if not ok or (MO_RX.search(text) and not ks_cue(text, it["county"])):
                return None, "not_kansas"
        elif not re.search(r"\bKansas\b|\bKan\.|\bKCC\b|Evergy", text):
            return None, "not_kansas"
        kind, reg, tag = classify(it["title"], it["summary"])
        summ = re.sub(r"\s+", " ", (art.get("og_desc") or it["summary"] or "").strip())
        if len(summ) > 220: summ = summ[:217].rsplit(" ", 1)[0] + "…"
        return ({"id": hashlib.sha1((it["url"] + jur).encode()).hexdigest()[:12], "jurisdiction": jur, "county": it["county"], "city": it["city"],
                    "date": it["date"] or art.get("published") or None, "title": re.sub(r"\s[-|]\s[^-|]{2,40}$", "", it["title"]),
                    "summary": summ, "url": it["url"], "outlet": outlet, "kind": kind, "reg_status": reg, "tag": tag,
                    "origin": "news (auto-tagged)", "fetched": art.get("fetched_at")}, None)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for n, (res, why) in enumerate(ex.map(lambda kv: one(kv[0][0], kv[1]), list(cand.items())), 1):
            if res: out.append(res)
            else: dropped[why] += 1
            if ART_PATH and n % 25 == 0:  # checkpoint so an interruption keeps fetched articles
                save_json_atomic(ART_PATH, dict(art_cache))
    STATUS["News search (Bing News + Google News RSS)"] = {"ok": qfail < len(Q) / 2, "detail": f"{len(Q)} queries, {qfail} failed; {len(out)} Kansas items kept; dropped {dropped['not_kansas']} non-Kansas, {dropped['unfetchable']} unfetchable, {dropped['unresolved']} unresolvable Google links, {dropped['older_than_24mo']} older than 24 months", "url": BING}
    return out, searched

MO_OUTLETS = ("komu.com", "abc17news.com", "columbiamissourian.com", "stltoday.com", "ksdk.com", "kmov.com", "newstribune.com",
              "fox2now.com", "missouriindependent.com", "kcur.org/news/missouri", "lakeexpo.com", "independencemo.gov")
def is_missouri(i):
    """Drop Missouri stories that share a Kansas place name (Independence, Montgomery County, Clay County ...)."""
    u = i["url"].lower(); t = f"{i['title']} {i.get('summary','')}"
    if "/missouri/" in u or any(o in u for o in MO_OUTLETS): return True
    if re.search(r"\b(Oklahoma|Iowa|Nebraska|Colorado|Texas|Virginia)\b", i["title"]) and not re.search(r"\bKansas\b|\bKan\.", i["title"]): return True
    return bool(re.search(r"\bMissouri\b|,\s*Mo\.", t)) and not re.search(r"\bKansas\b|\bKan\.|\bKS\b", t)

COUNTY_RX = re.compile(r"\b([A-Z][a-z]+)(?: and ([A-Z][a-z]+))? (?i:count(?:y|ies))\b")
def rehome(i, pbc):
    """Auto-tagged items are filed under the place that was searched. If the headline doesn't name that place but does
    name another tracked city or county, file it there instead (e.g. a 'Sedgwick County extends moratorium' story
    found by a Wichita query belongs to Sedgwick County, not the City of Wichita)."""
    jur = i["jurisdiction"]
    t = i["title"]
    if jur != "state":
        kind, name = jur.split(":", 1)
        if mentions(t, name.title(), kind == "city"): return [i]
    cities = {c.lower(): c for cs in pbc.values() for c in cs}
    counties = {c.lower() for c in pbc}
    hits = [f"city:{c}" for c in cities if len(c) > 4 and mentions(t, cities[c], True)]
    if not hits:
        for m in COUNTY_RX.finditer(t):
            hits += [f"county:{x.lower()}" for x in m.groups() if x and x.lower() in counties]
    if not hits: return [i]
    out = []
    for j in dict.fromkeys(hits):
        n = dict(i, jurisdiction=j, rehomed_from=jur); n["id"] = hashlib.sha1((n["url"] + j).encode()).hexdigest()[:12]
        out.append(n)
    return out

FAILED_RX = re.compile(r"\b(reject\w*|fail\w*|voted down|vote(s|d)? against|den(y|ies|ied)|declin\w*|won't|will not)\b", re.I)
def fix_failed(i):
    """'County rejects data-center ban' is not a proposed ban: mark it failed (scores 0) and neutral.
    Also: a utility rate increase is not good news for ratepayers; tag rate cases neutral unless they shield customers."""
    i["title"] = re.split(r"\s+\|\s+", i["title"])[0].rstrip(" ,")
    if i["kind"] == "rate case" and i["tag"] == "positive" and not re.search(r"shield|protect|lower|reduc|refund|reject", i["title"], re.I):
        i["tag"] = "neutral"
    if i["kind"] == "regulatory" and i.get("reg_status") == "proposed" and FAILED_RX.search(i["title"]):
        i["reg_status"] = "failed"; i["tag"] = "neutral"

# ------------------------------------------------------------------ scoring
def recency(d, today):
    if not d: return 0.3
    try: age = (today - dt.date.fromisoformat(d[:10])).days
    except Exception: return 0.3
    return 1.0 if age <= 183 else 0.6 if age <= 365 else 0.3 if age <= 730 else 0.0

def reg_status(items, today):
    """Latest moratorium/ban status for one jurisdiction from its regulatory items."""
    final = [i for i in items if i.get("final")]
    if final:
        i = max(final, key=lambda i: i.get("date") or "")
        st = i["reg_status"]
        if st == "active" and i.get("expires") and i["expires"] < today.isoformat(): st = "lifted"
        if st not in PEN_GOV: st = None   # e.g. "none" (proposal failed), "rules", "proposed_rules"
        return st, i, rules_flag(items)
    last = {}
    for i in items:
        st = i.get("reg_status")
        if st in ("active", "proposed", "lifted"):
            if st == "active" and i.get("expires") and i["expires"] < today.isoformat(): st = "lifted"
            if (i.get("date") or "") >= (last.get(st, {}).get("date") or ""): last[st] = i
    rules = rules_flag(items)
    a, l, p = (last.get(k) for k in ("active", "lifted", "proposed"))
    d = lambda x: (x or {}).get("date") or ""
    if a and (not l or d(a) > d(l)): return "active", a, rules
    if l: return ("proposed", p, rules) if p and d(p) > d(l) else ("lifted", l, rules)
    if p: return "proposed", p, rules
    return None, None, rules

def rules_flag(items):
    """'rules' if data-center zoning rules were adopted, 'proposed_rules' if restrictive rules are pending, else None."""
    st = {i.get("reg_status") for i in items}
    return "rules" if "rules" in st else "proposed_rules" if "proposed_rules" in st else None

PEN_RULES = {"governing": {"rules": 10, "proposed_rules": 5}, "secondary": {"rules": 2, "proposed_rules": 1}}
PEN_GOV = {"active": 60, "proposed": 30, "lifted": 5}
PEN_SEC = {"active": 25, "proposed": 12, "lifted": 2}
LABEL = {"active": "moratorium/ban in effect", "proposed": "moratorium/ban proposed or under consideration", "lifted": "moratorium lifted or expired"}

def score_site(site, by_jur, today):
    an = site["annexation"]; county = (site.get("county") or "").lower()
    if an["status"] == "annexed":
        gov = f"city:{(an['place_base'] or '').lower()}"; sec = [f"county:{county}"]
    else:
        gov = f"county:{county}"
        sec = [f"city:{an['nearest_base'].lower()}"] if an.get("nearest_base") and (an.get("nearest_city_mi") or 99) <= 3 else []
    jurs = [gov] + sec
    R, why_r = 100.0, []
    for j, pen, role in [(gov, PEN_GOV, "governing")] + [(s, PEN_SEC, "secondary") for s in sec]:
        items = [i for i in by_jur.get(j, []) if i["kind"] == "regulatory"]
        st, it, rules = reg_status(items, today)
        nm = j.split(":", 1)[1].title() + (" County" if j.startswith("county:") else "")
        if st:
            R -= pen[st]; why_r.append(f"−{pen[st]}: {nm} ({role}) — {LABEL[st]} (latest: {it.get('date') or 'n.d.'})")
        if rules:
            p = PEN_RULES[role][rules]
            R -= p; why_r.append(f"−{p}: {nm} ({role}) — " + ("data-center zoning rules adopted" if rules == "rules" else "data-center zoning rules proposed"))
        elif not st and any(i.get("final") and i.get("reg_status") == "none" for i in items):
            why_r.append(f"0: {nm} ({role}) — moratorium proposal failed")
    for i in by_jur.get("state", []):
        if i.get("state_effect") == "enacted_restriction": R -= 15; why_r.append(f"−15: statewide restriction — {i['title']}")
        elif i.get("state_effect") == "pending_restriction": R -= 5; why_r.append(f"−5: statewide proposal — {i['title']}")
    R = max(0.0, R)
    if not why_r: why_r = ["No moratorium, ban or restrictive rule found for " + ", ".join(j.split(':',1)[1].title() + (' County' if j.startswith('county:') else '') for j in jurs)]
    Sv, neg, why_s = 70.0, 0.0, []
    for j in jurs:
        w_j = 1.0 if j == gov else 0.5
        for i in by_jur.get(j, []):
            if i["kind"] not in ("sentiment", "rate case"): continue
            w = recency(i.get("date"), today) * w_j
            if i["tag"] == "negative": neg += 10 * w
            elif i["tag"] == "positive": Sv += 6 * w
    neg = min(neg, 60); Sv = max(0.0, min(100.0, Sv - neg))
    cnt = lambda t: sum(1 for j in jurs for i in by_jur.get(j, []) if i["kind"] in ("sentiment", "rate case") and i["tag"] == t)
    why_s.append(f"{cnt('negative')} negative, {cnt('positive')} positive, {cnt('neutral')} neutral local items (governing: {gov}{'; secondary: ' + ', '.join(sec) if sec else ''})")
    C = COMMUNITY_WEIGHTS["regulatory"] * R + COMMUNITY_WEIGHTS["sentiment"] * Sv
    issue_ids = [i["id"] for j in jurs + ["state"] for i in by_jur.get(j, [])]
    site["community"] = {"score": round(C), "regulatory": {"score": round(R), "why": why_r}, "sentiment": {"score": round(Sv), "why": why_s},
                         "jurisdictions": {"governing": gov, "secondary": sec}, "issues": issue_ids,
                         "local_issue_count": sum(1 for j in jurs for i in by_jur.get(j, []) if i["kind"] != "annexation")}
    site["infra_score"] = site["score"]
    site["overall"] = round(WEIGHTS["infrastructure"] * site["score"] + WEIGHTS["community"] * C)

# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="sites.json from scrape.py (updated in place)")
    ap.add_argument("--cache-dir", default=None, help="cache for public boundaries + news (safe to persist)")
    ap.add_argument("--curated", default=None, help="hand-verified issues JSON (plaintext, decrypted at runtime)")
    ap.add_argument("--state", default=None, help="state JSON (issue first-seen dates); updated in place")
    ap.add_argument("--news-max-age-h", type=float, default=144, help="re-query news when cache is older than this (default 6 days)")
    args = ap.parse_args()
    D = json.load(open(args.data)); sites = D["sites"]; today = dt.date.today()

    try:
        raw, places = load_places(args.cache_dir)
        STATUS["Census TIGERweb Incorporated Places"] = {"ok": True, "detail": f"{len(places)} Kansas incorporated places (Jan 1 vintage)", "url": TIGER}
    except Exception as e:
        places = []; STATUS["Census TIGERweb Incorporated Places"] = {"ok": False, "detail": str(e)[:200], "url": TIGER}
    base_of = {p["name"]: p["base"] for p in places}
    bases = {p["base"].lower(): p["base"] for p in places}
    for s in sites:
        a = annexation(s, places)
        a["place_base"] = base_of.get(a["place"]); a["nearest_base"] = base_of.get(a["nearest_city"])
        s["annexation"] = a

    # jurisdictions to search
    pbc = {}
    for s in sites:
        c = s.get("county") or ""
        if not c: continue
        cs = pbc.setdefault(c, set()); a = s["annexation"]
        if a.get("place_base"): cs.add(a["place_base"])
        if a.get("nearest_base") and (a.get("nearest_city_mi") or 99) <= 3: cs.add(a["nearest_base"])
        if (s.get("city") or "").lower() in bases: cs.add(bases[s["city"].lower()])
    art_path = os.path.join(args.cache_dir, "articles.json") if args.cache_dir else None
    art_cache = json.load(open(art_path)) if art_path and os.path.exists(art_path) else {}
    global ART_PATH; ART_PATH = art_path
    try:
        news, searched = gather_news(args.cache_dir, pbc, args.news_max_age_h, art_cache)
    finally:
        if art_path: save_json_atomic(art_path, art_cache)

    cur = {"issues": [], "exclude_urls": [], "overrides": {}}
    if args.curated and os.path.exists(args.curated):
        cur.update(json.load(open(args.curated)))
        STATUS["Curated regulatory issues (hand-verified)"] = {"ok": True, "detail": f"{len(cur['issues'])} verified items", "url": ""}
    else:
        STATUS["Curated regulatory issues (hand-verified)"] = {"ok": False, "detail": "curated file not available", "url": ""}
    excl = set(cur.get("exclude_urls", []))
    curated_urls = {i["url"] for i in cur["issues"]}
    issues = []
    for i in cur["issues"]:
        i = dict(i); i.setdefault("origin", "hand-verified"); i["id"] = hashlib.sha1((i["url"] + i["jurisdiction"]).encode()).hexdigest()[:12]
        issues.append(i)
    seen_ids = {i["id"] for i in issues}
    for i0 in news:
        i0["title"] = re.split(r"\s+\|\s+", i0["title"])[0].rstrip(" ,")
        if i0["url"] in excl or i0["url"] in curated_urls or is_missouri(i0): continue
        for i in rehome(i0, pbc):
            fix_failed(i)
            ov = cur.get("overrides", {}).get(i["url"])
            if ov: i.update(ov); i["origin"] = "news (tag hand-checked)"
            if i["id"] in seen_ids: continue
            seen_ids.add(i["id"]); issues.append(i)

    # first-seen tracking for issues (for the weekly report)
    state = json.load(open(args.state)) if args.state and os.path.exists(args.state) else {}
    seen = state.setdefault("issues", {})
    for i in issues:
        i["first_seen"] = seen.setdefault(i["id"], today.isoformat())
    if args.state: json.dump(state, open(args.state, "w"), indent=1)

    by_jur = {}
    for i in issues: by_jur.setdefault(i["jurisdiction"], []).append(i)
    for v in by_jur.values(): v.sort(key=lambda i: i.get("date") or "", reverse=True)
    for s in sites:
        score_site(s, by_jur, today)
        g = s["community"]["jurisdictions"]
        s["community"]["searched"] = [q for j in [g["governing"]] + g["secondary"] + ["state"] for q in searched.get(j, [])]
        an = s["annexation"]
        an["news"] = [i["id"] for j in [g["governing"]] + g["secondary"] for i in by_jur.get(j, []) if i["kind"] == "annexation"][:8]
        if any(by_id_kind_pending(i, s) for i in by_jur.get(g["governing"], []) + sum((by_jur.get(x, []) for x in g["secondary"]), [])):
            an["pending"] = True
    sites.sort(key=lambda s: -s["overall"])
    used = {x for s in sites for x in s["community"]["issues"] + s["annexation"]["news"]}
    D["issues"] = {i["id"]: i for i in issues if i["id"] in used}
    D["community_rules"] = COMMUNITY_RULES
    D["weights"] = {"overall": WEIGHTS, "community": COMMUNITY_WEIGHTS}
    D["sources"].update(STATUS)
    D["annexation_counts"] = {k: sum(1 for s in sites if s["annexation"]["status"] == k) for k in ("annexed", "unincorporated", "unknown")}
    D["annexation_counts"]["pending"] = sum(1 for s in sites if s["annexation"]["pending"])
    D["annexation_counts"]["listing_says_annexed"] = sum(1 for s in sites if s["annexation"].get("listing_says_annexed"))
    json.dump(D, open(args.data, "w"), indent=1)
    log(f"updated {args.data}: {len(D['issues'])} issues referenced; annexation {D['annexation_counts']}")
    for k, v in STATUS.items():
        log(("OK  " if v["ok"] else "FAIL"), k, "-", v["detail"])

def by_id_kind_pending(i, s):
    """A hand-verified annexation item naming this site marks it pending."""
    return i.get("kind") == "annexation" and i.get("annex_pending_site") in (s["id"], s["name"])

if __name__ == "__main__":
    main()
