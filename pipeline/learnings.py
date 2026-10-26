#!/usr/bin/env python3
"""Summarise deal-pipeline decisions into sourcing preferences.

Reads stage events from the pipeline Apps Script (doGet, needs PIPELINE_URL/EDIFICE_PIPELINE_URL and
PIPELINE_TOKEN/EDIFICE_PIPELINE_TOKEN in the environment) or from a local --events JSON/CSV export,
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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", help="local events export (JSON or CSV); default: fetch from the endpoint")
    ap.add_argument("--data", default="private/data/sites.json")
    ap.add_argument("--storage", default="private/data/storage.json")
    ap.add_argument("--out", default="private/learnings.md")
    a = ap.parse_args()
    ev = load_events(a.events) if a.events else fetch_events()
    if ev is None:
        ev = []
        print("[learnings] no endpoint configured and no --events file; writing an empty summary")
    land = {s["id"]: s for s in json.load(open(a.data, encoding="utf-8"))["sites"]} if os.path.exists(a.data) else {}
    stor = {r["id"]: r for r in json.load(open(a.storage, encoding="utf-8")).get("rows", [])} if os.path.exists(a.storage) else {}
    lines = ["# What the pipeline decisions say", "",
             "Generated from stage changes and their reasons. Use it to weight sourcing: favour what was pursued, filter out what was passed.", ""]
    lines += summarise("land", ev, land, land_metrics, lambda s: f"{s.get('county')} County")
    lines += summarise("storage", ev, stor, storage_metrics, lambda r: r.get("city_label") or r.get("city") or "")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    open(a.out, "w", encoding="utf-8").write("\n".join(lines))
    print(f"[learnings] {len(ev)} events -> {a.out}")

if __name__ == "__main__":
    main()
