#!/usr/bin/env python3
"""Edifice deal pipeline on Firestore: the daily data push and the bots' command line.

Auth (never printed, never logged, never written anywhere by this script):
  EDIFICE_FIREBASE_SA            path to a service-account key file, or the key JSON itself (CI secret)
  GOOGLE_APPLICATION_CREDENTIALS path to a key file
  default                        ~/.config/edifice/firebase-sa.json (keep it chmod 600)
  FIRESTORE_EMULATOR_HOST        talk to the local emulator instead (no key needed; set FIREBASE_PROJECT)

Bots:
  edks.py followups [--assignee NAME] [--all] [--json]     open follow-ups (oldest due first)
  edks.py done EVENT_ID [--by NAME]                        mark a follow-up done
  edks.py reopen EVENT_ID [--by NAME]
  edks.py note SITE_ID "text" [--by NAME]                  add a note to a deal
  edks.py followup SITE_ID "task" [--assignee NAME] [--due YYYY-MM-DD] [--by NAME]
  edks.py dump [--out FILE]                                every event as JSON (feeds learnings.py)
  edks.py push-learnings FILE                              publish learnings.md to the app
Pipeline / admin:
  edks.py push --data sites.json [--storage storage.json] [--email-engine FILE]
  edks.py users list | add EMAIL --role admin|editor|viewer [--name N] | remove EMAIL
  edks.py import FILE                                      import events (JSON or CSV), idempotent by event_id
"""
import argparse, csv, datetime as dt, hashlib, json, os, subprocess, sys, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = ["New", "Reviewing", "Contacted", "In Diligence", "LOI Submitted", "Under Contract", "Closed", "Passed", "On Hold"]
ROLES = ("admin", "editor", "viewer")
MAX_DOC = 900_000  # Firestore caps a document at 1 MiB
EV_FIELDS = ("event_id", "site_id", "view", "name", "type", "ts", "from_stage", "to_stage", "reasons", "text",
             "assignee", "due", "status", "by", "done_at", "done_by")

def die(msg):
    sys.exit(f"[edks] {msg}")

def now_iso():
    t = dt.datetime.now(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"

def sa_source():
    return os.environ.get("EDIFICE_FIREBASE_SA") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") \
        or os.path.expanduser("~/.config/edifice/firebase-sa.json")

def configured():
    """True when Firestore is reachable without prompting: emulator, key in env, or key file present."""
    s = sa_source()
    return bool(os.environ.get("FIRESTORE_EMULATOR_HOST") or s.lstrip().startswith("{") or os.path.exists(s))

def db():
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        die("firebase-admin is not installed (pip install firebase-admin)")
    if os.environ.get("FIRESTORE_EMULATOR_HOST"):
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore as gcf
        return gcf.Client(project=os.environ.get("FIREBASE_PROJECT", "demo-edifice"), credentials=AnonymousCredentials())
    src = sa_source()
    try:
        if src.lstrip().startswith("{"):
            cred = credentials.Certificate(json.loads(src))
        elif os.path.exists(src):
            cred = credentials.Certificate(src)
        else:
            die("no service-account key found (set EDIFICE_FIREBASE_SA or save it to ~/.config/edifice/firebase-sa.json)")
    except SystemExit:
        raise
    except Exception:
        die("the service-account key could not be loaded (contents not shown)")
    app = firebase_admin.initialize_app(cred)
    return firestore.client(app)

# ------------------------------------------------------------------ data push (daily pipeline)
def _h(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

def _put_blob(client, base, text, batch_writes):
    """Store a large JSON string under meta/{base}, chunked when it would exceed a document."""
    parts = [text[i:i + MAX_DOC] for i in range(0, len(text), MAX_DOC)] or [""]
    old = client.collection("meta").document(base).get()
    old_n = (old.to_dict() or {}).get("parts", 0) if old.exists else 0
    if len(parts) == 1:
        batch_writes.append(("set", client.collection("meta").document(base), {"j": parts[0], "parts": 0, "h": _h(text), "updated": now_iso()}))
    else:
        for i, p in enumerate(parts):
            batch_writes.append(("set", client.collection("meta").document(f"{base}_{i}"), {"j": p}))
        batch_writes.append(("set", client.collection("meta").document(base), {"parts": len(parts), "h": _h(text), "updated": now_iso()}))
    for i in range(len(parts) if len(parts) > 1 else 0, old_n):
        batch_writes.append(("delete", client.collection("meta").document(f"{base}_{i}"), None))

def _commit(client, writes):
    for i in range(0, len(writes), 400):
        b = client.batch()
        for op, ref, data in writes[i:i + 400]:
            b.set(ref, data) if op == "set" else b.delete(ref)
        b.commit()

def load_engine(path, password_env):
    if path and os.path.exists(path):
        return open(path, encoding="utf-8").read()
    enc = os.path.join(HERE, "email_engine.enc")
    if os.path.exists(enc) and os.environ.get(password_env):
        r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                            "-pass", f"env:{password_env}", "-in", enc], capture_output=True)
        if r.returncode == 0:
            return r.stdout.decode("utf-8")
    print("[edks] WARNING: no email engine available; leaving the stored one in place")
    return None

