#!/usr/bin/env python3
"""Local-government stance from public data-center trackers and designations (Powered Land view).

Runs after community.py. For every Kansas entry on morethanjustparks.com's Data Center Tracker
(state page), it captures the popup detail, cites the ORIGINAL source the tracker links (the tracker
page is cited second), applies a hand review (encrypted: tracker_review.enc) that classifies the
entry from the source's content, and adjusts each site's Laws & zoning score (R):

  formal corridor / data-center overlay ... +20 governing / +10 secondary (+12 / +6 when the site's
                                            position inside the boundary can't be confirmed — "approximate")
  government support, approved ............ +10 / +5   (vote, option/purchase agreement, partnership, PILOT)
  government support, engaged ............. +6 / +3    (negotiations, letter of intent)
  permitted precedent (operating/under construction) +3 / +1 (not credited where a moratorium is in force)
  moratorium not already scored ........... −60 / −25 active, −30 / −12 proposed, −5 / −2 lifted
  government refusal (e.g. incentives denied) −8 / −4
  opposition not already scored ........... −5 / −2
  market interest / context ............... 0

Per jurisdiction only the strongest positive and the strongest negative count (no stacking); governing
and secondary jurisdictions are summed; the site total is capped at +20 / −30. Never double-counted:
negative signals already covered by a curated regulatory item or a news item score 0, and when a
credited signal's source is already a local-news item, that item is removed from the news tally.
If the tracker can't be fetched the last good pull (tracker_state) is used.
"""
import argparse, datetime as dt, json, math, os, re, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BASE = "https://morethanjustparks.com"
STATE_PAGE = BASE + "/data-center-tracker/state/kansas"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
POPUP_KEYS = ["name", "company", "status", "capacity_mw", "pin_town", "county", "lat", "lng", "date_filed", "editorial_summary",
              "community_opposition_level", "community_opposition_details", "water_usage_mgd", "power_source", "jobs_estimate",
              "tax_incentives_value", "land_use_acres", "source_url", "verification_source_name", "verified", "field_verified_at"]

WEIGHTS = {"corridor": (20, 10), "corridor_approx": (12, 6), "gov_support:approved": (10, 5), "gov_support:engaged": (6, 3),
           "precedent": (3, 1), "gov_refusal": (-8, -4), "opposition": (-5, -2),
           "moratorium:active": (-60, -25), "moratorium:proposed": (-30, -12), "moratorium:lifted": (-5, -2)}
CAP_POS, CAP_NEG = 20, -30
RULES_DOC = ("Local-government stance (tracker & designations). Every Kansas entry on the More Than Just Parks Data Center Tracker "
             "is read at its original source (cited first; the tracker page second) and adjusts Laws & zoning: formal data-center "
             "corridor/overlay +20 governing / +10 secondary (+12 / +6 approximate, i.e. matched by city/county because the boundary "
             "isn't published per parcel); government support approved (vote, option or purchase agreement, partnership, PILOT) "
             "+10 / +5; engaged (negotiations, letter of intent) +6 / +3; permitted precedent (operating or under-construction data "
             "center, not credited where a moratorium is in force) +3 / +1; moratorium not already scored −60/−25 active, −30/−12 "
             "proposed, −5/−2 lifted; refusal (e.g. incentives denied) −8 / −4; opposition not already scored −5 / −2; market "
             "interest/context 0. Per jurisdiction only the strongest positive and strongest negative count; governing + secondary "
             "are summed; site total capped at +20 / −30, and Laws & zoning stays within 0–100 (the adjustment is applied to the "
             "unfloored total, so a site far below 0 may not move). Never double-counted: a negative signal already covered by a "
             "curated regulatory or news item scores 0, and a credited signal whose source is already a local-news item is removed "
             "from the news tally.")

def log(*a): print("[tracker]", *a, flush=True)

# ---------------------------------------------------------------- fetch
def get(u, timeout=40):
    return urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=timeout).read().decode("utf-8", "ignore")

def payload(t):
    ch = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', t, re.S)
    return "".join(json.loads('"' + c + '"') for c in ch)

