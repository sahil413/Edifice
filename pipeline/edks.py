#!/usr/bin/env python3
"""Edifice deal pipeline on Firestore: the daily data push and the bots' command line.

Auth is keyless by default (credentials are never printed, logged or written by this script):
  Application Default Credentials  box: `gcloud auth application-default login`; CI: Workload Identity
                                   Federation via google-github-actions/auth (GOOGLE_APPLICATION_CREDENTIALS)
  (auth, keyless: ADC -> gcloud CLI login via `gcloud auth print-access-token`; WIF in CI)
  EDIFICE_IMPERSONATE_SA           act as this service account (needs roles/iam.serviceAccountTokenCreator)
  FIREBASE_PROJECT                 project id (default dms-edvc)
  EDIFICE_FIREBASE_SA              optional legacy key: a path or the key JSON (also ~/.config/edifice/firebase-sa.json)
  FIRESTORE_EMULATOR_HOST          talk to the local emulator instead (FIREBASE_PROJECT defaults to demo-edifice)

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
Manual sites (ids m-...; admin-only ones live in rsites/rdeals/revents):
  edks.py add-manual --spec FILE                           create/refresh a manual site + its deal + events from a JSON spec
  edks.py enrich [--all] [--cache-dir D] [--contacts F --curated F --local-review F --state F --tracker-review F --tracker-state F]
                                                           enrich pending manual sites (--all: rescore every manual site)
  edks.py delete-manual SITE_ID                            remove a manual site, its deal and its events
"""
import argparse, csv, re, datetime as dt, hashlib, json, os, subprocess, sys, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = ["New", "Under Consideration", "Reviewing", "Contacted", "In Diligence", "LOI Submitted", "Under Contract", "Closed", "Passed", "On Hold"]
ROLES = ("admin", "editor", "viewer")
MAX_DOC = 900_000  # Firestore caps a document at 1 MiB
EV_FIELDS = ("event_id", "site_id", "view", "name", "type", "ts", "from_stage", "to_stage", "reasons", "text",
             "assignee", "due", "status", "by", "done_at", "done_by")

def die(msg):
    sys.exit(f"[edks] {msg}")