def cmd_push(a):
    client = db()
    land = json.load(open(a.data, encoding="utf-8"))
    views = {"land": land.pop("sites")}
    stor = None
    if a.storage and os.path.exists(a.storage):
        stor = json.load(open(a.storage, encoding="utf-8"))
        views["storage"] = stor.pop("rows")
    elif a.storage:
        print(f"[edks] WARNING: {a.storage} not found; storage rows left as they are")
    existing = {d.id: (d.to_dict() or {}) for d in client.collection("sites").select(["view", "h"]).stream()}
    writes, keep, changed = [], set(), 0
    for view, rows in views.items():
        for r in rows:
            rid = str(r["id"])
            if "/" in rid or rid.startswith("__"):
                die(f"unusable site id {rid!r}")
            j = json.dumps(r, ensure_ascii=False, separators=(",", ":"))
            if len(j) > MAX_DOC:
                die(f"site {rid} is too large for one document ({len(j)} bytes)")
            keep.add(rid)
            h = _h(j)
            if existing.get(rid, {}).get("h") == h:
                continue
            changed += 1
            writes.append(("set", client.collection("sites").document(rid),
                           {"view": view, "name": r.get("name") or "", "score": r.get("overall", r.get("score")), "h": h, "j": j}))
    stale = [rid for rid, d in existing.items() if rid not in keep and d.get("view") in views]
    for rid in stale:
        writes.append(("delete", client.collection("sites").document(rid), None))
    _put_blob(client, "land", json.dumps(land, ensure_ascii=False, separators=(",", ":")), writes)
    if stor is not None:
        _put_blob(client, "storage", json.dumps(stor, ensure_ascii=False, separators=(",", ":")), writes)
    eng = load_engine(a.email_engine, a.password_env)
    if eng is not None:
        writes.append(("set", client.collection("private").document("email_engine"), {"js": eng, "h": _h(eng), "updated": now_iso()}))
    _commit(client, writes)
    print(f"[edks] pushed: {len(keep)} sites ({changed} changed, {len(stale)} removed), meta {'land+storage' if stor is not None else 'land'}"
          f"{', email engine' if eng is not None else ''}")

# ------------------------------------------------------------------ users
def cmd_users(a):
    client = db()
    col = client.collection("users")
    if a.action == "list":
        for d in col.stream():
            u = d.to_dict() or {}
            print(f"{d.id}\t{u.get('role')}\t{u.get('name', '')}")
        return
    if not a.email:
        die("an email is required")
    em = a.email.strip().lower()
    if a.action == "add":
        if a.role not in ROLES:
            die(f"role must be one of {', '.join(ROLES)}")
        data = {"role": a.role, "added_by": "pipeline", "added": dt.datetime.now(dt.timezone.utc)}
        if a.name:
            data["name"] = a.name
        col.document(em).set(data, merge=True)
        print(f"[edks] {em} is now {a.role}")
    elif a.action == "remove":
        col.document(em).delete()
        print(f"[edks] removed {em}")

