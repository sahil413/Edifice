#!/usr/bin/env python3
"""Summarise deal-pipeline decisions into sourcing preferences.

Reads stage events from Firestore (via edks.py credentials: emulator, EDIFICE_FIREBASE_SA or the key file),
from a local --events JSON/CSV export (e.g. `edks.py dump`), or from the legacy Apps Script endpoint
(PIPELINE_URL/PIPELINE_TOKEN),
joins them to the private site data and writes a markdown summary. The output path must be private
(gitignored); nothing here is committed except this code.
"""
import argparse, csv, json, os, statistics, sys, urllib.parse, urllib.request
from collections import Counter

PURSUE = {"Reviewing", "Contacted", "In Diligence", "LOI Submitted", "Under Contract", "Closed"}
PASS = {"Passed", "On Hold"}

def env(*names):
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return ""

def fetch_events():
    url, tok = env("PIPELINE_URL", "EDIFICE_PIPELINE_URL"), env("PIPELINE_TOKEN", "EDIFICE_PIPELINE_TOKEN")
    if not url or not tok:
        return None
    q = url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"token": tok})
    with urllib.request.urlopen(q, timeout=60) as r:
        j = json.loads(r.read().decode("utf-8"))
    if not j.get("ok"):
        sys.exit("[learnings] endpoint refused the request")
    return j.get("events") or []

def firestore_events():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import edks
    if not edks.configured():
        return None
    return edks.all_events(edks.db())

def load_events(path):
    if path.endswith(".csv"):
        return list(csv.DictReader(open(path, encoding="utf-8")))
    j = json.load(open(path, encoding="utf-8"))
    return j.get("events", j) if isinstance(j, dict) else j

def reasons(e):
    r = e.get("reasons") or ""
    return [x.strip() for x in (r if isinstance(r, list) else str(r).split(",")) if x.strip()]

def med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None

def land_metrics(s):
    p = s.get("price") or {}
    lines = [l for l in (s.get("power") or {}).get("lines_by_class") or [] if l.get("class", 0) >= 69]
    return {"$/acre": p.get("per_acre"), "acres": s.get("acres"),
            "best 69+ kV line mi": min((l["mi"] for l in lines), default=None),
            "gas mi": (s.get("gasline") or {}).get("mi"), "community score": (s.get("community") or {}).get("score")}

def storage_metrics(r):
    return {"price": r.get("price"), "$/acre": r.get("price_per_acre"), "acres": r.get("acres"),
            "AADT": (r.get("traffic") or {}).get("aadt"), "3-mi population": (r.get("demand") or {}).get("pop3")}

def summarise(view, events, objs, metric_fn, place_fn):
    last = {}
    for e in events:
        if e.get("type") != "stage" or e.get("view") != view:
            continue
        k = str(e.get("site_id"))
        ts = str(e.get("timestamp") or e.get("ts") or "")
        if k not in last or ts > last[k][0]:
            last[k] = (ts, e)
    pur = [e for _, e in last.values() if e.get("to_stage") in PURSUE]
    pas = [e for _, e in last.values() if e.get("to_stage") in PASS]
    out = [f"## {view.title()}", "", f"{len(pur)} pursued, {len(pas)} passed or on hold (latest decision per deal).", ""]
    if not last:
        return out + ["No decisions yet.", ""]
    for lab, grp in (("Pursue", pur), ("Pass", pas)):
        c = Counter(r for e in grp for r in reasons(e))
        out.append(f"- {lab} reasons: " + (", ".join(f"{k} {v}" for k, v in c.most_common()) or "none"))
    out += ["", "| Median | Pursued | Passed |", "|---|---|---|"]
    keys = list(metric_fn({}).keys())
    for k in keys:
        a = med([metric_fn(objs[e["site_id"]])[k] for e in pur if e.get("site_id") in objs])
        b = med([metric_fn(objs[e["site_id"]])[k] for e in pas if e.get("site_id") in objs])
        out.append(f"| {k} | {'' if a is None else round(a, 2)} | {'' if b is None else round(b, 2)} |")
    locs = Counter(place_fn(objs[e["site_id"]]) for e in pas if "Location" in reasons(e) and e.get("site_id") in objs)
    if locs:
        out += ["", "Passed on location: " + ", ".join(f"{k} ({v})" for k, v in locs.most_common())]
    quotes = [e for _, e in sorted(last.values(), key=lambda x: x[0]) if e.get("text")][-8:]
    if quotes:
        out += ["", "In your words:"] + [f"- {e.get('to_stage')} · {e.get('name', '')}: \"{e['text']}\"" for e in reversed(quotes)]
    open_fu = [e for e in events if e.get("type") == "followup" and e.get("view") == view and (e.get("status") or "open") != "done"]
    if open_fu:
        out += ["", f"Open follow-ups ({len(open_fu)}):"] + [f"- [{e.get('assignee') or 'Any bot'}] {e.get('name', '')}: {e.get('text')}" + (f" (due {e['due']})" if e.get("due") else "") for e in open_fu]
    return out + [""]


