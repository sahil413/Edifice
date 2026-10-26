#!/usr/bin/env python3
"""Render sites.json into the single-page app and (optionally) encrypt it with StaticCrypt.

Plaintext HTML is written only to a temp dir (or --plain-out, which must be outside the repo
or gitignored). The password is read from an environment variable and passed to StaticCrypt via
its STATICRYPT_PASSWORD env var, never on the command line, never printed, never written.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CERT_LIST_URL = "https://www.kansascommerce.gov/program/business-incentives-and-services/certified-sites-program/sites/"
# Fixed public salt so "remember me" survives daily rebuilds (salt is not secret; it ships in the output).
SALT = "4d7ebf500422fa19f7b747f4920395aa"

EMAIL_STUB = "function emailSec(){return ''}function emInit(){}function landContacts(){return []}"

def load_email_engine(path, password_env):
    """The draft-email engine (templates, personas, lint lists) is private: it lives encrypted in
    pipeline/email_engine.enc and is only ever inlined into the page before StaticCrypt runs.
    Plaintext source stays in private/ (gitignored). Falls back to a stub that hides the panel."""
    if path and os.path.exists(path):
        return open(path, encoding="utf-8").read()
    enc = os.path.join(HERE, "email_engine.enc")
    if os.path.exists(enc) and os.environ.get(password_env):
        r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                            "-pass", f"env:{password_env}", "-in", enc], capture_output=True)
        if r.returncode == 0:
            return r.stdout.decode("utf-8")
        print("[render] WARNING: could not decrypt email_engine.enc; draft-email panel disabled")
    else:
        print("[render] WARNING: no email engine available; draft-email panel disabled")
    return EMAIL_STUB

def load_pipeline_config(password_env):
    """Deal-pipeline client config (Apps Script URL, shared token, admin users). Kept encrypted in
    pipeline/pipeline_config.enc; repo secrets PIPELINE_URL / PIPELINE_TOKEN override it in CI.
    It is only inlined into the page before StaticCrypt runs."""
    cfg = {}
    enc = os.path.join(HERE, "pipeline_config.enc")
    if os.path.exists(enc) and os.environ.get(password_env):
        r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                            "-pass", f"env:{password_env}", "-in", enc], capture_output=True)
        if r.returncode == 0:
            try:
                cfg = json.loads(r.stdout.decode("utf-8"))
            except ValueError:
                print("[render] WARNING: pipeline_config.enc is not valid JSON")
        else:
            print("[render] WARNING: could not decrypt pipeline_config.enc")
    for key, env in (("url", "PIPELINE_URL"), ("token", "PIPELINE_TOKEN")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    print(f"[render] pipeline sync: {'configured' if cfg.get('url') and cfg.get('token') else 'not configured (browser-only)'}; users {len(cfg.get('users') or [])}")
    return cfg

def build_html(data, engine=EMAIL_STUB):
    data = dict(data, cert_list_url=CERT_LIST_URL)
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    tpl = tpl.replace("/*__EMAIL__*/", engine.replace("</script", "<\\/script"))
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")
    return tpl.replace("/*__DATA__*/", js)

FB_VER = "13.1.0"
FB_KEYS = ("apiKey", "authDomain", "projectId", "appId", "storageBucket", "messagingSenderId")

def firebase_config(env_name, emulator):
    """Public Firebase web config (identifies the project; not a secret). From env/repo variable."""
    if emulator:
        return {"apiKey": "demo-key", "authDomain": "localhost", "projectId": os.environ.get("FIREBASE_PROJECT", "demo-edifice")}
    raw = os.environ.get(env_name, "")
    try:
        cfg = json.loads(raw) if raw else None
    except ValueError:
        cfg = None
    if not cfg or not all(cfg.get(k) for k in ("apiKey", "authDomain", "projectId", "appId")):
        sys.exit(f"[render] {env_name} must hold the Firebase web config JSON (apiKey, authDomain, projectId, appId)")
    return {k: cfg[k] for k in FB_KEYS if cfg.get(k)}

def build_firebase_html(cfg, emulator=False):
    """The sign-in shell: the app code (already public in this repo as template.html) with no data and no
    draft engine. Both are read from Firestore after an allowed Google account signs in."""
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    head = '<script id="data" type="application/json">/*__DATA__*/</script>\n<script>\n'
    if tpl.count(head) != 1:
        sys.exit("[render] template layout changed: cannot find the data/app script pair")
    tpl = tpl.replace(head, '<script id="data" type="application/json">null</script>\n<script type="text/plain" id="appjs">\n')
    shell = open(os.path.join(HERE, "firebase_shell.html"), encoding="utf-8").read()
    shell = shell.replace("__FB_VER__", FB_VER).replace("__FB_CONFIG__", json.dumps(cfg)).replace("__FB_EMU__", "true" if emulator else "false")
    tpl = tpl.replace("<body>", '<body class="gated">', 1)
    i = tpl.rindex("</body>")
    return tpl[:i] + shell + "\n" + tpl[i:]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

def data_probes(data, engine=None):
    probes = set()
    for s in data["sites"]:
        if len(s["name"]) >= 8: probes.add(s["name"])
        if len(s.get("address") or "") >= 8: probes.add(s["address"])
        for c in s.get("contacts", []):
            if c.get("email"): probes.add(c["email"])
            if len(c.get("phone") or "") >= 10: probes.add(c["phone"])
    for i in (data.get("issues") or {}).values():
        if len(i.get("title") or "") >= 12: probes.add(i["title"])
        if i.get("summary") and len(i["summary"]) >= 30: probes.add(i["summary"][:60])
    st = data.get("storage") or {}
    for r in st.get("rows", []):
        if len(r.get("name") or "") >= 8: probes.add(r["name"])
        if len(r.get("address") or "") >= 8: probes.add(r["address"])
        for c in r.get("contacts", []):
            if c.get("email"): probes.add(c["email"])
    of = st.get("own_facility") or {}
    for k in ("name", "address"):
        if len(of.get(k) or "") >= 6: probes.add(of[k])
    for s in data["sites"]:
        o = s.get("outreach")
        if o and len(o["next_step"]["text"]) >= 30: probes.add(o["next_step"]["text"][:60])
    for it in (data.get("tracker") or {}).get("items", []):
        if len(it.get("summary") or "") >= 30: probes.add(it["summary"][:60])
    probes |= {"function genDraft", "edks_prefs_", "Kansas-based investor"}
    if engine and engine != EMAIL_STUB:
        probes |= {ln.strip()[:60] for ln in engine.splitlines() if len(ln.strip()) >= 40 and "function" in ln}
    return probes

def verify_public(path, data, engine=None):
    """Firebase shell: no site data, no contacts, no draft engine, no email addresses."""
    html = open(path, encoding="utf-8").read()
    probes = data_probes(data, engine)
    leaks = [p for p in probes if p in html]
    emails = sorted(set(EMAIL_RE.findall(html)))
    if leaks:
        sys.exit(f"[render] PLAINTEXT LEAK: {len(leaks)} data strings found in {path}")
    if emails:
        sys.exit(f"[render] LEAK: {len(emails)} email address(es) found in {path}")
    if "/*__DATA__*/" in html or '"sites":[' in html or "staticrypt" in html.lower():
        sys.exit(f"[render] {path} does not look like the sign-in shell")
    print(f"[render] verified {path}: 0 of {len(probes)} data probes, 0 email addresses, {len(html)} bytes")

def encrypt(plain_path, out_dir, password_env):
    pw = os.environ.get(password_env)
    if not pw:
        sys.exit(f"[render] env var {password_env} is not set; refusing to publish unencrypted output")
    env = dict(os.environ, STATICRYPT_PASSWORD=pw)
    cmd = ["npx", "-y", "staticrypt@3", plain_path, "-d", out_dir, "-c", "false", "--short", "--salt", SALT,
           "--remember", "30",
           "--template-title", "Edifice — Kansas Sites",
           "--template-instructions", "Private research. Enter the password to continue.",
           "--template-button", "Unlock", "--template-placeholder", "Password",
           "--template-color-primary", "#F14B21", "--template-color-secondary", "#0A0A0A"]
    r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = r.stdout.replace(pw, "***")
    if r.returncode != 0:
        sys.exit(f"[render] staticrypt failed:\n{out}")

def verify(enc_path, data):
    html = open(enc_path, encoding="utf-8").read()
    probes = set()
    for s in data["sites"]:
        if len(s["name"]) >= 8: probes.add(s["name"])
        for c in s.get("contacts", []):
            if c.get("email"): probes.add(c["email"])
    for i in (data.get("issues") or {}).values():
        if len(i.get("title") or "") >= 12: probes.add(i["title"])
        if i.get("summary") and len(i["summary"]) >= 30: probes.add(i["summary"][:60])
    for r in (data.get("storage") or {}).get("rows", []):
        if len(r.get("name") or "") >= 8: probes.add(r["name"])
        if len(r.get("address") or "") >= 8: probes.add(r["address"])
        for c in r.get("contacts", []):
            if c.get("email"): probes.add(c["email"])
    for s in data["sites"]:
        o = s.get("outreach")
        if o and len(o["next_step"]["text"]) >= 30: probes.add(o["next_step"]["text"][:60])
    for it in (data.get("tracker") or {}).get("items", []):
        if len(it.get("summary") or "") >= 30: probes.add(it["summary"][:60])
    if data.get("tracker"): probes |= {"moved_to_stance", "included_because"}
    if data.get("storage"): probes |= {"sf_per_capita", "zoning_rules", "own_facility"}
    probes |= {"function genDraft", "edks_prefs_", "Kansas-based investor"}
    probes |= {"__DATA__", "score_parts", "lines_by_class", "community_rules", "annexation_counts", "infra_score"}
    pc = data.get("pipeline") or {}
    secret_hit = bool(pc.get("token")) and pc["token"] in html
    if pc: probes |= {"edks_pipe_v1", "followup_status", "sheet_url"}
    for u in pc.get("users") or []:
        if u.get("email"): probes.add(u["email"])
    if secret_hit:
        sys.exit("[render] FAIL: pipeline token visible in encrypted output")
    leaks = [p for p in probes if p in html]
    if leaks:
        sys.exit(f"[render] PLAINTEXT LEAK: {len(leaks)} strings found in {enc_path}, e.g. {leaks[:3]}")
    if "staticrypt" not in html.lower():
        sys.exit("[render] output does not look like a StaticCrypt page")
    print(f"[render] verified {enc_path}: 0 of {len(probes)} plaintext probes present")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="plaintext sites.json")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "..", "sites"), help="where encrypted index.html goes")
    ap.add_argument("--password-env", default="EDIFICE_SITES_PASSWORD")
    ap.add_argument("--plain-out", default=None, help="also keep plaintext HTML here (private/gitignored only)")
    ap.add_argument("--no-encrypt", action="store_true")
    ap.add_argument("--storage", default=None, help="plaintext storage.json (merged into the page as D.storage)")
    ap.add_argument("--email-engine", default=None, help="plaintext email engine JS (private/ only); default: decrypt pipeline/email_engine.enc")
    ap.add_argument("--mode", choices=["staticrypt", "firebase"], default="staticrypt",
                    help="firebase: write the Google sign-in shell (data lives in Firestore, see edks.py push)")
    ap.add_argument("--firebase-config-env", default="FIREBASE_WEB_CONFIG")
    ap.add_argument("--firebase-emulator", action="store_true", help="shell talks to the local emulators (testing only)")
    a = ap.parse_args()
    data = json.load(open(a.data, encoding="utf-8"))
    if a.storage and os.path.exists(a.storage):
        data["storage"] = json.load(open(a.storage, encoding="utf-8"))
        print(f"[render] merged storage view: {len(data['storage'].get('rows', []))} rows")
    elif a.storage:
        print(f"[render] WARNING: --storage {a.storage} not found; storage view will be empty")
    if a.mode == "firebase":
        html = build_firebase_html(firebase_config(a.firebase_config_env, a.firebase_emulator), a.firebase_emulator)
        os.makedirs(a.out_dir, exist_ok=True)
        out = os.path.join(a.out_dir, "index.html")
        open(out, "w", encoding="utf-8").write(html)
        verify_public(out, data, load_email_engine(a.email_engine, a.password_env))
        return
    data["pipeline"] = load_pipeline_config(a.password_env)
    html = build_html(data, load_email_engine(a.email_engine, a.password_env))
    tmp = tempfile.mkdtemp(prefix="edks_")
    try:
        plain = os.path.join(tmp, "index.html")
        open(plain, "w", encoding="utf-8").write(html)
        if a.plain_out:
            os.makedirs(os.path.dirname(os.path.abspath(a.plain_out)), exist_ok=True)
            shutil.copy(plain, a.plain_out)
        if not a.no_encrypt:
            os.makedirs(a.out_dir, exist_ok=True)
            encrypt(plain, a.out_dir, a.password_env)
            verify(os.path.join(a.out_dir, "index.html"), data)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