# ------------------------------------------------------------------ events
def all_events(client):
    out = []
    for d in client.collection_group("events").stream():
        e = d.to_dict() or {}
        e["event_id"] = d.id
        e.setdefault("site_id", d.reference.parent.parent.id)
        for k, v in list(e.items()):
            if isinstance(v, dt.datetime):
                e[k] = v.isoformat()
        out.append(e)
    out.sort(key=lambda e: str(e.get("ts", "")))
    return out

def find_event(client, eid):
    for d in client.collection_group("events").stream():
        if d.id == eid:
            return d
    return None

def site_info(client, sid):
    s = client.collection("sites").document(sid).get()
    if not s.exists:
        die(f"no site {sid}")
    d = s.to_dict()
    return d.get("view"), d.get("name", "")

def ensure_deal(client, sid, view, name, by):
    ref = client.collection("deals").document(sid)
    snap = ref.get()
    data = {"site_id": sid, "view": view, "name": name, "updated": dt.datetime.now(dt.timezone.utc), "updated_by": by}
    if not snap.exists or "stage" not in (snap.to_dict() or {}):
        data["stage"] = "New"
    ref.set(data, merge=True)
    return ref

def add_event(client, sid, typ, text, by, **kw):
    view, name = site_info(client, sid)
    ref = ensure_deal(client, sid, view, name, by)
    eid = kw.pop("event_id", None) or (dt.datetime.now().strftime("%y%m%d") + uuid.uuid4().hex[:10])
    e = {"event_id": eid, "site_id": sid, "view": view, "name": name, "type": typ, "ts": now_iso(), "text": text, "by": by}
    e.update({k: v for k, v in kw.items() if v is not None})
    ref.collection("events").document(eid).set(e)
    return e

def cmd_followups(a):
    ev = [e for e in all_events(db()) if e.get("type") == "followup"]
    if not a.all:
        ev = [e for e in ev if (e.get("status") or "open") != "done"]
    if a.assignee:
        ev = [e for e in ev if (e.get("assignee") or "Any bot") in (a.assignee, "Any bot")]
    ev.sort(key=lambda e: (e.get("due") or "9999", str(e.get("ts"))))
    if a.json:
        print(json.dumps(ev, indent=1, ensure_ascii=False))
        return
    if not ev:
        print("No open follow-ups.")
    for e in ev:
        print(f"{e['event_id']}\t{e.get('status') or 'open'}\t{e.get('assignee') or 'Any bot'}\t{e.get('due') or '-'}\t{e.get('site_id')}\t{e.get('name', '')}\t{e.get('text', '')}")

def cmd_status(a, status):
    client = db()
    d = find_event(client, a.event_id)
    if not d or (d.to_dict() or {}).get("type") != "followup":
        die(f"no follow-up {a.event_id}")
    d.reference.update({"status": status, "done_at": dt.datetime.now(dt.timezone.utc) if status == "done" else None,
                        "done_by": a.by if status == "done" else None})
    print(f"[edks] {a.event_id} -> {status}")

def cmd_note(a):
    e = add_event(db(), a.site_id, "note", a.text, a.by)
    print(f"[edks] note {e['event_id']} on {e['name']}")

def cmd_followup(a):
    e = add_event(db(), a.site_id, "followup", a.text, a.by, assignee=a.assignee, due=a.due or "", status="open")
    print(f"[edks] follow-up {e['event_id']} on {e['name']} -> {a.assignee}")

def cmd_dump(a):
    ev = all_events(db())
    out = json.dumps({"generated_at": now_iso(), "events": ev}, indent=1, ensure_ascii=False)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        open(a.out, "w", encoding="utf-8").write(out)
        print(f"[edks] {len(ev)} events -> {a.out}")
    else:
        print(out)