def obj_at(p, i):
    d, j = 0, i
    while j > 0:
        c = p[j]
        if c == "}": d += 1
        elif c == "{":
            if d == 0: break
            d -= 1
        j -= 1
    d, k, ins, esc = 0, j, False, False
    while k < len(p):
        c = p[k]
        if ins:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': ins = False
        else:
            if c == '"': ins = True
            elif c == "{": d += 1
            elif c == "}":
                d -= 1
                if d == 0: return json.loads(p[j:k + 1])
        k += 1

def detail(path):
    u = BASE + path
    p = payload(get(u)); fid = path.split("--")[-1]
    best = None
    for m in re.finditer(r'"id":"%s"' % re.escape(fid), p):
        try: o = obj_at(p, m.start())
        except Exception: continue
        if o and "source_url" in o and (best is None or len(o) > len(best)): best = o
    if best: best["_page"] = u
    return best

def fetch(state, max_refresh=15, max_age_days=7, sleep=1.5):
    """Refresh the Kansas tracker entries into state; returns (state, status dict). Falls back to state on failure."""
    ents = state.setdefault("entries", {})
    try:
        links = sorted(set(re.findall(r'href="(/data-center-tracker/[^"]*?-ks--[0-9a-f-]{36})"', get(STATE_PAGE))))
    except Exception as e:
        log("state page failed:", e)
        return state, {"ok": False, "detail": f"tracker fetch failed ({type(e).__name__}); using last good pull of {len(ents)} entries from {state.get('fetched', 'n/a')}", "url": STATE_PAGE}
    if not links:
        return state, {"ok": False, "detail": f"tracker page returned no Kansas entries; using last good pull ({len(ents)} entries)", "url": STATE_PAGE}
    today = dt.date.today()
    def stale(l):
        e = ents.get(l.split("--")[-1][:8])
        if not e: return (0, "")
        f = e.get("_fetched") or "1970-01-01"
        return (1, f) if (today - dt.date.fromisoformat(f)).days >= max_age_days else None
    todo = sorted([l for l in links if stale(l) is not None], key=stale)[:max_refresh]
    ok = fail = 0
    for l in todo:
        try:
            o = detail(l)
            if not o: raise ValueError("no record")
            rec = {k: o.get(k) for k in POPUP_KEYS}
            id8 = o["id"][:8]
            rec.update({"id": o["id"], "_page": o["_page"], "_fetched": today.isoformat(), "_src_title": (ents.get(id8) or {}).get("_src_title")})
            ents[id8] = rec; ok += 1
        except Exception as e:
            fail += 1; log("detail failed", l, type(e).__name__)
        time.sleep(sleep)
    live = {l.split("--")[-1][:8] for l in links}
    for k, v in ents.items(): v["_listed"] = k in live
    state["fetched"] = today.isoformat(); state["state_page"] = STATE_PAGE
    return state, {"ok": True, "detail": f"{len(links)} Kansas entries listed; {ok} detail pages refreshed, {fail} failed (cached copies used)", "url": STATE_PAGE}

# ---------------------------------------------------------------- scoring helpers
def nurl(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u); u = re.sub(r"^www\d?\.", "", u)
    u = u.split("#")[0].split("?")[0]
    return u.rstrip("/")

def miles(a, b, c, d):
    try:
        R = 3958.8; p1, p2 = math.radians(a), math.radians(c)
        h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2
        return 2 * R * math.asin(math.sqrt(h))
    except Exception: return None

def recency(d, today):
    if not d: return 0.3
    try: age = (today - dt.date.fromisoformat(d[:10])).days
    except Exception: return 0.3
    return 1.0 if age <= 183 else 0.6 if age <= 365 else 0.3 if age <= 730 else 0.0

def jname(j):
    k, n = j.split(":", 1)
    return n.title() + (" County" if k == "county" else "")

PEN_RULES = {"governing": 10, "secondary": 2}

def reg_by_jur(issues):
    out = {}
    for i in issues.values():
        if i.get("kind") == "regulatory": out.setdefault(i.get("jurisdiction"), []).append(i)
    return out

