#!/usr/bin/env python3
"""Encrypt/decrypt private pipeline files (curated contacts, change-tracking state) so they can
live in the public repo. AES-256-CBC + PBKDF2 (200k iters) via openssl; the password is read by
openssl straight from an env var (-pass env:VAR), never passed on argv or printed.

  python3 pipeline/crypt.py enc private/contacts.json pipeline/contacts.enc
  python3 pipeline/crypt.py dec pipeline/contacts.enc "$RUNNER_TEMP/contacts.json"
"""
import argparse, os, subprocess, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["enc", "dec"])
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("--password-env", default=os.environ.get("PASSWORD_ENV", "EDIFICE_SITES_PASSWORD"))
    a = ap.parse_args()
    if not os.environ.get(a.password_env):
        sys.exit(f"[crypt] env var {a.password_env} not set")
    cmd = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", f"env:{a.password_env}",
           "-in", a.src, "-out", a.dst]
    if a.mode == "dec": cmd.insert(3, "-d")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"[crypt] openssl {a.mode} failed (wrong password?)")
    print(f"[crypt] {a.mode} {a.src} -> {a.dst}")

if __name__ == "__main__":
    main()
