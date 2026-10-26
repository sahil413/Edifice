#!/usr/bin/env python3
"""Outreach plans for the Powered Land view (called by tracker.py after the stance adjustment).

Rule-based and source-bound: every statement comes from data already in sites.json — tracker/designation
items (with their original sources), curated regulatory and news items, the hand-verified zoning
citations, the EDC review, and contacts that carry their own source link. Nothing is invented: where no
named official is on file, the plan says so. Mitigations are labelled as suggestions; any precedent they
mention is cited.

Plans are built for the top 15 sites by overall score plus every site whose governing or secondary
jurisdiction has a tracker entry.
"""
import re

TOP_N = 15
KC_METRO = {"johnson", "wyandotte", "leavenworth", "miami"}

def jname(j):
    k, n = j.split(":", 1)
    return ("City of " if k == "city" else "") + n.title() + (" County" if k == "county" else "")

def cite(url, title="", outlet="", date=""):
    return {"url": url, "title": title, "outlet": outlet, "date": date}

def pick_contact(s, roles):
    for r in roles:
        for c in s.get("contacts", []):
            if c.get("role") == r and (c.get("name") or c.get("org")): return c
    return None

def evergy_contact(s):
    ev = [c for c in s.get("contacts", []) if c.get("role") == "Utility economic development (Evergy)"]
    if not ev: return None
    want = "Kansas City Metro" if (s.get("county") or "").lower() in KC_METRO else "Kansas Projects"
    return next((c for c in ev if want in (c.get("title") or "")), ev[0])

def latest_reg(issues_by_jur, j):
    items = [i for i in issues_by_jur.get(j, []) if i.get("kind") == "regulatory" and i.get("reg_status") in ("active", "proposed", "lifted", "rules", "proposed_rules")]
    return sorted(items, key=lambda i: i.get("date") or "", reverse=True)

def risk_type(text):
    t = text.lower()
    for k, rx in [("ballot", r"ballot|vote on|election"), ("petition", r"petition"), ("moratorium", r"moratori|pause|ban\b|bans\b"),
                  ("water", r"water|aquifer|drought"), ("noise", r"noise|hum|vibration"), ("rates", r"rate (case|increase|hike)|electric bill"),
                  ("rules", r"rules|regulation|ordinance|zoning")]:
        if re.search(rx, t): return k
    return "opposition"

MITIGATION = {
    "moratorium": "Suggested: don't file while it is in force; ask staff for the end date and whether data-center rules are being drafted, and offer input on the draft.",
    "ballot": "Suggested: expect a public vote; brief officials early with a one-page fact sheet (taxes paid, water and noise commitments) and hold any filing until the vote is settled.",
    "petition": "Suggested: expect a petition drive; meet neighbors before filing and publish the project's tax and water numbers up front.",
    "water": "Suggested: lead with a closed-loop or air-cooled design and state a daily water figure up front.",
    "noise": "Suggested: commit to a noise limit measured at the nearest home and avoid on-site gas generators near residences.",
    "rates": "Suggested: offer to fund grid and transmission upgrades so residential rates aren't affected.",
    "rules": "Suggested: get the draft or adopted data-center standards from planning staff and design to them before the first meeting.",
    "cup": "Suggested: plan for a public hearing; book a pre-application meeting with planning staff first.",
    "prohibited": "Suggested: a rezoning or text amendment would be needed; confirm with planning staff whether one would be entertained before spending on diligence.",
    "opposition": "Suggested: hold a neighbor meeting before any filing and publish what the project would pay in local taxes.",
}

def precedents(tracker_items):
    """Cited mitigation precedents from reviewed tracker entries (any Kansas jurisdiction)."""
    out = {}
    for it in tracker_items:
        for a in it.get("angles", []):
            if a.get("type") == "water": out.setdefault("water", []).append(a)
            if a.get("type") == "mitigation_precedent": out.setdefault("water", []).append(a); out.setdefault("rates", []).append(a)
    return out