# ------------------------------------------------------------------ learned weights (bounded, shrunk)
# Each factor of the base score gets a multiplier (1 + delta). delta comes from two kinds of evidence in your
# decisions (latest decision per deal; pursue = Reviewing and later, pass = Passed; On Hold is ignored):
#   * a ridge-regularised logistic regression of pursue-vs-pass on the standardised factor scores, and
#   * the reasons you gave (a factor named in a reason counts as "this matters"),
# combined as delta = CAP * tanh(evidence) * n / (n + PRIOR_N). The prior is the current (base) weights
# (delta = 0); a small sample is shrunk toward it and no factor can move more than +/-CAP.
# Power (land) and storage are separate models. Restricted (admin-only) sites are never used.
CAP, PRIOR_N, RIDGE = 0.25, 20.0, 1.0
LAND_F = [("transmission", "Transmission line", 0.6), ("substation", "Substation", 0.6), ("gas", "Gas pipeline", 0.6),
          ("scale", "Site scale", 0.6), ("fiber", "Fiber", 0.6), ("water", "Water", 0.6), ("community", "Community & zoning", 0.4)]
STOR_F = [("demand", "Demand", 1.0), ("competition", "Competition", 1.0), ("visibility", "Visibility", 1.0),
          ("zoning", "Zoning", 1.0), ("price", "Price", 1.0)]
STOR_MAX = {"demand": 30, "competition": 25, "visibility": 15, "zoning": 20, "price": 10}
REASON_MAP = {"land": {"Power": ["transmission", "substation"], "Gas": ["gas"], "Size": ["scale"],
                       "Zoning / sentiment": ["community"], "Location": ["community"]},
              "storage": {"Price": ["price"], "Zoning / sentiment": ["zoning"], "Location": ["demand", "visibility"],
                          "Size": ["demand"]}}
REASON_W = 1.5   # evidence added when every decision cites the factor

def land_features(s):
    """{factor: (fraction 0..1, base weight)} for a land site record."""
    P = s.get("score_parts") or {}
    out = {}
    for k, _, grp in LAND_F:
        if k == "community":
            c = (s.get("community") or {}).get("score")
            if isinstance(c, (int, float)):
                out[k] = (max(0.0, min(1.0, c / 100.0)), 0.4)
            continue
        p = P.get(k) or {}
        if isinstance(p, dict) and p.get("max"):
            out[k] = (max(0.0, min(1.0, (p.get("pts") or 0) / p["max"])), grp * p["max"] / 100.0)
    return out

def storage_features(r):
    P = r.get("score_parts") or {}
    out = {}
    for k, _, _ in STOR_F:
        if isinstance(P.get(k), (int, float)):
            m = STOR_MAX[k]
            out[k] = (max(0.0, min(1.0, P[k] / m)), m / 100.0)
    return out

def learned_score(feats, deltas):
    """100 * sum((1+d)*W*f) / sum((1+d)*W); equals the base score when every delta is 0."""
    num = den = 0.0
    for k, (f, w) in feats.items():
        m = 1.0 + float((deltas or {}).get(k, 0.0))
        num += m * w * f; den += m * w
    return 100.0 * num / den if den else None