def mor_active(items, today):
    """(latest moratorium status, rules flag) for one jurisdiction — same logic as community.reg_status."""
    st_set = {i.get("reg_status") for i in items}
    rules = "rules" if "rules" in st_set else "proposed_rules" if "proposed_rules" in st_set else None
    exp = lambda i, st: "lifted" if st == "active" and i.get("expires") and i["expires"] < today.isoformat() else st
    final = [i for i in items if i.get("final")]
    if final:
        i = max(final, key=lambda i: i.get("date") or "")
        st = exp(i, i.get("reg_status"))
        return (st if st in ("active", "proposed", "lifted") else None), rules
    last = {}
    for i in items:
        st = exp(i, i.get("reg_status"))
        if st in ("active", "proposed", "lifted") and (i.get("date") or "") >= (last.get(st, {}).get("date") or ""): last[st] = i
    d = lambda x: (x or {}).get("date") or ""
    a, l, p = (last.get(k) for k in ("active", "lifted", "proposed"))
    if a and (not l or d(a) > d(l)): return "active", rules
    if l: return ("proposed" if p and d(p) > d(l) else "lifted"), rules
    return ("proposed" if p else None), rules

def build_items(state, review):
    rv = {r["id8"]: r for r in review.get("entries", [])}
    items = []
    for id8, e in sorted(state.get("entries", {}).items()):
        r = rv.get(id8)
        if not r:
            jur = [f"city:{e['pin_town'].lower()}"] if e.get("pin_town") else ([f"county:{e['county'].lower()}"] if e.get("county") else [])
            r = {"id8": id8, "signal": "unreviewed", "jurisdictions": jur, "summary": (e.get("editorial_summary") or "").strip()[:240] or
                 f"{e.get('name')}: listed as {e.get('status')} — not yet reviewed (scored 0)", "also": [], "risks": [], "angles": [], "officials": []}
        orig = r.get("original") or {"url": e.get("source_url"), "outlet": e.get("verification_source_name"), "title": e.get("_src_title") or "", "date": ""}
        items.append({"id8": id8, "name": e.get("name"), "status": e.get("status"), "tracker_url": e.get("_page"),
                      "listed": e.get("_listed", True), "lat": e.get("lat"), "lng": e.get("lng"),
                      "popup": {k: e.get(k) for k in POPUP_KEYS if e.get(k) not in (None, "", [], {})},
                      "original": orig, "also": r.get("also", []), "summary": r.get("summary"), "signal": r["signal"],
                      "strength": r.get("strength"), "jurisdictions": r.get("jurisdictions", []), "approximate": r.get("approximate", True),
                      "supersedes_rules": r.get("supersedes_rules", False), "already_counted": r.get("already_counted"),
                      "moratorium_status": r.get("moratorium_status"), "note": r.get("note", ""), "risks": r.get("risks", []),
                      "angles": r.get("angles", []), "officials": r.get("officials", [])})
    return items

SIG_LABEL = {"corridor": "data-center corridor/overlay", "gov_support": "government support", "precedent": "permitted data center",
             "moratorium": "moratorium", "gov_refusal": "government refusal", "opposition": "opposition", "context": "context",
             "unreviewed": "not yet reviewed"}

def value(it, j, role, reg, today):
    """(points, reason) for one tracker item in one jurisdiction/role."""
    g = 0 if role == "governing" else 1
    s = it["signal"]
    if s == "corridor":
        v = WEIGHTS["corridor_approx" if it["approximate"] else "corridor"][g]
        extra = ""
        if it.get("supersedes_rules"):
            st, rules = mor_active(reg.get(j, []), today)
            if rules == "rules":
                v += PEN_RULES[role]; extra = f" (incl. +{PEN_RULES[role]} reversing the 'zoning rules adopted' penalty — same ordinance, counted once)"
        return v, ("approximate match by jurisdiction" if it["approximate"] else "site inside the designated area") + extra
    if s == "gov_support":
        return WEIGHTS["gov_support:" + (it.get("strength") or "engaged")][g], it.get("strength") or "engaged"
    if s == "precedent":
        st, _ = mor_active(reg.get(j, []), today)
        if st == "active": return 0, "not credited — a moratorium is in force here"
        return WEIGHTS["precedent"][g], ""
    if s == "moratorium":
        if it.get("already_counted") or reg.get(j): return 0, "already scored from curated regulatory items"
        return WEIGHTS["moratorium:" + (it.get("moratorium_status") or "proposed")][g], it.get("moratorium_status") or "proposed"
    if s == "gov_refusal":
        return WEIGHTS["gov_refusal"][g], ""
    if s == "opposition":
        if it.get("already_counted"): return 0, f"already counted in {it['already_counted']}"
        return WEIGHTS["opposition"][g], ""
    if it.get("already_counted"): return 0, f"already counted in {'local news' if it['already_counted'] == 'news' else 'Laws & zoning (curated items)'}"
    return 0, "context only (no government action)" if s == "context" else "not yet reviewed"

