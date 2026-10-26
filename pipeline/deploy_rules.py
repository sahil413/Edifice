#!/usr/bin/env python3
"""Deploy firebase/firestore.rules exactly as written, keyless, via the Firebase Rules REST API.

Uses the gcloud CLI login (`gcloud auth print-access-token`); the token is kept in memory and never printed.
  python3 pipeline/deploy_rules.py [--project dms-edvc] [--rules firebase/firestore.rules]
"""
import argparse, json, os, shutil, subprocess, sys, urllib.error, urllib.request

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=os.environ.get("FIREBASE_PROJECT", "dms-edvc"))
    ap.add_argument("--rules", default=os.path.join(os.path.dirname(__file__), "..", "firebase", "firestore.rules"))
    a = ap.parse_args()
    gb = shutil.which("gcloud") or os.path.expanduser("~/google-cloud-sdk/bin/gcloud")
    r = subprocess.run([gb, "auth", "print-access-token"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit("[rules] gcloud could not issue a token (run `gcloud auth login`)")
    hdr = {"Authorization": "Bearer " + r.stdout.strip(), "Content-Type": "application/json",
           "x-goog-user-project": a.project}
    base = "https://firebaserules.googleapis.com/v1/projects/" + a.project

    def call(method, url, body=None):
        req = urllib.request.Request(url, method=method, headers=hdr,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            return json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {url} -> {e.code} {e.read()[:300]!r}")

    src = open(a.rules, encoding="utf-8").read()
    rs = call("POST", base + "/rulesets", {"source": {"files": [{"name": "firestore.rules", "content": src}]}})
    name = f"projects/{a.project}/releases/cloud.firestore"
    try:
        rel = call("PATCH", base + "/releases/cloud.firestore", {"release": {"name": name, "rulesetName": rs["name"]}})
    except RuntimeError:
        rel = call("POST", base + "/releases", {"name": name, "rulesetName": rs["name"]})
    got = call("GET", "https://firebaserules.googleapis.com/v1/" + rs["name"])
    ok = got["source"]["files"][0]["content"] == src
    print(f"[rules] released {rel['rulesetName']} (exact source match: {ok})")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