def _ridge_logit(X, y, lam=RIDGE, iters=60):
    """Ridge logistic regression by Newton steps (tiny K, no numpy needed). Returns coefficients (no intercept penalty)."""
    import math
    n, K = len(X), len(X[0]) if X else 0
    b = [0.0] * (K + 1)
    for _ in range(iters):
        g = [0.0] * (K + 1); H = [[0.0] * (K + 1) for _ in range(K + 1)]
        for xi, yi in zip(X, y):
            z = [1.0] + xi
            eta = sum(bj * zj for bj, zj in zip(b, z)); p = 1 / (1 + math.exp(-max(-30, min(30, eta))))
            for a_ in range(K + 1):
                g[a_] += (yi - p) * z[a_]
                for c_ in range(K + 1):
                    H[a_][c_] += p * (1 - p) * z[a_] * z[c_]
        for j in range(1, K + 1):
            g[j] -= lam * b[j]; H[j][j] += lam
        H[0][0] += 1e-6
        # solve H d = g (Gaussian elimination)
        M = [row[:] + [gi] for row, gi in zip(H, g)]
        for i in range(K + 1):
            piv = max(range(i, K + 1), key=lambda r_: abs(M[r_][i])); M[i], M[piv] = M[piv], M[i]
            if abs(M[i][i]) < 1e-12: continue
            for r_ in range(K + 1):
                if r_ != i:
                    f = M[r_][i] / M[i][i]
                    M[r_] = [a1 - f * a2 for a1, a2 in zip(M[r_], M[i])]
        d = [M[i][K + 1] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(K + 1)]
        b = [bj + dj for bj, dj in zip(b, d)]
        if max(abs(x) for x in d) < 1e-7: break
    return b[1:]

def decisions(events, view, objs):
    """Latest pursue/pass decision per deal on a known, non-restricted site."""
    last = {}
    for e in events:
        if e.get("type") != "stage" or e.get("view") != view or e.get("restricted"):
            continue
        sid = str(e.get("site_id")); ts = str(e.get("ts") or e.get("timestamp") or "")
        if sid in objs and (sid not in last or ts > last[sid][0]):
            last[sid] = (ts, e)
    out = []
    for sid, (_, e) in last.items():
        y = 1 if e.get("to_stage") in PURSUE else 0 if e.get("to_stage") == "Passed" else None
        if y is not None:
            out.append((sid, y, e))
    return out

def fit_track(view, events, objs, answers=None):
    import math
    F = LAND_F if view == "land" else STOR_F
    feat = land_features if view == "land" else storage_features
    keys = [k for k, _, _ in F]; label = {k: l for k, l, _ in F}
    D = decisions(events, view, objs)
    n = len(D)
    rows = [(feat(objs[sid]), y, e) for sid, y, e in D]
    # standardised features for the regression
    beta = {k: 0.0 for k in keys}
    if n >= 2 and len({y for _, y, _ in rows}) == 2:
        cols = {k: [r[0].get(k, (0.0, 0))[0] for r in rows] for k in keys}
        mu = {k: sum(v) / n for k, v in cols.items()}
        sd = {k: (sum((x - mu[k]) ** 2 for x in v) / n) ** 0.5 for k, v in cols.items()}
        use = [k for k in keys if sd[k] > 1e-9]
        if use:
            X = [[(r[0].get(k, (0.0, 0))[0] - mu[k]) / sd[k] for k in use] for r in rows]
            b = _ridge_logit(X, [y for _, y, _ in rows])
            beta.update({k: v for k, v in zip(use, b)})
    # reasons (stage reasons + answered "why" reasons) -> how often each factor is cited
    cite = {k: 0 for k in keys}
    RM = REASON_MAP[view]
    for _, _, e in rows:
        rs = set(reasons(e)) | set((answers or {}).get(e.get("event_id"), []))
        for k in {k for r in rs for k in RM.get(r, [])}:
            cite[k] += 1
    deltas, drivers = {}, []
    shrink = n / (n + PRIOR_N)
    for k in keys:
        ev = beta[k] * 0.5 + (REASON_W * cite[k] / n if n else 0.0)
        d = CAP * math.tanh(ev) * shrink
        deltas[k] = round(max(-CAP, min(CAP, d)), 4)
    for k in sorted(keys, key=lambda k: -abs(deltas[k])):
        d = deltas[k]
        if abs(d) < 0.01:
            continue
        why = []
        if abs(beta[k]) > 0.05:
            why.append(f"sites you pursued scored {'higher' if beta[k] > 0 else 'lower'} on it than sites you passed")
        if cite[k]:
            why.append(f"you cited it in {cite[k]} of {n} decision{'s' if n != 1 else ''}")
        drivers.append({"factor": k, "label": label[k], "delta": d,
                        "text": f"{label[k]} counts {abs(round(d * 100))}% {'more' if d > 0 else 'less'}" + (": " + " and ".join(why) if why else "") + "."})
    if not drivers:
        drivers.append({"factor": "", "label": "", "delta": 0,
                        "text": "No factor has moved yet: " + (f"{n} decision{'s' if n != 1 else ''} so far, shrunk toward the base weights." if n else "no pursue or pass decisions yet.")})
    return {"n": n, "pursued": sum(1 for _, y, _ in rows if y), "passed": sum(1 for _, y, _ in rows if not y),
            "deltas": deltas, "drivers": drivers, "cap": CAP, "prior_n": PRIOR_N,
            "evidence": {k: {"beta": round(beta[k], 4), "cited": cite[k]} for k in keys}}