def cmd_push_learnings(a):
    md = open(a.file, encoding="utf-8").read()
    db().collection("learnings").document("summary").set({"md": md[:MAX_DOC], "updated": now_iso()})
    print(f"[edks] learnings published ({len(md)} chars)")

def cmd_import(a):
    """Events from the old Sheet (CSV export of the Events tab) or a browser export. Idempotent."""
    client = db()
    rows = list(csv.DictReader(open(a.file, encoding="utf-8"))) if a.file.endswith(".csv") else json.load(open(a.file, encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("events", [])
    have = {e["event_id"] for e in all_events(client)}
    n = 0
    last_stage = {}
    for r in rows:
        eid, sid = str(r.get("event_id") or "").strip(), str(r.get("site_id") or "").strip()
        if not eid or not sid or eid in have:
            continue
        typ = r.get("type")
        if typ == "followup_status":  # the Sheet logged status flips as their own rows; applied below
            continue
        e = {k: r.get(k) for k in EV_FIELDS if r.get(k) not in (None, "")}
        e["ts"] = str(r.get("ts") or r.get("timestamp") or now_iso())
        e["event_id"], e["site_id"] = eid, sid
        if isinstance(e.get("reasons"), str):
            e["reasons"] = [x.strip() for x in e["reasons"].split(",") if x.strip()]
        ref = ensure_deal(client, sid, e.get("view") or "land", e.get("name") or "", e.get("by") or "import")
        ref.collection("events").document(eid).set(e)
        if typ == "stage" and e.get("to_stage") in STAGES and e["ts"] > last_stage.get(sid, ("",))[0]:
            last_stage[sid] = (e["ts"], e["to_stage"])
        have.add(eid); n += 1
    flips = sorted((r for r in rows if r.get("type") == "followup_status"), key=lambda r: str(r.get("ts") or r.get("timestamp")))
    for r in flips:
        d = find_event(client, str(r.get("ref_id")))
        if d:
            d.reference.update({"status": r.get("status") or "open"})
    for sid, (_, st) in last_stage.items():
        client.collection("deals").document(sid).set({"stage": st}, merge=True)
    print(f"[edks] imported {n} events ({len(flips)} follow-up status changes, {len(last_stage)} deal stages)")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("push"); p.add_argument("--data", required=True); p.add_argument("--storage")
    p.add_argument("--email-engine"); p.add_argument("--password-env", default=os.environ.get("PASSWORD_ENV", "EDIFICE_SITES_PASSWORD"))
    p = sp.add_parser("users"); p.add_argument("action", choices=["list", "add", "remove"]); p.add_argument("email", nargs="?")
    p.add_argument("--role", default="viewer"); p.add_argument("--name")
    p = sp.add_parser("followups"); p.add_argument("--assignee"); p.add_argument("--all", action="store_true"); p.add_argument("--json", action="store_true")
    for c in ("done", "reopen"):
        p = sp.add_parser(c); p.add_argument("event_id"); p.add_argument("--by", default="bot:cli")
    p = sp.add_parser("note"); p.add_argument("site_id"); p.add_argument("text"); p.add_argument("--by", default="bot:cli")
    p = sp.add_parser("followup"); p.add_argument("site_id"); p.add_argument("text"); p.add_argument("--assignee", default="Any bot")
    p.add_argument("--due"); p.add_argument("--by", default="bot:cli")
    p = sp.add_parser("dump"); p.add_argument("--out")
    p = sp.add_parser("push-learnings"); p.add_argument("file")
    p = sp.add_parser("import"); p.add_argument("file")
    a = ap.parse_args()
    {"push": cmd_push, "users": cmd_users, "followups": cmd_followups, "done": lambda a: cmd_status(a, "done"),
     "reopen": lambda a: cmd_status(a, "open"), "note": cmd_note, "followup": cmd_followup, "dump": cmd_dump,
     "push-learnings": cmd_push_learnings, "import": cmd_import}[a.cmd](a)

if __name__ == "__main__":
    main()
