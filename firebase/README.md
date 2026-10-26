# Firebase app

The deal pipeline runs on Firebase: Google sign-in (Firebase Auth), Firestore for data, and the
existing GitHub Pages site at edifice.vc/sites as the host. The public page is only a sign-in shell;
site data and the draft engine are read from Firestore after an allowed account signs in.

## Data
- `sites/{id}`, `meta/{land,storage}`, `private/email_engine` - written daily by CI (`pipeline/edks.py push`, service account). Members read; no client writes.
- `deals/{siteId}` - current stage. `deals/{siteId}/events/{id}` - stage changes with reasons, notes, follow-ups.
- `users/{email}` - role: admin, editor or viewer. Lowercase email is the document id.
- `learnings/summary` - written by `pipeline/learnings.py --push`.

## Rules tests
    cd firebase && npm install && npm run test:rules

## Local end-to-end
    cd firebase && npx firebase emulators:start --only auth,firestore --project demo-edifice
    export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
    python3 pipeline/edks.py users add you@example.com --role admin
    python3 pipeline/edks.py push --data private/data/sites.json --storage private/data/storage.json --email-engine private/email_engine.js
    python3 pipeline/render.py --data private/data/sites.json --storage private/data/storage.json --mode firebase --firebase-emulator --email-engine private/email_engine.js --out-dir /tmp/fb
    python3 -m http.server 5050 -d /tmp/fb   # open http://127.0.0.1:5050

## Configuration
- Repo variable `FIREBASE_WEB_CONFIG`: the web app config JSON (public project metadata).
- Repo secret `FIREBASE_SERVICE_ACCOUNT`: service-account key JSON (CI push only).
- Repo variable `SITES_MODE=firebase`: serve the sign-in app at /sites instead of the StaticCrypt page.
  Until then the app is staged at /sites/app/.
- Deploy rules: `firebase deploy --only firestore:rules --config firebase/firebase.json --project <id>`.

## Bot CLI (`pipeline/edks.py`)
Key from `EDIFICE_FIREBASE_SA` (path or JSON), `GOOGLE_APPLICATION_CREDENTIALS`, or
`~/.config/edifice/firebase-sa.json`. The key is never printed.

    edks.py followups [--assignee NAME] [--all] [--json]
    edks.py done EVENT_ID | reopen EVENT_ID
    edks.py note SITE_ID "text"
    edks.py followup SITE_ID "text" --assignee "Coding Agent" --due 2026-11-02
    edks.py dump --out private/events.json        # then learnings.py --events private/events.json
    edks.py users list | add EMAIL --role admin | remove EMAIL
