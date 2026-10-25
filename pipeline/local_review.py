#!/usr/bin/env python3
"""Local review for the community score: economic-development (EDC) sites and data-center laws & zoning.

EDC   - for each governing jurisdiction, crawl its economic-development organization's site (homepage plus up to
        MAX_PAGES same-site pages whose links look like incentives / sites / industries / utilities / data / tech) and
        record verbatim sentences that show (a) marketing to data-center / large-load users, (b) marketing to tech or
        industrial users, (c) incentives (IRBs, tax abatements, Opportunity Zones, ...), (d) a published data-center or
        large-load policy. Every finding keeps the page URL and title it came from.
LAWS  - the hand-curated zoning file (status per jurisdiction + citations with verbatim quotes). Each run re-fetches the
        cited pages at most weekly and records whether the quote is still on the page (verified / not re-verified).

Both are cached in --cache-dir (EDC pages 7 days, law citations 7 days), so daily runs are cheap and the slow lookups
refresh weekly. Nothing here invents a finding: no page fetched -> "not reviewed"; no matching sentence -> no finding.
The curated file is plaintext only at runtime (decrypted into a temp dir in CI) and is never committed.
"""
import datetime as dt, hashlib, json, os, re, sys, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor
import requests, warnings
from bs4 import XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_PAGES = 8
EDC_MAX_AGE_H = 24 * 7
LAW_MAX_AGE_H = 24 * 7
LINK_RX = re.compile(r"incentiv|site|propert|build|industr|data|tech|utilit|power|energy|business|why|advantage|tax|abate|irb|bond|opportunity|zone|program|financ|target|sector|develop", re.I)
SKIP_RX = re.compile(r"\.(jpg|jpeg|png|gif|zip|docx?|xlsx?|mp4)$|mailto:|tel:|javascript:|facebook|twitter|linkedin|instagram|youtube|calendar|event|news/|blog/|login|wp-admin|#", re.I)
CAT = {
    "data_center": re.compile(r"data[- ]?cent(er|re)s?|hyperscale|large[- ]load|mission[- ]critical|server farm|co-?location|megawatt|\bMW\b", re.I),
    "tech":        re.compile(r"(technology|tech) (compan|firm|park|campus|sector|industr|employer|cluster|corridor)|advanced manufactur|aerospace|information technology|digital infrastructure|fiber[- ]optic|dark fiber|semiconductor|biotech|bioscience|animal health", re.I),
    "incentive":   re.compile(r"industrial revenue bond|\bIRBs?\b|tax abatement|property tax (abatement|exemption)|abatement|opportunity zone|\bPEAK\b|HPIP|STAR bond|tax increment|\bTIF\b|enterprise zone|sales tax exemption|rural opportunity zone|\bCDBG\b|neighborhood revitalization", re.I),
    "policy":      re.compile(r"data[- ]?cent(er|re)s?.{0,80}(policy|polic(ies)|guidelines|criteria|standards|regulation)|(policy|guidelines|criteria|standards).{0,80}data[- ]?cent(er|re)s?|large[- ]load.{0,60}(tariff|policy|rate)", re.I),
}
INCENTIVE_TYPES = [("IRB", r"industrial revenue bond|\bIRBs?\b"), ("Tax abatement", r"abatement|property tax exemption"),
                   ("Opportunity Zone", r"opportunity zone"), ("PEAK / HPIP (state)", r"\bPEAK\b|HPIP"), ("TIF / STAR bonds", r"tax increment|\bTIF\b|STAR bond"),
                   ("Neighborhood revitalization", r"neighborhood revitalization"), ("Sales tax exemption", r"sales tax exemption"), ("Enterprise zone", r"enterprise zone")]
S = requests.Session(); S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

def log(*a): print("[local_review]", *a, file=sys.stderr, flush=True)

def _cache(cdir, key, fn, max_age_h):
    if not cdir: return fn()
    os.makedirs(cdir, exist_ok=True); p = os.path.join(cdir, key)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
        try: return json.load(open(p))
        except Exception: pass
    v = fn(); json.dump(v, open(p + ".tmp", "w")); os.replace(p + ".tmp", p); return v

def fetch(url, timeout=25):
    """-> {ok, url, status, title, text, links} ; PDFs via pdftotext when available."""
    try:
        r = S.get(url, timeout=timeout, allow_redirects=True)
    except Exception as e:
        return {"ok": False, "url": url, "status": repr(e)[:80]}
    out = {"ok": r.status_code == 200, "url": r.url, "status": r.status_code}
    if not out["ok"]: return out
    if r.content[:4] == b"%PDF":
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(r.content); f.flush()
            try: out["text"] = subprocess.run(["pdftotext", f.name, "-"], capture_output=True, text=True, timeout=60).stdout
            except Exception: out["text"] = ""
        out["text"] = re.sub(r"(?m)^\s*\d{1,3}\s*$", "", out["text"])  # strip ordinance line numbers
        out.update(title=os.path.basename(urllib.parse.urlparse(r.url).path), links=[]); return out
    soup = BeautifulSoup(r.text, "html.parser")
    out["title"] = re.sub(r"\s+", " ", soup.title.get_text()).strip()[:160] if soup.title else ""
    out["links"] = []
    for a in soup.find_all("a", href=True):
        out["links"].append([urllib.parse.urljoin(r.url, a["href"]).split("#")[0], re.sub(r"\s+", " ", a.get_text(" ")).strip()[:80]])
    for t in soup(["script", "style", "noscript", "svg", "form"]): t.decompose()
    for t in soup.select("nav, footer, header"): t.decompose()
    out["text"] = re.sub(r"[ \t\r\f\v]+", " ", soup.get_text("\n"))
    return out

