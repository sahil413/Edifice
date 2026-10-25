#!/usr/bin/env python3
"""Weekly opportunity report: new/changed/removed sites since the last snapshot, top N by score
with reasons, and who to contact. Writes Markdown + HTML (private dir only — never commit).

  python3 pipeline/weekly_report.py --data private/data/sites.json --snapshots private/snapshots --out private/reports
"""
import argparse, datetime as dt, glob, html, json, os, re, shutil

WATCH = ["acres", "overall", "infra_score", "zoning"]
ANN = {"annexed": "inside city limits", "unincorporated": "unincorporated", "unknown": "annexation unknown"}

def load(p): return json.load(open(p, encoding="utf-8"))

def best_line(s, cls=69):
    L = [l for l in (s.get("power") or {}).get("lines_by_class", []) if l["class"] >= cls]
    return min(L, key=lambda l: l["mi"]) if L else None

def contact_line(c):
    bits = [c.get("name") or c.get("org") or "", c.get("title") or "", c.get("org") if c.get("name") else "", c.get("phone") or "", c.get("email") or ""]
    return " · ".join(b for b in bits if b)

def key_contacts(s, n=3):
    pri = {"Local economic development": 0, "Listing contact": 1, "Utility economic development (Evergy)": 2, "State site-selection contact": 3}
    cs = sorted([c for c in s.get("contacts", []) if c.get("email") or c.get("phone")], key=lambda c: pri.get(c.get("role"), 9))
    out, seen = [], set()
    for c in cs:
        k = (c.get("name"), c.get("org"))
        if k in seen: continue
        seen.add(k); out.append(c)
    return out[:n]

def ov(s): return s.get("overall", s.get("score", 0))
def comm(s): return (s.get("community") or {})
def ann_text(s):
    a = s.get("annexation") or {}
    t = ANN.get(a.get("status"), "annexation unknown")
    if a.get("status") == "annexed" and a.get("place"): t = f"annexed ({a['place']})"
    if a.get("pending"): t += ", pending annexation"
    elif a.get("listing_says_annexed"): t += " per Census (listing says inside city limits)"
    elif a.get("annex_possible"): t += ", annexation possible"
    return t

def money(n):
    return "—" if n is None else (f"${n/1e6:,.2f}M" if n >= 1e6 else f"${n:,.0f}")

def price_text(s):
    p = s.get("price") or {}
    if "basis" not in p:  # snapshot from before price parsing
        return f"${p['sale']:,.0f} listed" if p.get("sale") else (p.get("note") or "no price listed")
    if p.get("per_acre") is None and p.get("total") is None:
        return p.get("label") or "no price listed"
    lo, hi = p.get("per_acre_low"), p.get("per_acre_high")
    ppa = (f"${lo:,.0f}–{hi:,.0f}/ac" if lo is not None and hi and hi != lo else f"${p['per_acre']:,.0f}/ac") if p.get("per_acre") is not None else ""
    tot = (money(p["total"]) + (" (calc.)" if p.get("total_computed") else "")) if p.get("total") is not None else ""
    t = " · ".join(x for x in (tot, ppa) if x)
    if p.get("label"): t += f" · {p['label']}"
    if p.get("flagged"): t += " · ⚠ check price"
    return t