def now_iso():
    t = dt.datetime.now(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"

KEY_FILE = os.path.expanduser("~/.config/edifice/firebase-sa.json")

def project_id():
    return os.environ.get("FIREBASE_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") \
        or os.environ.get("GCLOUD_PROJECT") or "dms-edvc"

def _key_source():
    """A service-account key, only if one was provided explicitly (keys are optional; keyless is preferred)."""
    s = os.environ.get("EDIFICE_FIREBASE_SA", "")
    if s:
        return s
    return KEY_FILE if os.path.exists(KEY_FILE) else ""

def _adc():
    """Application Default Credentials: gcloud user login on the box, or Workload Identity Federation in CI
    (google-github-actions/auth sets GOOGLE_APPLICATION_CREDENTIALS to a short-lived external-account config).
    EDIFICE_IMPERSONATE_SA=<sa email> makes the box act as the pipeline service account instead of the user."""
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError
    scopes = ["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/datastore"]
    adc_file = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if not (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.path.exists(adc_file)
            or os.environ.get("EDIFICE_ADC_METADATA")):
        return None  # no ADC configured: skip the slow metadata-server probe and fall back to the gcloud login
    try:
        cred, _ = google.auth.default(scopes=scopes)
        # A quota project only for end-user ADC; federated/service-account callers bill to their own project
        # (setting one there makes the WIF impersonation call need serviceusage.services.use).
        from google.oauth2 import credentials as _uc
        if isinstance(cred, _uc.Credentials) and not cred.quota_project_id:
            cred = cred.with_quota_project(project_id())
    except DefaultCredentialsError:
        return None
    sa = os.environ.get("EDIFICE_IMPERSONATE_SA")
    if sa:
        from google.auth import impersonated_credentials
        cred = impersonated_credentials.Credentials(source_credentials=cred, target_principal=sa,
                                                    target_scopes=scopes, lifetime=3600)
    return cred

def _gcloud_bin():
    import shutil
    b = shutil.which("gcloud")
    alt = os.path.expanduser("~/google-cloud-sdk/bin/gcloud")
    return b or (alt if os.path.exists(alt) else None)

def _gcloud():
    """Keyless fallback: the gcloud CLI user login (`gcloud auth login`). Tokens come from
    `gcloud auth print-access-token`, impersonating EDIFICE_IMPERSONATE_SA when set; they live only in memory."""
    gb = _gcloud_bin()
    if not gb or os.environ.get("EDIFICE_NO_GCLOUD"):
        return None
    import datetime, subprocess
    from google.auth import credentials as gac

    class GcloudCredentials(gac.Credentials):
        def refresh(self, request):
            cmd = [gb, "auth", "print-access-token", "--quiet"]
            sa = os.environ.get("EDIFICE_IMPERSONATE_SA")
            if sa:
                cmd.append("--impersonate-service-account=" + sa)
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 or not r.stdout.strip():
                die("gcloud could not issue an access token (run `gcloud auth login`; token not shown)")
            self.token = r.stdout.strip()
            self.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(minutes=45)

    r = subprocess.run([gb, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return GcloudCredentials()

def configured():
    """True when Firestore is reachable without prompting: emulator, explicit key, or ADC."""
    if os.environ.get("FIRESTORE_EMULATOR_HOST") or _key_source():
        return True
    try:
        return _adc() is not None or _gcloud() is not None
    except Exception:
        return False

def db():
    try:
        from google.cloud import firestore as gcf
    except ImportError:
        die("google-cloud-firestore is not installed (pip install firebase-admin)")
    if os.environ.get("FIRESTORE_EMULATOR_HOST"):
        from google.auth.credentials import AnonymousCredentials
        return gcf.Client(project=os.environ.get("FIREBASE_PROJECT", "demo-edifice"), credentials=AnonymousCredentials())
    src = _key_source()
    try:
        if src:
            from google.oauth2 import service_account
            info = json.loads(src) if src.lstrip().startswith("{") else json.load(open(src, encoding="utf-8"))
            cred = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/datastore"])
        else:
            cred = _adc() or _gcloud()
            if cred is None:
                die("no credentials: run `gcloud auth login` (box) or use Workload Identity (CI)")
    except SystemExit:
        raise
    except Exception:
        die("credentials could not be loaded (contents not shown)")
    return gcf.Client(project=project_id(), credentials=cred)

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
    # Manual sites (Add site / add-manual; ids "m-...") are owned by the enrich job: never overwritten or removed here.
    existing = {d.id: (d.to_dict() or {}) for d in client.collection("sites").select(["view", "h", "manual"]).stream()}
    manual = {rid for rid, d in existing.items() if d.get("manual")}
    writes, keep, changed = [], set(), 0
    # Rank with the learned weights: each site doc also carries its learned score (base score with the
    # bounded per-factor multipliers from learnings/weights); the app re-applies newer weights live.
    try:
        import learnings
        wd = client.collection("learnings").document("weights").get()
        W = wd.to_dict() if wd.exists else {}
    except Exception:
        W = {}
    def learned_of(view, r):
        deltas = (W.get("power" if view == "land" else "storage") or {}).get("deltas") or {}
        f = learnings.land_features(r) if view == "land" else learnings.storage_features(r)
        base, ls = learnings.learned_score(f, {}), learnings.learned_score(f, deltas)
        sc = r.get("overall", r.get("score"))
        if ls is None or base is None or not isinstance(sc, (int, float)):
            return sc
        return round(sc + (ls - base), 1)
    # Durable hand overrides (overrides/{site_id}.fields, admin SDK only): top-level fields that replace the
    # source values on every push, so a rename or merged description survives the LOIS refresh.
    OV = {d.id: (d.to_dict() or {}).get("fields") or {} for d in client.collection("overrides").stream()}
    for view, rows in views.items():
        for r in rows:
            rid = str(r["id"])
            if OV.get(rid):
                r.update(OV[rid])
            if "/" in rid or rid.startswith("__"):
                die(f"unusable site id {rid!r}")
            if rid in manual or rid.startswith("m-"):
                continue
            j = json.dumps(r, ensure_ascii=False, separators=(",", ":"))
            if len(j) > MAX_DOC:
                die(f"site {rid} is too large for one document ({len(j)} bytes)")
            keep.add(rid)
            lv = learned_of(view, r)
            h = _h(j + "|" + str(lv))
            if existing.get(rid, {}).get("h") == h:
                continue
            changed += 1
            writes.append(("set", client.collection("sites").document(rid),
                           {"view": view, "name": r.get("name") or "", "score": r.get("overall", r.get("score")), "learned": lv, "h": h, "j": j}))
    stale = [rid for rid, d in existing.items() if rid not in keep and d.get("view") in views and rid not in manual]
    for rid in stale:
        writes.append(("delete", client.collection("sites").document(rid), None))
    # meta/sites_index: {id: h} of every pipeline-owned site doc, so the app can read only changed docs and serve
    # the rest from its local cache (manual sites are excluded: the app reads those with their own query).
    hidx = {rid: existing.get(rid, {}).get("h") for rid in keep}
    for op, ref, data in writes:
        if op == "set" and ref.parent.id == "sites":
            hidx[ref.id] = data["h"]
    writes.append(("set", client.collection("meta").document("sites_index"), {"h": hidx, "n": len(hidx), "updated": now_iso()}))
    _put_blob(client, "land", json.dumps(land, ensure_ascii=False, separators=(",", ":")), writes)
    if stor is not None:
        _put_blob(client, "storage", json.dumps(stor, ensure_ascii=False, separators=(",", ":")), writes)
    eng = load_engine(a.email_engine, a.password_env)
    if eng is not None:
        writes.append(("set", client.collection("private").document("email_engine"), {"js": eng, "h": _h(eng), "updated": now_iso()}))
    _commit(client, writes)
    print(f"[edks] pushed: {len(keep)} sites ({changed} changed, {len(stale)} removed, {len(manual)} manual kept), meta {'land+storage' if stor is not None else 'land'}"
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

def cmd_verify(a):
    """CI-safe health check: counts only (no emails or site data are printed)."""
    client = db()
    roles = [((d.to_dict() or {}).get("role")) for d in client.collection("users").stream()]
    admins = roles.count("admin")
    sites = client.collection("sites").count().get()[0][0].value   # aggregation: 1 read, not one per site
    meta = sorted(d.id for d in client.collection("meta").stream())
    print(f"[verify] users={len(roles)} admins={admins} sites={sites} meta={','.join(meta)}")
    ok = admins >= a.min_admins and sites > 0
    if a.rules:
        import json as _j, urllib.request
        from google.auth.transport.requests import Request
        cred = client._credentials
        if not cred.valid:
            cred.refresh(Request())
        base = f"https://firebaserules.googleapis.com/v1/projects/{project_id()}"
        hdr = {"Authorization": "Bearer " + cred.token}
        def get(u):
            return _j.load(urllib.request.urlopen(urllib.request.Request(u, headers=hdr)))
        try:
            rel = get(base + "/releases/cloud.firestore")
            live = get("https://firebaserules.googleapis.com/v1/" + rel["rulesetName"])["source"]["files"][0]["content"]
            same = live == open(a.rules, encoding="utf-8").read()
            print(f"[verify] rules released={rel['rulesetName'].rsplit('/', 1)[-1]} matches file: {same}")
            ok = ok and same
        except Exception as e:
            body = e.read()[:200].decode("utf-8", "replace") if hasattr(e, "read") else ""
            print(f"[verify] rules check failed: {type(e).__name__} {getattr(e, 'code', '')} {body}")
            ok = False
    if not ok:
        die("verification failed")

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

def locate(client, sid):
    """(view, name, restricted) for a member or admin-only site."""
    for restricted in (False, True):
        s = client.collection(_cols(restricted)[0]).document(sid).get()
        if s.exists:
            d = s.to_dict() or {}
            return d.get("view") or "land", d.get("name", ""), restricted
    die(f"no site {sid}")

# ------------------------------------------------------------------ review queue (admin-only)
# Every stage change files reviewQueue/{event_id}: pending -> asked (the routine asked Sahil why) -> answered
# (his answer is written back to the deal as a note event and feeds the learned weights).
def track_of(view):
    return "storage" if view == "storage" else "power"

def queue_review(client, e, restricted=False):
    ref = client.collection("reviewQueue").document(e["event_id"])
    if ref.get().exists:
        return False
    ref.set({"event_id": e["event_id"], "site_id": e["site_id"], "view": e.get("view") or "land", "name": e.get("name", ""),
             "track": track_of(e.get("view")), "from_stage": e.get("from_stage") or "", "to_stage": e.get("to_stage") or "",
             "who": e.get("by") or "", "reasons": list(e.get("reasons") or []), "text": e.get("text") or "", "ts": e.get("ts") or now_iso(),
             "status": "pending", "restricted": bool(restricted), "created": dt.datetime.now(dt.timezone.utc)})
    return True

def _relearn(client):
    try:
        import learnings
        learnings.recompute(client, quiet=True)
    except Exception as ex:  # never block a stage write on the model
        print(f"[edks] WARNING: weights not refit ({type(ex).__name__})")

def cmd_stage(a):
    client = db()
    view, name, restricted = locate(client, a.site_id)
    _, dcol, ecol = _cols(restricted)
    dref = client.collection(dcol).document(a.site_id); snap = dref.get()
    frm = (snap.to_dict() or {}).get("stage", "New") if snap.exists else "New"
    if a.to_stage not in STAGES:
        die(f"stage must be one of: {', '.join(STAGES)}")
    eid = a.event_id or (dt.datetime.now().strftime("%y%m%d") + uuid.uuid4().hex[:10])
    if dref.collection(ecol).document(eid).get().exists:
        print(f"[edks] {eid} already recorded"); return
    e = {"event_id": eid, "site_id": a.site_id, "view": view, "name": name, "type": "stage", "ts": a.ts or now_iso(),
         "from_stage": frm, "to_stage": a.to_stage, "reasons": [x.strip() for x in (a.reasons or "").split(",") if x.strip()],
         "text": a.text or "", "by": a.by}
    dref.set({"site_id": a.site_id, "view": view, "name": name, "stage": a.to_stage,
              "updated": dt.datetime.now(dt.timezone.utc), "updated_by": a.by}, merge=True)
    dref.collection(ecol).document(eid).set(e)
    queue_review(client, e, restricted)
    if not restricted:
        _relearn(client)
    print(f"[edks] {name}: {frm} -> {a.to_stage} ({eid}); review queued")

def _iso(v):
    return v.isoformat() if isinstance(v, dt.datetime) else v

def email_context(client, sid, k=3):
    items = []
    for d in client.collection("dealEmails").document(sid).collection("items").stream():
        x = d.to_dict() or {}
        items.append({f: _iso(x.get(f)) for f in ("date", "account", "from", "to", "subject", "excerpt", "link", "dir")})
    items.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    summ = client.collection("dealEmails").document(sid).get()
    return {"summary": (summ.to_dict() or {}).get("summary", "") if summ.exists else "", "recent": items[:k], "total": len(items)}

def cmd_reviews(a):
    client = db()
    q = client.collection("reviewQueue")
    rows = []
    for d in q.stream():
        x = {k: _iso(v) for k, v in (d.to_dict() or {}).items()}
        x["id"] = d.id
        if a.status != "all" and x.get("status") != a.status:
            continue
        if a.track and x.get("track") != a.track:
            continue
        rows.append(x)
    rows.sort(key=lambda x: str(x.get("ts") or ""))
    for x in rows:
        x["email_context"] = email_context(client, x["site_id"])
        x["question"] = (f"{x.get('name')}: moved {x.get('from_stage') or 'New'} -> {x.get('to_stage')} by {x.get('who')}"
                         + (f" (reasons: {', '.join(x.get('reasons') or [])})" if x.get("reasons") else "")
                         + ". What drove this decision, and what would change it?")
    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False, default=str)); return
    if not rows:
        print(f"No {a.status} reviews."); return
    for x in rows:
        print(f"{x['id']}	{x.get('status')}	{x.get('track')}	{x.get('ts')}	{x.get('site_id')}	{x.get('name')}	"
              f"{x.get('from_stage') or 'New'} -> {x.get('to_stage')}	{x.get('who')}	{', '.join(x.get('reasons') or [])}	{x.get('text') or ''}")
        c = x["email_context"]
        if c["summary"]:
            print(f"    email: {c['summary']}")
        for m in c["recent"]:
            print(f"    {str(m.get('date'))[:16]} [{m.get('account')}] {m.get('dir')} {m.get('subject')} {m.get('link')}")

def _review(client, rid):
    ref = client.collection("reviewQueue").document(rid); snap = ref.get()
    if not snap.exists:
        die(f"no review {rid}")
    return ref, snap.to_dict() or {}

def cmd_mark_asked(a):
    client = db(); n = 0
    for rid in a.review_ids:
        ref, x = _review(client, rid)
        if x.get("status") == "pending":
            ref.update({"status": "asked", "asked_at": dt.datetime.now(dt.timezone.utc), "asked_via": a.via}); n += 1
    print(f"[edks] {n} review(s) marked asked")

def cmd_answer(a):
    client = db()
    ref, x = _review(client, a.review_id)
    if x.get("status") == "answered" and not a.force:
        print(f"[edks] {a.review_id} already answered (event {x.get('answer_event_id')}); use --force to add another"); return
    restricted = bool(x.get("restricted"))
    _, dcol, ecol = _cols(restricted)
    eid = "a" + hashlib.sha1(f"{a.review_id}|{a.text}".encode()).hexdigest()[:15]
    rs = [r.strip() for r in (a.reasons or "").split(",") if r.strip()]
    e = {"event_id": eid, "site_id": x["site_id"], "view": x.get("view") or "land", "name": x.get("name", ""), "type": "note",
         "ts": now_iso(), "by": a.by, "review_id": a.review_id, "reasons": rs,
         "text": f"Why {x.get('from_stage') or 'New'} -> {x.get('to_stage')}: {a.text}"}
    dref = client.collection(dcol).document(x["site_id"])
    if not dref.get().exists:
        ensure_deal(client, x["site_id"], e["view"], e["name"], a.by)
    dref.collection(ecol).document(eid).set(e)
    ref.update({"status": "answered", "answer": a.text, "answer_reasons": rs, "answered_at": dt.datetime.now(dt.timezone.utc),
                "answered_by": a.by, "answer_event_id": eid})
    if not restricted:
        _relearn(client)
    print(f"[edks] {a.review_id} answered -> note {eid} on {x.get('name')}")

def cmd_backfill_reviews(a):
    """File reviews for stage events since --since that have none yet (member and admin-only deals)."""
    client = db(); n = 0
    for restricted in (False, True):
        _, dcol, ecol = _cols(restricted)
        for d in client.collection_group(ecol).stream():
            e = d.to_dict() or {}
            if e.get("type") != "stage" or str(e.get("ts", "")) < a.since:
                continue
            e.setdefault("event_id", d.id); e.setdefault("site_id", d.reference.parent.parent.id)
            n += queue_review(client, e, restricted)
    print(f"[edks] backfill: {n} review(s) filed")

def cmd_migrate_under_consideration(a):
    """One-time relabel: every existing site still in New (explicit or implicit) -> Under Consideration.
    Writes only the deal doc (updated_by system:migration): no stage event, no review item, ignored by learn.
    Sites added afterwards have no deal doc and default to New."""
    client = db(); n = {}
    for restricted in (False, True):
        scol, dcol, _ = _cols(restricted)
        for s in client.collection(scol).select(["view", "name"]).stream():
            d = s.to_dict() or {}; view = d.get("view")
            if view not in ("land", "storage") or s.id.startswith("__"):
                continue
            dref = client.collection(dcol).document(s.id); snap = dref.get()
            cur = (snap.to_dict() or {}).get("stage", "New") if snap.exists else "New"
            k = (view, "admin-only" if restricted else "members")
            if cur != "New":
                n[k + ("kept",)] = n.get(k + ("kept",), 0) + 1; continue
            n[k + ("moved",)] = n.get(k + ("moved",), 0) + 1
            if not a.dry_run:
                dref.set({"site_id": s.id, "view": view, "name": d.get("name") or "", "stage": "Under Consideration",
                          "updated": dt.datetime.now(dt.timezone.utc), "updated_by": "system:migration"}, merge=True)
    for k in sorted(n):
        print(f"[edks] migrate{' (dry run)' if a.dry_run else ''}: {' '.join(k)} {n[k]}")

def cmd_learn(a):
    import learnings
    learnings.recompute(db(), force=a.force)

def cmd_senders(a):
    """config/senders: the Gmail accounts allowed to send from the app (admin-only doc; addresses given at runtime)."""
    client = db(); ref = client.collection("config").document("senders")
    if a.emails:
        em = [x.strip().lower() for x in a.emails.split(",") if x.strip()]
        ref.set({"emails": em, "updated": dt.datetime.now(dt.timezone.utc)}, merge=True)
    cid = (getattr(a, "gmail_client_id", None) or "").strip()
    if cid:
        if not re.fullmatch(r"[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com", cid):
            sys.exit("[edks] --gmail-client-id should look like 1234-abc.apps.googleusercontent.com")
        ref.set({"gmail_client_id": cid, "updated": dt.datetime.now(dt.timezone.utc)}, merge=True)
    snap = ref.get(); d = (snap.to_dict() or {}) if snap.exists else {}
    print(f"[edks] senders: {len(d.get('emails') or [])} account(s) configured; send-from-app client id {'set' if d.get('gmail_client_id') else 'not set'}")

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
    if typ == "stage":
        queue_review(client, e)
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
        if typ == "stage":
            queue_review(client, e)
        if typ == "stage" and e.get("to_stage") in STAGES and e["ts"] > last_stage.get(sid, ("",))[0]:
            last_stage[sid] = (e["ts"], e["to_stage"])
        have.add(eid); n += 1
    flips = sorted((r for r in rows if r.get("type") == "followup_status"), key=lambda r: str(r.get("ts") or r.get("timestamp")))
    for r in flips:
        d = find_event(client, str(r.get("ref_id")))
        if d:
            d.reference.update({"status": r.get("status") or "open"})
    for sid, (_, st) in last_stage.items():
        client.collection("deals").document(sid).set({"stage": st, "updated": dt.datetime.now(dt.timezone.utc)}, merge=True)
    if last_stage:
        _relearn(client)
    print(f"[edks] imported {n} events ({len(flips)} follow-up status changes, {len(last_stage)} deal stages)")

# ------------------------------------------------------------------ manual sites
def _cols(restricted):
    return ("rsites", "rdeals", "revents") if restricted else ("sites", "deals", "events")

def _slug(name):
    import re
    return "m-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:34] or uuid.uuid4().hex[:8])

def _listed_ok(v):
    try:
        d = dt.date.fromisoformat(str(v))
    except ValueError:
        die(f"listed date must be YYYY-MM-DD, got {v!r}")
    if d > dt.datetime.now(__import__("zoneinfo").ZoneInfo("America/New_York")).date():
        die(f"listed date {d} is in the future")
    return d.isoformat()

def cmd_set_listed(a):
    """Set (or --clear) a site's listed date for Days on market. Writes only deals/rdeals {listed, updated,
    updated_by}; stage is never touched. The sync push never writes deal docs, so the date is never overwritten."""
    client = db()
    view, name, restricted = locate(client, a.site_id)
    dref = client.collection(_cols(restricted)[1]).document(a.site_id)
    snap = dref.get(); cur = (snap.to_dict() or {}) if snap.exists else {}
    data = {"site_id": a.site_id, "view": cur.get("view") or view, "name": cur.get("name") or name,
            "updated": dt.datetime.now(dt.timezone.utc), "updated_by": a.by}
    if not cur.get("stage"):
        data["stage"] = "New"
    if a.clear:
        from google.cloud import firestore as gcf
        data["listed"] = gcf.DELETE_FIELD; msg = "cleared"
    else:
        if not a.date: die("give a date (YYYY-MM-DD) or --clear")
        data["listed"] = _listed_ok(a.date); msg = data["listed"]
    dref.set(data, merge=True)
    print(f"[edks] {a.site_id}: listed date {msg} (was {cur.get('listed') or 'unset'}); stage unchanged ({cur.get('stage') or 'New'})")

def cmd_add_manual(a):
    """Spec: {id?, restricted?, name, input, edits{}, extra{}, stage, by, events:[{type, text, reasons, to_stage,
    from_stage, assignee, due, ts, key}]}. Idempotent: event ids come from the spec."""
    sp = json.load(open(a.spec, encoding="utf-8"))
    client = db()
    sid = sp.get("id") or _slug(sp["name"]); restricted = bool(sp.get("restricted"))
    scol, dcol, ecol = _cols(restricted)
    other = client.collection(_cols(not restricted)[0]).document(sid).get()
    if other.exists:
        die(f"{sid} already exists in the other collection")
    by = sp.get("by") or "bot:Coding Agent"
    ref = client.collection(scol).document(sid); snap = ref.get()
    doc = {"view": "land", "name": sp["name"], "manual": True, "status": "pending", "input": sp.get("input", ""),
           "edits": sp.get("edits") or {}, "extra": sp.get("extra") or {}, "restricted": restricted,
           "edited_by": by, "edited": dt.datetime.now(dt.timezone.utc)}
    if not snap.exists:
        doc.update(created_by=by, created=dt.datetime.now(dt.timezone.utc))
    ref.set(doc, merge=True)
    dref = client.collection(dcol).document(sid)
    dref.set({"site_id": sid, "view": "land", "name": sp["name"], "stage": sp.get("stage") or "New",
              "updated": dt.datetime.now(dt.timezone.utc), "updated_by": by}, merge=True)
    if sp.get("listed"):
        dref.set({"listed": _listed_ok(sp["listed"])}, merge=True)
    n = 0
    for i, e in enumerate(sp.get("events") or []):
        eid = "m" + hashlib.sha1(f"{sid}|{e.get('key') or i}".encode()).hexdigest()[:15]
        ev = {"event_id": eid, "site_id": sid, "view": "land", "name": sp["name"], "type": e["type"],
              "ts": e.get("ts") or now_iso(), "text": e.get("text", ""), "by": by}
        for k in ("from_stage", "to_stage", "reasons", "assignee", "due", "status"):
            if e.get(k) not in (None, ""): ev[k] = e[k]
        if e["type"] == "followup": ev.setdefault("status", "open")
        if not dref.collection(ecol).document(eid).get().exists:
            dref.collection(ecol).document(eid).set(ev); n += 1
            if ev["type"] == "stage":
                queue_review(client, ev, restricted)
    print(f"[edks] manual site {sid} ({'admin-only' if restricted else 'members'}): stage {sp.get('stage') or 'New'}, {n} new events")

def cmd_delete_manual(a):
    client = db(); n = 0
    for restricted in (False, True):
        scol, dcol, ecol = _cols(restricted)
        ref = client.collection(scol).document(a.site_id); snap = ref.get()
        if snap.exists and not (snap.to_dict() or {}).get("manual"):
            die(f"{a.site_id} is not a manual site")
        if snap.exists: ref.delete(); n += 1
        dref = client.collection(dcol).document(a.site_id)
        for e in dref.collection(ecol).stream(): e.reference.delete(); n += 1
        if dref.get().exists: dref.delete(); n += 1
    print(f"[edks] deleted {n} docs for {a.site_id}")

# Fields each enrich step produces. If a step fails, the prior record's values are kept rather than dropped.
STEP_FIELDS = {"community": ["community", "annexation", "_issues"], "tracker": ["outreach"]}

def keep_prior(sites, prev, ok):
    """Copy fields from each site's last good record when the step that makes them failed (or was skipped),
    so a failed step never wipes e.g. an outreach plan. Returns the number of sites touched."""
    n = 0
    for r in sites:
        p = prev.get(r.get("id")) or {}
        got = []
        for step, fields in STEP_FIELDS.items():
            if ok.get(step, True): continue
            for f in fields:
                if f in p and (f != "annexation" or not r.get(f)):
                    r[f] = p[f]; got.append(f)
        if "community" in got and isinstance((r.get("community") or {}).get("score"), (int, float)):
            # infrastructure is fresh; re-blend with the kept community score (same weights/cap as community.py)
            r["infra_score"] = r.get("score")
            r["overall"] = round(0.6 * (r.get("score") or 0) + 0.4 * r["community"]["score"])
            fpp = (r.get("floodplain") or {}).get("pct")
            if fpp is not None and fpp > 50: r["overall"] = min(r["overall"], 30)
        if got:
            r["stale"] = sorted({s for s, v in ok.items() if not v})
            n += 1
    return n

def cmd_enrich(a):
    """Enrich queued manual sites (status pending) from public sources; --all rescores every manual site.
    Logs counts only (CI logs are public)."""
    import shutil, tempfile
    sys.path.insert(0, HERE)
    client = db()
    todo = []
    for restricted in (False, True):
        scol = _cols(restricted)[0]
        q = client.collection(scol).where("manual", "==", True)
        for d in q.stream():
            x = d.to_dict() or {}
            if a.all or x.get("status") == "pending" or (a.site and d.id == a.site):
                todo.append((scol, d.id, x))
    if a.site: todo = [t for t in todo if t[1] == a.site]
    if a.count:
        print(len(todo)); return
    if not todo:
        print("[edks] enrich: nothing queued"); return
    # Steps run as subprocesses with cwd=HERE, so relative paths must be made absolute first
    # (a relative --tracker-review once made the tracker step fail and wiped outreach plans).
    for k in ("cache_dir", "geo_cache_dir", "contacts", "curated", "local_review", "state", "tracker_review", "tracker_state"):
        if getattr(a, k, None): setattr(a, k, os.path.abspath(getattr(a, k)))
    import scrape, manual
    G = None
    if any((x.get("input") or (x.get("edits") or {}).get("lat") or (x.get("extra") or {}).get("site_geojson")) for _, _, x in todo):
        G = scrape.load_geo(a.geo_cache_dir or a.cache_dir or tempfile.mkdtemp())
        G["flood_cache"] = None     # manual sites are private: never write their floodplain lookups to the (public) actions cache
    prev = {}                       # last good record per site, to keep fields a failed step could not recompute
    for _, sid, x in todo:
        try: prev[sid] = json.loads(x.get("j") or "{}")
        except Exception: prev[sid] = {}
    curated = json.load(open(a.contacts)) if a.contacts and os.path.exists(a.contacts) else {}
    recs, meta, xtra = [], {}, {}
    for scol, sid, x in todo:
        if (prev.get(sid) or {}).get("floodplain"): x = {**x, "_fp_prev": prev[sid]["floodplain"]}
        try:
            r = manual.build(sid, x, G)
        except Exception as e:
            print(f"[edks] enrich: one site failed to build ({type(e).__name__})"); continue
        r["id"] = sid
        if curated: scrape.attach_contacts(r, curated)
        recs.append(r); meta[sid] = (scol, x.get("edited") or x.get("created")); xtra[sid] = x.get("extra") or {}
    tmp = tempfile.mkdtemp(prefix="enrich-")
    try:
        data = os.path.join(tmp, "sites.json")
        json.dump({"generated_at": now_iso(), "sites": recs, "sources": {}, "count": len(recs)}, open(data, "w"))
        def run(args, label):
            p = subprocess.run([sys.executable, "-W", "ignore"] + args, capture_output=True, text=True, cwd=HERE)
            print(f"[edks] enrich: {label} {'ok' if p.returncode == 0 else 'failed (exit %d)' % p.returncode}")
            if p.returncode != 0:
                lines = (p.stderr or p.stdout or "").strip().splitlines()
                if os.environ.get("GITHUB_ACTIONS"):   # public repo: CI logs get the error type only, never site data
                    last = lines[-1] if lines else ""
                    print("[edks]   " + (last.split(":", 1)[0] if last else "no output"))
                else:
                    print("\n".join("[edks]   " + ln for ln in lines[-12:]))
            return p.returncode == 0
        ok = {}
        def tmpcopy(f):
            if not f or not os.path.exists(f): return None
            d = os.path.join(tmp, os.path.basename(f)); shutil.copy(f, d); return d
        if a.curated and os.path.exists(a.curated):
            st = tmpcopy(a.state) or os.path.join(tmp, "state.json")
            if not os.path.exists(st): json.dump({}, open(st, "w"))
            args = [os.path.join(HERE, "community.py"), "--data", data, "--curated", a.curated, "--state", st]
            if a.cache_dir: args += ["--cache-dir", os.path.join(a.cache_dir, "community")]
            if a.local_review: args += ["--local-review", a.local_review]
            ok["community"] = run(args, "community")
        else:
            ok["community"] = False; print("[edks] enrich: community skipped (--curated file not found)")
        if ok["community"] and a.tracker_review and os.path.exists(a.tracker_review):
            ts = tmpcopy(a.tracker_state) or os.path.join(tmp, "tracker_state.json")
            if not os.path.exists(ts): json.dump({}, open(ts, "w"))
            ok["tracker"] = run([os.path.join(HERE, "tracker.py"), "--data", data, "--review", a.tracker_review, "--state", ts, "--offline"], "tracker")
        else:
            ok["tracker"] = False
            if ok["community"]: print("[edks] enrich: tracker skipped (--tracker-review file not found)")
        out = json.load(open(data))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if getattr(a, "dry_run", False):
        keep_prior(out["sites"], prev, ok)
        dst = a.dry_run if isinstance(a.dry_run, str) else os.path.join(tempfile.gettempdir(), "enrich-dry.json")
        json.dump(out, open(dst, "w"), ensure_ascii=False)
        print(f"[edks] enrich: dry run, nothing written ({len(out['sites'])} records in {dst})"); return
    issues = out.get("issues") or {}
    kept = keep_prior(out["sites"], prev, ok)
    if kept: print(f"[edks] enrich: kept prior fields on {kept} site(s) because a step failed")
    n = 0
    for r in out["sites"]:
        sid = r["id"]; scol, stamp = meta[sid]
        for k, v in xtra[sid].items():       # hand-curated facts (e.g. a recent annexation) beat the public layers
            if k in ("community", "annexation") and isinstance(v, dict): r[k] = {**(r.get(k) or {}), **v}
        ids = set()
        def walk(v):
            if isinstance(v, dict):
                for k, w in v.items():
                    if k in ("issue_id", "issue") and isinstance(w, str) and w in issues: ids.add(w)
                    walk(w)
            elif isinstance(v, list):
                for w in v:
                    if isinstance(w, str) and w in issues: ids.add(w)
                    walk(w)
        walk(r.get("community")); walk(r.get("annexation"))
        if ids: r["_issues"] = {i: issues[i] for i in ids}
        j = json.dumps(r, ensure_ascii=False, separators=(",", ":"))
        if len(j) > MAX_DOC:
            print("[edks] enrich: one record too large; skipped"); continue
        ref = client.collection(scol).document(sid)
        cur = (ref.get().to_dict() or {})
        still = (cur.get("edited") or cur.get("created")) == stamp     # re-queued by an edit while we worked?
        ref.update({"j": j, "h": _h(j), "score": r.get("overall", r.get("score")), "name": (cur.get("edits") or {}).get("name") or cur.get("name") or r.get("name") or "",
                    "status": "enriched" if still else "pending", "enriched": dt.datetime.now(dt.timezone.utc),
                    "unknown": r.get("unknown", [])})
        n += 1
    print(f"[edks] enrich: {n} of {len(todo)} manual sites updated")

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
    p = sp.add_parser("add-manual"); p.add_argument("--spec", required=True)
    p = sp.add_parser("delete-manual"); p.add_argument("site_id")
    p = sp.add_parser("set-listed", help="set a site's listed date (Days on market)"); p.add_argument("site_id"); p.add_argument("date", nargs="?")
    p.add_argument("--clear", action="store_true"); p.add_argument("--by", default="bot:cli")
    p = sp.add_parser("enrich"); p.add_argument("--all", action="store_true"); p.add_argument("--site"); p.add_argument("--count", action="store_true")
    p.add_argument("--dry-run", nargs="?", const=True, default=False, help="run every step but write nothing (optional: output JSON path)")
    for k in ("--cache-dir", "--geo-cache-dir", "--contacts", "--curated", "--local-review", "--state", "--tracker-review", "--tracker-state"):
        p.add_argument(k)
    p = sp.add_parser("verify"); p.add_argument("--min-admins", type=int, default=1); p.add_argument("--rules")
    p = sp.add_parser("stage", help="move a deal and file a review"); p.add_argument("site_id"); p.add_argument("to_stage")
    p.add_argument("--reasons", default=""); p.add_argument("--text", default=""); p.add_argument("--by", default="bot:cli")
    p.add_argument("--ts"); p.add_argument("--event-id")
    p = sp.add_parser("reviews", help="list the review queue with matched email context")
    p.add_argument("--status", default="pending", choices=["pending", "asked", "answered", "all"]); p.add_argument("--track", choices=["power", "storage"])
    p.add_argument("--json", action="store_true")
    p = sp.add_parser("mark-asked"); p.add_argument("review_ids", nargs="+"); p.add_argument("--via", default="bot")
    p = sp.add_parser("answer", help="write Sahil's answer back as a note and mark the review answered")
    p.add_argument("review_id"); p.add_argument("text"); p.add_argument("--reasons", default=""); p.add_argument("--by", default="bot:cli")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("backfill-reviews"); p.add_argument("--since", default="2026-10-01")
    p = sp.add_parser("migrate-under-consideration"); p.add_argument("--dry-run", action="store_true")
    p = sp.add_parser("learn", help="refit learnings/weights"); p.add_argument("--force", action="store_true")
    p = sp.add_parser("senders"); p.add_argument("--emails", help="comma-separated; omit to just count"); p.add_argument("--gmail-client-id", help="OAuth Web client ID for send-from-app (not a secret)")
    a = ap.parse_args()
    {"push": cmd_push, "users": cmd_users, "followups": cmd_followups, "done": lambda a: cmd_status(a, "done"),
     "reopen": lambda a: cmd_status(a, "open"), "note": cmd_note, "followup": cmd_followup, "dump": cmd_dump,
     "push-learnings": cmd_push_learnings, "import": cmd_import, "verify": cmd_verify,
     "add-manual": cmd_add_manual, "delete-manual": cmd_delete_manual, "set-listed": cmd_set_listed, "enrich": cmd_enrich,
     "stage": cmd_stage, "reviews": cmd_reviews, "mark-asked": cmd_mark_asked, "answer": cmd_answer,
     "backfill-reviews": cmd_backfill_reviews, "learn": cmd_learn, "senders": cmd_senders, "migrate-under-consideration": cmd_migrate_under_consideration}[a.cmd](a)

if __name__ == "__main__":
    main()