def sentences(text):
    t = re.sub(r"\s*\n\s*", "\n", text or "")
    for blk in t.split("\n"):
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", blk.strip()):
            s = s.strip(" •-–|")
            if 25 <= len(s) <= 400 and len(s.split()) >= 5: yield s

# ------------------------------------------------------------------ EDC
def crawl_edc(org):
    """org: {name, url} -> raw crawl {pages:[{url,title,text}]}"""
    home = fetch(org["url"])
    pages = [home] if home.get("ok") else []
    if home.get("ok"):
        dom = urllib.parse.urlparse(home["url"]).netloc.replace("www.", "")
        seen, cand = {home["url"].rstrip("/")}, []
        for u, txt in home.get("links", []):
            if urllib.parse.urlparse(u).netloc.replace("www.", "") != dom or SKIP_RX.search(u): continue
            k = u.rstrip("/")
            if k in seen: continue
            if LINK_RX.search(u) or LINK_RX.search(txt):
                seen.add(k)
                score = (3 if re.search(r"incentiv|data|site|propert|utilit|power|abate|irb|opportunity", u + " " + txt, re.I) else 1)
                cand.append((score, u))
        cand.sort(key=lambda x: -x[0])
        for _, u in cand[:MAX_PAGES]:
            p = fetch(u)
            if p.get("ok"): pages.append(p)
            time.sleep(0.3)
    return {"home_status": home.get("status"), "fetched_at": dt.date.today().isoformat(),
            "pages": [{"url": p["url"], "title": p.get("title", ""), "text": (p.get("text") or "")[:60000]} for p in pages]}

def review_edc(org, raw):
    CHALLENGE = re.compile(r"^(one moment|just a moment|attention required|access denied|checking your browser)", re.I)
    if raw["pages"] and CHALLENGE.search(raw["pages"][0].get("title", "")) and len(raw["pages"]) == 1:
        raw = dict(raw, pages=[], home_status=403)  # bot-challenge interstitial, not the real site
    if not raw["pages"]:
        return {"org": org.get("name"), "url": org["url"], "reviewed": False, "status": ("site blocks automated fetch" if raw.get("home_status") in (202, 403, 429) else f"site not reachable ({str(raw.get('home_status'))[:40]})"),
                "fetched": raw.get("fetched_at"), "pages_reviewed": 0}
    find = {k: [] for k in CAT}; inc_types = {}
    for p in raw["pages"]:
        seen = set()
        for s in sentences(p["text"]):
            key = s.lower()[:120]
            if key in seen: continue
            seen.add(key)
            for k, rx in CAT.items():
                if k == "tech" and CAT["data_center"].search(s): continue
                if rx.search(s) and len(find[k]) < 6 and not any(s == f["quote"] for f in find[k]):
                    if k == "data_center" and re.search(r"\bMW\b|megawatt", s, re.I) and not re.search(r"data|load|power|electric|substation|capacity", s, re.I): continue
                    find[k].append({"quote": s, "url": p["url"], "title": p["title"]})
                    if k == "incentive":
                        for nm, r in INCENTIVE_TYPES:
                            if re.search(r, s, re.I): inc_types.setdefault(nm, {"quote": s, "url": p["url"], "title": p["title"]})
    return {"org": org.get("name") or raw["pages"][0]["title"], "url": org["url"], "reviewed": True, "fetched": raw["fetched_at"],
            "pages_reviewed": len(raw["pages"]), "pages": [{"url": p["url"], "title": p["title"]} for p in raw["pages"]],
            "markets_data_centers": bool(find["data_center"]), "markets_tech": bool(find["tech"]),
            "incentives": [dict(v, type=k) for k, v in inc_types.items()], "dc_policy": bool(find["policy"]),
            "findings": find}

def edc_score(r):
    """0-100. Not reviewed -> 40 (unknown). Reviewed: base 50, +15 data-center/large-load marketing, +10 tech/industry
    marketing, +5 per incentive type (max 15), +10 published data-center/large-load policy."""
    if not r or not r.get("reviewed"): return 40, ["EDC site not reviewed (unreachable or none identified): 40"]
    sc, why = 50, ["EDC site reviewed: base 50"]
    if r["markets_data_centers"]: sc += 15; why.append("+15 markets to data-center / large-load users")
    if r["markets_tech"]: sc += 10; why.append("+10 markets to tech / advanced-industry users")
    n = min(3, len(r["incentives"]))
    if n: sc += 5 * n; why.append(f"+{5*n} lists incentives ({', '.join(i['type'] for i in r['incentives'][:3])})")
    if r["dc_policy"]: sc += 10; why.append("+10 published data-center / large-load policy language")
    return min(100, sc), why