def site_line(s):
    bl = best_line(s); g = s.get("gasline"); c = comm(s)
    sc = (f"overall **{ov(s)}** (infra {s.get('infra_score', s.get('score'))} · community {c['score']}: reg {c['regulatory']['score']} / sentiment {c['sentiment']['score']})"
          if c else f"score **{ov(s)}**")
    return (f"**{s['name']}** — {s['city']}, {s['county']} Co · {s['acres']:,.0f} ac · {sc} · {ann_text(s)}"
            + (f" · {bl['kv']:.0f} kV line {bl['mi']:.1f} mi" if bl else "")
            + (f" · gas pipeline {g['mi']:.1f} mi" if g else "")
            + f" · price {price_text(s)}"
            + (" · CERTIFIED" if s.get("certified") else "") + f"  \n  [{s['url']}]({s['url']})")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--snapshots", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--page-url", default="https://edifice.vc/sites/")
    a = ap.parse_args()
    cur = load(a.data); today = dt.date.today().isoformat()
    os.makedirs(a.snapshots, exist_ok=True); os.makedirs(a.out, exist_ok=True)
    prev_files = sorted(f for f in glob.glob(os.path.join(a.snapshots, "sites-*.json")) if today not in f)
    prev = load(prev_files[-1]) if prev_files else None
    S = {s["id"]: s for s in cur["sites"]}
    if prev:
        P = {s["id"]: s for s in prev["sites"]}
        since = prev["generated_at"][:10]
        new = [S[i] for i in S if i not in P]
        removed = [P[i] for i in P if i not in S]
        changed = []
        for i in S:
            if i in P:
                diffs = [f"{k}: {P[i].get(k)} → {S[i].get(k)}" for k in WATCH if P[i].get(k) != S[i].get(k)]
                pa, sa = (P[i].get("annexation") or {}).get("status"), (S[i].get("annexation") or {}).get("status")
                if pa and pa != sa: diffs.append(f"annexation: {pa} → {sa}")
                pc, sc = comm(P[i]), comm(S[i])
                for k in ("regulatory", "sentiment"):
                    if pc.get(k) and sc.get(k) and pc[k]["score"] != sc[k]["score"]: diffs.append(f"{k}: {pc[k]['score']} → {sc[k]['score']}")
                if P[i].get("lois_modified") != S[i].get("lois_modified"): diffs.append("listing updated " + str(S[i].get("lois_modified", ""))[:10])
                if diffs: changed.append((S[i], diffs))
    else:
        since = "first run (using listing dates for the past 7 days)"
        new = [s for s in cur["sites"] if s.get("is_new")]
        removed = []
        changed = [(s, ["listing updated " + str(s.get("lois_modified", ""))[:10]]) for s in cur["sites"] if s.get("is_changed") and not s.get("is_new")]
    top = sorted(cur["sites"], key=lambda s: -ov(s))[:a.top]
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    I = cur.get("issues", {})
    new_issues = sorted([i for i in I.values() if i.get("first_seen", "") > week_ago and i.get("kind") != "annexation"
                         and (i.get("date") or "9999") >= (dt.date.today() - dt.timedelta(days=60)).isoformat()],
                        key=lambda i: (i.get("kind") != "regulatory", i.get("date") or ""), reverse=False)
    affected = {}
    for s in cur["sites"]:
        for x in comm(s).get("issues", []): affected.setdefault(x, []).append(s)
    fails = [f"{k}: {v['detail']}" for k, v in cur["sources"].items() if not v["ok"]]

    md = [f"# Edifice — Kansas data-center sites: weekly report ({today})", "",
          f"{cur['count']} sites tracked · {len(new)} new · {len(changed)} changed · {len(removed)} removed since {since}. "
          f"Full list (password): {a.page_url}", ""]
    md += ["## New sites", ""] + ([f"- {site_line(s)}" for s in sorted(new, key=lambda s: -ov(s))] or ["- None"]) + [""]
    md += ["## Changed sites", ""] + ([f"- {site_line(s)}  \n  _{'; '.join(d)}_" for s, d in sorted(changed, key=lambda x: -ov(x[0]))[:25]] or ["- None"]) + [""]
    if removed: md += ["## Removed / no longer listed", ""] + [f"- {s['name']} — {s['city']} ({s['acres']:,.0f} ac)" for s in removed] + [""]
    if "weights" in cur:
        w = cur["weights"]
        md += [f"_Overall = {w['overall']['infrastructure']:.0%} infrastructure + {w['overall']['community']:.0%} community/regulatory "
               f"(community = {w['community']['regulatory']:.0%} regulatory + {w['community']['sentiment']:.0%} sentiment)._", ""]
    md += ["## New regulatory & community issues (first seen this week, dated within 60 days)", ""]
    for i in new_issues[:30]:
        n = len(affected.get(i["id"], []))
        md.append(f"- [{i.get('tag','').upper()}] {i.get('date') or 'n.d.'} · {i['jurisdiction']} · {i['kind']}{' ('+i['reg_status']+')' if i.get('reg_status') else ''}: "
                  f"{i['title']} — affects {n} site{'s' if n != 1 else ''}  \n  [{i.get('outlet') or i['url']}]({i['url']})")
    if not new_issues: md.append("- None")
    md += [""]
    if cur.get("annexation_counts"):
        ac = cur["annexation_counts"]
        md += [f"Annexation: {ac.get('annexed',0)} inside city limits · {ac.get('unincorporated',0)} unincorporated · {ac.get('unknown',0)} unknown · {ac.get('pending',0)} pending · {ac.get('listing_says_annexed',0)} where the listing says annexed but the Census boundary doesn't.", ""]
    md += [f"## Top {len(top)} opportunities (overall rank)", ""]
    for n, s in enumerate(top, 1):
        md.append(f"{n}. {site_line(s)}")
        c = comm(s)
        if c:
            md.append("   - Regulatory: " + "; ".join(c["regulatory"]["why"]))
            md.append("   - Sentiment: " + "; ".join(c["sentiment"]["why"][:4]))
        pp = s.get("price") or {}
        if pp.get("basis") or pp.get("flags"):
            md.append("   - Price: " + price_text(s) + (f" ({pp['basis']})" if pp.get("basis") else "") + "".join(f"; ⚠ {f}" for f in pp.get("flags") or []))
        md.append("   - Infrastructure: " + "; ".join(f"{k} {p['pts']:g}/{p['max']} ({p['why']})" for k, p in s["score_parts"].items()))
        for c in key_contacts(s):
            md.append(f"   - Contact ({c.get('role')}): {contact_line(c)}")
    md += [""]
    cheap = sorted((x for x in cur["sites"] if ov(x) >= 70 and (x.get("price") or {}).get("per_acre") is not None),
                   key=lambda x: x["price"]["per_acre"])[:5]
    priced = [x for x in cur["sites"] if (x.get("price") or {}).get("per_acre") is not None]
    md += ["## Lowest $/acre among sites scoring 70+ overall", "",
           f"_{len(priced)} of {len(cur['sites'])} sites list a price; ⚠ marks a value to confirm._", ""]
    md += [f"{n}. {site_line(x)}" for n, x in enumerate(cheap, 1)] or ["- None"]
    md += [""]
    if fails: md += ["## Source problems this run", ""] + [f"- {f}" for f in fails] + [""]
    md += ["_Scores are screening estimates from public GIS (HIFLD lines, EIA pipelines, OSM substations) and LOIS listing data; confirm capacity with the utility._"]
    text = "\n".join(md)
    base = os.path.join(a.out, f"weekly-{today}")
    open(base + ".md", "w", encoding="utf-8").write(text)
    open(base + ".html", "w", encoding="utf-8").write(md_to_html(text))
    shutil.copy(a.data, os.path.join(a.snapshots, f"sites-{today}.json"))
    print(base + ".md"); print(base + ".html")

def md_to_html(text):
    out, in_list = [], None
    def inline(t):
        t = html.escape(t)
        t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t); t = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", t)
        return t.replace("  \n", "<br>")
    for block in re.split(r"\n(?=[-#\d]|\s{3}-|$)", text):
        b = block.rstrip()
        if not b: continue
        if b.startswith("#"):
            lvl = len(b) - len(b.lstrip("#")); out.append(f"<h{lvl}>{inline(b.lstrip('# '))}</h{lvl}>")
        elif re.match(r"^(\d+\.|-|\s+-)\s", b):
            out.append(f"<p style='margin:4px 0 4px {24 if b.startswith(' ') else 0}px'>{inline(b.strip())}</p>")
        else:
            out.append(f"<p>{inline(b)}</p>")
    return ("<!doctype html><meta charset='utf-8'><body style=\"font-family:Jost,Helvetica,Arial,sans-serif;max-width:760px;margin:auto;padding:16px;color:#111\">"
            "<div style='color:#F14B21;font:12px monospace;letter-spacing:.14em'>EDIFICE · KANSAS SITES</div>" + "\n".join(out) + "</body>")

if __name__ == "__main__":
    main()