def raw_R(why):
    """Unfloored Laws & zoning total from community.py's itemised lines (100 + signed deltas)."""
    tot = 100.0
    for w in why:
        m = re.match(r"^([−+-])(\d+(?:\.\d+)?):", w)
        if m: tot += (-1 if m.group(1) in "−-" else 1) * float(m.group(2))
    return tot

def apply(D, items, today):
    issues = D.get("issues") or {}
    reg = reg_by_jur(issues)
    W = D.get("weights") or {}
    wi = (W.get("overall") or {}).get("infrastructure", 0.6); wc = (W.get("overall") or {}).get("community", 0.4)
    by_jur = {}
    for it in items:
        for j in it["jurisdictions"]: by_jur.setdefault(j, []).append(it)
    touched = 0
    for s in D["sites"]:
        c = s.get("community")
        if not c: continue
        base = c.get("base")
        if not base:
            base = c["base"] = {"R": c["laws"]["score"], "R_raw": raw_R(c["laws"]["why"]), "Sv": c["news"]["score"], "C": c["score"],
                                "overall": s.get("overall"), "laws_why": list(c["laws"]["why"]), "news_why": list(c["news"]["why"])}
        gov = c["jurisdictions"]["governing"]; sec = c["jurisdictions"]["secondary"]
        lines, applied, total, excl = [], [], 0, set()
        for j, role in [(gov, "governing")] + [(x, "secondary") for x in sec]:
            cand = []
            for it in by_jur.get(j, []):
                v, why = value(it, j, role, reg, today)
                d = miles(s.get("lat"), s.get("lon"), it.get("lat"), it.get("lng")) if it.get("lat") is not None else None
                cand.append((v, why, it, d))
            if not cand: continue
            pos = max((x for x in cand if x[0] > 0), key=lambda x: x[0], default=None)
            neg = min((x for x in cand if x[0] < 0), key=lambda x: x[0], default=None)
            for v, why, it, d in cand:
                eff = v if ((pos and it is pos[2]) or (neg and it is neg[2])) else 0
                if v != 0 and eff == 0: why = (why + "; " if why else "") + "a stronger signal in this jurisdiction already counts"
                applied.append({"id8": it["id8"], "name": it["name"], "jurisdiction": j, "role": role, "signal": it["signal"],
                                "label": SIG_LABEL.get(it["signal"], it["signal"]), "points": eff, "reason": why,
                                "approximate": bool(it["signal"] == "corridor" and it["approximate"]) or it["signal"] != "corridor",
                                "distance_mi": round(d, 1) if d is not None else None, "summary": it["summary"], "original": it["original"],
                                "also": it["also"], "tracker_url": it["tracker_url"], "status": it["status"]})
                if eff:
                    total += eff
                    src = it["original"].get("outlet") or ""
                    lines.append(f"{'+' if eff > 0 else '−'}{abs(eff)}: {jname(j)} ({role}) — {SIG_LABEL[it['signal']]}: {it['name']} [{src}]")
                    for u in [it["original"].get("url")] + [a.get("url") for a in it["also"]]:
                        if u: excl.add(nurl(u))
        cap = max(CAP_NEG, min(CAP_POS, total))
        if cap != total: lines.append(f"(stance total {total:+d} capped at {cap:+d})")
        R = max(0.0, min(100.0, base["R_raw"] + cap)) if lines else base["R"]
        if base["R_raw"] + cap > 100 and cap > 0: lines.append(f"(Laws & zoning capped at 100 — only +{max(0, round(100 - base['R_raw']))} took effect)") if base["R_raw"] < 100 else lines.append("(Laws & zoning already at 100 — no further gain)")
        if base["R_raw"] + cap < 0 and cap > 0: lines.append("(Laws & zoning stays at the 0 floor — penalties outweigh this)")
        # news: drop items already credited above (counted once)
        news_items = [issues[i] for i in c["news"].get("items", []) if i in issues]
        moved = [i for i in news_items if nurl(i.get("url")) in excl]
        Sv = c["base"]["Sv"]
        if moved:
            Sv, neg = 70.0, 0.0
            for i in news_items:
                if i in moved: continue
                w = recency(i.get("date"), today) * (1.0 if i.get("jurisdiction") == gov else 0.5)
                if i["tag"] == "negative": neg += 10 * w
                elif i["tag"] == "positive": Sv += 6 * w
            Sv = max(0.0, min(100.0, Sv - min(neg, 60) + c["news"].get("manual_adj", 0)))
        E = c["edc"]["score"]
        if not lines and not moved:  # nothing applies — keep community.py's numbers exactly (no rounding drift)
            c["laws"]["score"], c["news"]["score"], c["score"], s["overall"] = base["R"], base["Sv"], base["C"], base["overall"]
            c["laws"]["why"], c["news"]["why"] = list(base["laws_why"]), list(base["news_why"])
            c["news"]["moved_to_stance"] = []
            c["stance"] = {"points": 0, "corridor": False, "items": applied, "badge": None}
            c["laws"]["tracker"] = applied
            if applied: touched += 1
            continue
        C = 0.45 * R + 0.30 * Sv + 0.25 * E
        c["laws"]["score"] = round(R); c["laws"]["why"] = base["laws_why"] + lines
        c["news"]["score"] = round(Sv)
        c["news"]["why"] = base["news_why"] + ([f"{len(moved)} item(s) not counted here because the same source is credited under Local-government stance: " + "; ".join(i['title'][:70] for i in moved)] if moved else [])
        c["news"]["moved_to_stance"] = [i["id"] for i in moved]
        c["score"] = round(C)
        s["overall"] = round(wi * (s.get("infra_score") if s.get("infra_score") is not None else s["score"]) + wc * C)
        fpp = (s.get("floodplain") or {}).get("pct")
        if fpp is not None and fpp > 50: s["overall"] = min(s["overall"], 30)   # same floodplain cap as community.py
        corr = [a for a in applied if a["signal"] == "corridor" and a["points"] > 0]
        pos_ = [a for a in applied if a["points"] > 0]
        c["stance"] = {"points": cap, "corridor": bool(corr), "items": applied,
                       "badge": "DC corridor" if corr else ("Gov. support" if any(a["signal"] == "gov_support" and a["points"] > 0 for a in applied) else None)}
        c["laws"]["tracker"] = applied
        if applied: touched += 1
    D.setdefault("community_rules", {})["stance"] = RULES_DOC
    D["tracker"] = {"source": "More Than Just Parks — Data Center Tracker", "url": STATE_PAGE, "weights": WEIGHTS, "cap": [CAP_NEG, CAP_POS],
                    "items": [{k: v for k, v in it.items() if k not in ("lat", "lng")} for it in items]}
    return touched

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--review", required=True, help="plaintext tracker_review.json (decrypted from tracker_review.enc)")
    ap.add_argument("--state", required=True, help="plaintext tracker_state.json (decrypted from tracker_state.enc); updated in place")
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    D = json.load(open(a.data, encoding="utf-8"))
    review = json.load(open(a.review, encoding="utf-8"))
    state = json.load(open(a.state, encoding="utf-8")) if os.path.exists(a.state) else {"entries": {}}
    if a.offline: st = {"ok": True, "detail": f"offline: last pull {state.get('fetched')} ({len(state.get('entries', {}))} entries)", "url": STATE_PAGE}
    else: state, st = fetch(state)
    json.dump(state, open(a.state, "w", encoding="utf-8"), ensure_ascii=False)
    items = build_items(state, review)
    n = apply(D, items, dt.date.today())
    D.setdefault("sources", {})["Data Center Tracker (local-government stance)"] = st
    import outreach
    k = outreach.annotate(D)
    json.dump(D, open(a.data, "w", encoding="utf-8"), ensure_ascii=False)
    unrev = sum(1 for i in items if i["signal"] == "unreviewed")
    log(f"{len(items)} Kansas entries ({unrev} unreviewed); {n} sites have a tracker signal in their jurisdictions; {k} outreach plans")

if __name__ == "__main__":
    main()