def plan(s, D, ibj, titems_by_jur, prec):
    c = s["community"]; gov = c["jurisdictions"]["governing"]; sec = c["jurisdictions"]["secondary"]; jurs = [gov] + sec
    issues = D.get("issues") or {}
    st_items = (c.get("stance") or {}).get("items", [])
    # ---------------- stance
    reg = {j: latest_reg(ibj, j) for j in jurs}
    lstat = c["laws"].get("status")
    def has(j, sts): return any(i["reg_status"] in sts for i in reg[j][:3])
    gov_active = bool(reg[gov]) and reg[gov][0]["reg_status"] == "active"
    support = [a for a in st_items if a["points"] > 0 and a["signal"] in ("corridor", "gov_support")]
    prec_only = [a for a in st_items if a["points"] > 0 and a["signal"] == "precedent"]
    proposed = [j for j in jurs if reg[j] and reg[j][0]["reg_status"] == "proposed"]
    sec_active = [j for j in sec if reg[j] and reg[j][0]["reg_status"] == "active"]
    news = [issues[i] for i in c["news"].get("items", []) if i in issues]
    negn = [i for i in news if i["tag"] == "negative"]; posn = [i for i in news if i["tag"] == "positive"]
    if gov_active or lstat == "prohibited": label = "Restrictive"
    elif proposed or sec_active or (support and len(negn) >= 2): label = "Contested"
    elif support: label = "Supportive"
    elif prec_only or posn: label = "Open (no formal position; precedent or positive coverage)"
    else: label = "No stated position found"
    ev = []
    for a in support + prec_only:
        o = a["original"]; ev.append({"text": f"{a['label'].capitalize()} ({jname(a['jurisdiction'])}): {a['summary']}", **cite(o.get("url"), o.get("title"), o.get("outlet")), "tracker_url": a.get("tracker_url")})
    for j in jurs:
        if reg[j]:
            i = reg[j][0]
            ev.append({"text": f"{jname(j)} — {i['title']} ({i.get('date') or 'n.d.'})", **cite(i["url"], i["title"], i.get("outlet", ""), i.get("date", ""))})
    if lstat in ("prohibited", "cup", "sup", "not_defined", "drafting") and c["laws"].get("citations"):
        cc = c["laws"]["citations"][0]
        ev.append({"text": f"Zoning: {c['laws'].get('summary') or lstat}", **cite(cc["url"], cc.get("title", ""))})
    if news: ev.append({"text": f"Local news: {len(negn)} negative, {len(posn)} positive of {len(news)} items" + (f"; latest negative: {negn[0]['title']}" if negn else ""),
                        **(cite(negn[0]["url"], negn[0]["title"], negn[0].get("outlet", ""), negn[0].get("date", "")) if negn else cite(news[0]["url"], news[0]["title"]))})
    # ---------------- contacts (EDC → city manager → county commission → utility → state)
    order = []
    edc = pick_contact(s, ["Local economic development", "Listing contact (economic development)"])
    if edc: order.append({"step": "Local economic development", "contact": edc, "ask": None})
    else:
        e = c.get("edc") or {}
        order.append({"step": "Local economic development", "contact": {"org": e.get("org") or "none identified", "source": e.get("url")} if e.get("org") else None,
                      "note": "no named EDC contact on file"})
    cities = [j for j in jurs if j.startswith("city:")]
    counties = [j for j in jurs if j.startswith("county:")]
    for j in cities:
        offs = [o for it in titems_by_jur.get(j, []) for o in it.get("officials", []) if re.search(r"manager|mayor|economic development director", o.get("title", ""), re.I) and "Council" not in o.get("org", "")]
        if offs:
            o = sorted(offs, key=lambda o: 0 if "Manager" in o["title"] else 1)[0]
            order.append({"step": "City manager / city hall", "contact": {"name": o["name"], "title": o["title"], "org": o["org"], "source": o["url"]}})
        else:
            order.append({"step": "City manager / city hall", "contact": {"org": jname(j)}, "note": "no named contact on file — reach via the city manager's office"})
    for j in counties:
        cz = next((x for x in (c["laws"].get("citations") or []) if j == gov), None)
        order.append({"step": "County commission / planning", "contact": {"org": jname(j) + " Commission", "source": cz["url"] if cz else None},
                      "note": "no named contact on file" + (" — planning & zoning page linked" if cz else "")})
    prov = ((s.get("electric") or [{}])[0] or {}).get("provider") or ""
    if "evergy" in prov.lower() or not prov:
        u = evergy_contact(s)
        if u: order.append({"step": "Utility (Evergy economic development)", "contact": u})
    else:
        u = pick_contact(s, ["Electric provider (as listed)"])
        real = re.search(r"evergy|bpu|board of public utilities|electric|energy|power|co-?op|cooperative|light|municipal|utilit|rec\b", prov, re.I)
        if u: order.append({"step": f"Utility ({prov})", "contact": u})
        elif real: order.append({"step": f"Utility ({prov})", "contact": {"org": prov}, "note": "no named contact on file"})
        else: order.append({"step": "Utility", "contact": None, "note": f"electric provider not identified in the listing (listed as “{prov}”) — confirm with the EDC"})
    stc = pick_contact(s, ["State site-selection contact"])
    if stc: order.append({"step": "State (Kansas Commerce site selection)", "contact": stc})
    # ---------------- lead angle
    angles = []
    for a in support:
        it = next((x for j in jurs for x in titems_by_jur.get(j, []) if x["id8"] == a["id8"]), None)
        for g in (it or {}).get("angles", []):
            if g.get("type") in ("corridor", "past_deal", "tax_base", "ready_site"):
                angles.append({"type": g["type"], "text": g["text"], **cite(g["url"])})
        if not it or not it.get("angles"):
            angles.append({"type": a["signal"], "text": a["summary"], **cite(a["original"].get("url"), a["original"].get("title"), a["original"].get("outlet"))})
    for j in jurs:
        for it in titems_by_jur.get(j, []):
            for g in it.get("angles", []):
                if g.get("type") in ("tax_base", "ready_site") and not any(x["text"] == g["text"] for x in angles):
                    angles.append({"type": g["type"], "text": g["text"], **cite(g["url"])})
    e = c.get("edc") or {}
    if (e.get("findings") or {}).get("data_center"):
        f = e["findings"]["data_center"][0]
        angles.append({"type": "edc_markets_dc", "text": f"{e.get('org')} markets to data-center / large-load users: “{f.get('quote','')[:140]}”", **cite(f["url"], f.get("title", ""))})
    inc = e.get("incentives") or []
    if inc:
        types = sorted({i.get("type") for i in inc if i.get("type")})
        angles.append({"type": "incentives", "text": f"{e.get('org')} lists incentives: {', '.join(types)}", **cite(inc[0]["url"], inc[0].get("title", ""))})
    if prec_only:
        a = prec_only[0]
        n = len([x for x in st_items if x["signal"] == "precedent" and x["jurisdiction"] == a["jurisdiction"]])
        angles.append({"type": "precedent", "text": f"{n} operating or under-construction data center(s) already in {jname(a['jurisdiction'])}, e.g. {a['summary']}", **cite(a["original"].get("url"), a["original"].get("title"), a["original"].get("outlet"))})
    sb = next((i for i in issues.values() if i.get("jurisdiction") == "state" and i.get("reg_status") == "incentive"), None)
    if sb: angles.append({"type": "state_incentive", "text": f"State: {sb['title']} — {sb['summary']}", **cite(sb["url"], sb["title"], sb.get("outlet", ""))})
    # ---------------- risks
    risks = []
    for j in jurs:
        for i in reg[j][:2]:
            if i["reg_status"] == "lifted": continue
            t = risk_type(i["title"] + " " + (i.get("summary") or ""))
            if i["reg_status"] in ("rules", "proposed_rules"): t = "rules" if t in ("opposition", "moratorium") else t
            risks.append({"type": t, "text": f"{jname(j)}: {i['title']} ({i.get('date') or 'n.d.'})", **cite(i["url"], i["title"], i.get("outlet", "")), "mitigation": MITIGATION.get(t, MITIGATION["opposition"])})
    if lstat in ("cup", "sup"):
        cc = (c["laws"].get("citations") or [{}])[0]
        risks.append({"type": "cup", "text": f"Zoning: conditional/special use permit required ({c['laws'].get('summary','')[:120]})", **cite(cc.get("url"), cc.get("title", "")), "mitigation": MITIGATION["cup"]})
    if lstat == "prohibited":
        cc = (c["laws"].get("citations") or [{}])[0]
        risks.append({"type": "prohibited", "text": f"Zoning: data centers prohibited ({c['laws'].get('summary','')[:120]})", **cite(cc.get("url"), cc.get("title", "")), "mitigation": MITIGATION["prohibited"]})
    for j in jurs:
        for it in titems_by_jur.get(j, []):
            for r in it.get("risks", []):
                if not any(x["url"] == r["url"] for x in risks):
                    risks.append({"type": r["type"], "text": f"{jname(j)}: {r['text']}", **cite(r["url"]), "mitigation": MITIGATION.get(r["type"], MITIGATION["opposition"])})
    seen = {x["url"] for x in risks}
    for i in sorted(negn, key=lambda i: i.get("date") or "", reverse=True)[:2]:
        if i["url"] in seen: continue
        t = risk_type(i["title"] + " " + (i.get("summary") or ""))
        risks.append({"type": t, "text": f"News ({i.get('date') or 'n.d.'}): {i['title']}", **cite(i["url"], i["title"], i.get("outlet", "")), "mitigation": MITIGATION.get(t, MITIGATION["opposition"])})
    for r in risks:
        for p in prec.get(r["type"], [])[:1]:
            r["precedent"] = {"text": p["text"], "url": p["url"]}
    # ---------------- next step
    who = edc if edc else (order[1]["contact"] if len(order) > 1 else None)
    nm = (who or {}).get("name") or (who or {}).get("org") or "the local EDC"
    reach = " (" + " · ".join(x for x in [(who or {}).get("email"), (who or {}).get("phone")] if x) + ")" if who and (who.get("email") or who.get("phone")) else ""
    util = next((o["contact"] for o in order if o["step"].startswith("Utility") and (o["contact"] or {}).get("name")), None)
    lead = angles[0]["text"] if angles else None
    if label == "Restrictive":
        j = gov if gov_active else gov
        txt = f"Email {nm}{reach} to ask when the {jname(j)} moratorium/restriction ends and whether data-center rules are being drafted; hold any filing until then."
    elif label == "Contested":
        what = next((r["text"] for r in risks if r["type"] in ("ballot", "petition", "moratorium")), None)
        txt = f"Call {nm}{reach} before any public filing to learn where things stand" + (f" on: {what}" if what else "") + ", and which officials back data-center projects."
    elif label == "Supportive":
        txt = f"Email {nm}{reach} to request a pre-application meeting on {s['name']}" + (f", referencing: {lead[:140]}" if lead else "") + (f"; copy {util['name']} ({util.get('org')}) to confirm large-load power availability." if util else ".")
    else:
        txt = f"Email {nm}{reach} to ask whether {jname(gov)} has a data-center position or zoning path for {s['name']}" + (f"; copy {util['name']} ({util.get('org')}) on power availability." if util else ".")
    nxt = {"text": txt, **cite((who or {}).get("source") or "")}
    def dd(xs, key):
        seen, out = set(), []
        for x in xs:
            k = key(x)
            if k in seen: continue
            seen.add(k); out.append(x)
        return out
    ev = dd(ev, lambda x: (x.get("url"), x["text"].split(": ", 1)[-1]))
    angles = dd(angles, lambda x: x["text"])
    return {"stance": {"label": label, "evidence": ev[:6]}, "contacts": order, "angles": angles[:4], "risks": risks[:6], "next_step": nxt}

def annotate(D):
    issues = D.get("issues") or {}
    ibj = {}
    for i in issues.values(): ibj.setdefault(i.get("jurisdiction"), []).append(i)
    titems = (D.get("tracker") or {}).get("items", [])
    tbj = {}
    for it in titems:
        for j in it.get("jurisdictions", []): tbj.setdefault(j, []).append(it)
    prec = precedents(titems)
    ranked = sorted([s for s in D["sites"] if s.get("community")], key=lambda s: -(s.get("overall") or 0))
    top = {s["id"] for s in ranked[:TOP_N]}
    n = 0
    for s in D["sites"]:
        s.pop("outreach", None)
        c = s.get("community")
        if not c: continue
        jurs = [c["jurisdictions"]["governing"]] + c["jurisdictions"]["secondary"]
        tracked = [j for j in jurs if j in tbj]
        if s["id"] not in top and not tracked: continue
        p = plan(s, D, ibj, tbj, prec)
        p["included_because"] = ", ".join(x for x in [f"top {TOP_N} overall" if s["id"] in top else "", ("tracked jurisdiction: " + ", ".join(jname(j) for j in tracked)) if tracked else ""] if x)
        s["outreach"] = p; n += 1
    return n