# ------------------------------------------------------------------ LAWS
def norm(s): return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def quote_on_page(q, text):
    """Quote present verbatim (normalized), or >=80% of its 6-word shingles present (PDF line breaks, hyphenation)."""
    q = norm(q)
    if q[:160] in text: return True
    w = q.split(); sh = [" ".join(w[i:i + 6]) for i in range(max(1, len(w) - 5))]
    return bool(sh) and sum(1 for x in sh if x in text) / len(sh) >= 0.8

def verify_citations(laws, cdir):
    """Re-fetch each cited page (cached weekly) and check the curated quote is still present."""
    cites = {c["url"]: c for j in laws.values() for c in j.get("citations", [])}
    def one(u):
        key = "law_" + hashlib.sha1(u.encode()).hexdigest()[:16] + ".json"
        def go():
            p = fetch(u, timeout=40)
            return {"ok": p.get("ok"), "status": p.get("status"), "text": norm(re.sub(r"-\n(?=[a-z])", "", p.get("text", "")))[:600000], "at": dt.date.today().isoformat()}
        try: return u, _cache(cdir, key, go, LAW_MAX_AGE_H)
        except Exception as e: return u, {"ok": False, "status": repr(e)[:60]}
    with ThreadPoolExecutor(8) as ex: res = dict(ex.map(one, list(cites)))
    stats = {"verified": 0, "unreachable": 0, "quote_missing": 0}
    for j in laws.values():
        for c in j.get("citations", []):
            r = res.get(c["url"], {})
            if len(c.get("quote") or "") < 30:
                c["quote_display"] = False
                c["check"] = ("page reachable" if r.get("ok") else f"not re-verified (fetch {r.get('status')})"); c["checked"] = r.get("at", ""); stats["verified" if r.get("ok") else "unreachable"] += 1
            elif r.get("ok") and quote_on_page(c["quote"], r.get("text", "")):
                c["check"] = "verified"; c["checked"] = r["at"]; stats["verified"] += 1
            elif r.get("ok") and len(r.get("text", "")) < 200:
                c["check"] = f"not re-verified (no text layer on latest fetch); verified when curated {c.get('verified', '')}"; c["checked"] = r["at"]; stats["unreachable"] += 1
            elif r.get("ok") and c.get("via") == "search index excerpt":
                c["check"] = f"verified via search index {c.get('verified', '')} (page body not readable by direct fetch)"; c["checked"] = r["at"]; stats["unreachable"] += 1
            elif r.get("ok"):
                c["check"] = "quote not found on latest fetch (page may have changed or is script-rendered)" + (f"; verified when curated {c['verified']}" if c.get("verified") else ""); c["checked"] = r["at"]; stats["quote_missing"] += 1
            else:
                c["check"] = f"not re-verified (fetch {r.get('status')}); verified when curated {c.get('verified', '')}"; stats["unreachable"] += 1
    return stats

LAW_ADJ = {"by_right": (0, "data centers allowed by right in at least one district"),
           "cup": (-10, "data centers need a conditional/special use permit (public hearing)"),
           "proposed": (-5, "data-center rules being drafted (outcome uncertain)"),
           "not_defined": (-5, "data centers not defined in the zoning code (case-by-case)"),
           "prohibited": (-50, "data centers prohibited"),
           "unknown": (-5, "no data-center zoning provision found in sources reviewed")}

def run(sites, jurs_by_site, review_file, cdir):
    """-> (edc results by jurisdiction, laws by jurisdiction, status dict)"""
    R = json.load(open(review_file)) if review_file and os.path.exists(review_file) else {"edc": {}, "laws": {}}
    edc_orgs, laws = R.get("edc", {}), R.get("laws", {})
    need = sorted({j for js in jurs_by_site.values() for j in js if j in edc_orgs})
    def one(j):
        org = edc_orgs[j]
        key = "edc_" + hashlib.sha1(org["url"].encode()).hexdigest()[:16] + ".json"
        try: raw = _cache(cdir, key, lambda: crawl_edc(org), EDC_MAX_AGE_H)
        except Exception as e: raw = {"pages": [], "home_status": repr(e)[:60]}
        return j, review_edc(org, raw)
    with ThreadPoolExecutor(6) as ex: edc = dict(ex.map(one, need))
    st = verify_citations(laws, cdir) if laws else {}
    status = {"EDC site review": {"ok": bool(edc) and sum(1 for v in edc.values() if v["reviewed"]) >= len(edc) * 0.6,
                                  "detail": f"{sum(1 for v in edc.values() if v['reviewed'])}/{len(edc)} EDC sites reviewed (refreshed weekly)", "url": ""},
              "Laws & zoning citations": {"ok": bool(laws), "detail": (f"{len(laws)} jurisdictions; citations: {st}" if laws else "curated zoning file not available"), "url": ""}}
    return edc, laws, status