def compute_weights(events, land_objs, stor_objs, answers=None):
    return {"power": fit_track("land", events, land_objs, answers), "storage": fit_track("storage", events, stor_objs, answers)}

def recompute(client, force=False, quiet=False):
    """Read decisions + site records from Firestore, refit both models, write learnings/weights if changed."""
    import hashlib, datetime as _dt
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import edks
    land, stor = {}, {}
    for d in client.collection("sites").select(["view", "j"]).stream():
        x = d.to_dict() or {}
        try:
            j = json.loads(x.get("j") or "{}")
        except Exception:
            continue
        (land if x.get("view") == "land" else stor)[d.id] = j
    events = [e for e in edks.all_events(client) if e.get("type") == "stage"]
    answers = {}
    for d in client.collection("reviewQueue").where("status", "==", "answered").stream():
        x = d.to_dict() or {}
        if not x.get("restricted") and x.get("answer_reasons"):
            answers[x.get("event_id") or d.id] = list(x["answer_reasons"])
    W = compute_weights(events, land, stor, answers)
    sig = hashlib.sha1(json.dumps({t: W[t]["deltas"] for t in W}, sort_keys=True).encode()).hexdigest()[:16]
    ref = client.collection("learnings").document("weights"); snap = ref.get(); old = (snap.to_dict() or {}) if snap.exists else {}
    if old.get("sig") == sig and not force:
        if not quiet: print(f"[learnings] weights unchanged (v{old.get('version')}, n={old.get('n')})")
        return old
    doc = {"version": int(old.get("version") or 0) + 1, "sig": sig, "n": W["power"]["n"] + W["storage"]["n"],
           "updated": _dt.datetime.now(_dt.timezone.utc), "power": W["power"], "storage": W["storage"],
           "method": f"delta = {CAP} * tanh(0.5*ridge-logit beta + {REASON_W}*share of decisions citing the factor) * n/(n+{PRIOR_N:g}); learned score = 100*sum((1+delta)*W*f)/sum((1+delta)*W)"}
    ref.set(doc)
    if not quiet: print(f"[learnings] weights v{doc['version']}: power n={W['power']['n']}, storage n={W['storage']['n']}")
    return doc

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", help="local events export (JSON or CSV); default: fetch from the endpoint")
    ap.add_argument("--data", default="private/data/sites.json")
    ap.add_argument("--storage", default="private/data/storage.json")
    ap.add_argument("--out", default="private/learnings.md")
    ap.add_argument("--push", action="store_true", help="also store the summary in Firestore learnings/summary")
    ap.add_argument("--weights", action="store_true", help="only refit the learned weights (learnings/weights) and exit")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.weights:
        import edks
        recompute(edks.db(), force=a.force)
        return
    ev = load_events(a.events) if a.events else firestore_events()
    if ev is None and not a.events:
        ev = fetch_events()
    if ev is None:
        ev = []
        print("[learnings] no Firestore credentials, endpoint or --events file; writing an empty summary")
    land = {s["id"]: s for s in json.load(open(a.data, encoding="utf-8"))["sites"]} if os.path.exists(a.data) else {}
    stor = {r["id"]: r for r in json.load(open(a.storage, encoding="utf-8")).get("rows", [])} if os.path.exists(a.storage) else {}
    lines = ["# What the pipeline decisions say", "",
             "Generated from stage changes and their reasons. Use it to weight sourcing: favour what was pursued, filter out what was passed.", ""]
    lines += summarise("land", ev, land, land_metrics, lambda s: f"{s.get('county')} County")
    lines += summarise("storage", ev, stor, storage_metrics, lambda r: r.get("city_label") or r.get("city") or "")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    open(a.out, "w", encoding="utf-8").write("\n".join(lines))
    print(f"[learnings] {len(ev)} events -> {a.out}")
    if a.push:
        import edks
        edks.db().collection("learnings").document("summary").set(
            {"md": "\n".join(lines), "events": len(ev), "updated": edks.now_iso()})
        print("[learnings] stored in Firestore learnings/summary")

if __name__ == "__main__":
    main()
