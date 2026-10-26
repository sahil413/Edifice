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

def build_html(data, engine=EMAIL_STUB):
    data = dict(data, cert_list_url=CERT_LIST_URL)
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    tpl = tpl.replace("/*__EMAIL__*/", engine.replace("</script", "<\\/script"))
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")
    return tpl.replace("/*__DATA__*/", js)

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
    a = ap.parse_args()
    data = json.load(open(a.data, encoding="utf-8"))
    if a.storage and os.path.exists(a.storage):
        data["storage"] = json.load(open(a.storage, encoding="utf-8"))
        print(f"[render] merged storage view: {len(data['storage'].get('rows', []))} rows")
    elif a.storage:
        print(f"[render] WARNING: --storage {a.storage} not found; storage view will be empty")
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
