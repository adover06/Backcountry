#!/usr/bin/env bash
# Install the Firebase service account and restart the backend.
#
#   ./scripts/install-service-account.sh                 # auto-find in ~/Downloads
#   ./scripts/install-service-account.sh /path/to/key.json
#
# Validates the file before installing, so saving the *web* config here by mistake
# (a common mix-up — it comes from a different tab of the same settings page) is
# caught with a clear message instead of another opaque 401.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO/data/firebase-service-account.json"

SRC="${1:-}"
if [[ -z "$SRC" ]]; then
  echo "Looking for a service account in ~/Downloads…"
  SRC="$(
    find "$HOME/Downloads" -maxdepth 1 -name '*.json' -print0 2>/dev/null \
    | xargs -0 -I{} sh -c '
        python3 -c "
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    if d.get(\"type\")==\"service_account\": print(sys.argv[1])
except Exception: pass
" "{}"' 2>/dev/null | head -1
  )"
fi

if [[ -z "$SRC" || ! -f "$SRC" ]]; then
  cat <<'EOF'
No service account JSON found.

Get one:
  1. https://console.firebase.google.com  ->  project backcountry-8220a
  2. gear icon  ->  Project settings
  3. "Service accounts" tab   (NOT "General" — that tab has the web config,
                               which is a different file and already installed)
  4. "Generate new private key"  ->  Generate key
  5. Re-run this script, or pass the path:
       ./scripts/install-service-account.sh ~/Downloads/whatever.json
EOF
  exit 1
fi

# Validate before touching anything.
python3 - "$SRC" <<'PY'
import json, sys
path = sys.argv[1]
try:
    d = json.load(open(path))
except Exception as exc:
    sys.exit(f"Not valid JSON: {exc}")

if d.get("type") != "service_account":
    sys.exit(
        "This is not a service account file.\n"
        f"  Its 'type' is {d.get('type')!r}.\n"
        "  If it contains apiKey/authDomain/appId it is the WEB config — that one is\n"
        "  public, already installed, and cannot verify tokens. You need the file from\n"
        "  the 'Service accounts' tab."
    )
for field in ("project_id", "private_key", "client_email"):
    if not d.get(field):
        sys.exit(f"Service account is missing '{field}'.")

print(f"  valid service account for project: {d['project_id']}")
print(f"  client_email: {d['client_email']}")
PY

# Docker creates a *directory* here when a bind mount points at a missing path.
if [[ -d "$DEST" ]]; then
  rmdir "$DEST" 2>/dev/null || { echo "ERROR: $DEST is a non-empty directory"; exit 1; }
fi

mkdir -p "$(dirname "$DEST")"
cp "$SRC" "$DEST"
chmod 600 "$DEST"
echo "  installed -> data/firebase-service-account.json (gitignored, mode 600)"

echo
echo "Restarting backend…"
cd "$REPO"
docker compose -f docker-compose.local.yml restart backend >/dev/null 2>&1 || {
  echo "  (compose restart failed — start the stack with 'docker compose -f docker-compose.local.yml up -d')"
  exit 1
}

sleep 6
if docker compose -f docker-compose.local.yml logs backend 2>&1 | tail -40 | grep -q "Firebase Admin initialized (project=backcountry"; then
  echo "  Firebase Admin initialized ✓  — sign in at http://localhost:8088/login"
else
  echo "  Backend restarted, but Firebase did not report a project. Check:"
  echo "    docker compose -f docker-compose.local.yml logs backend | grep -i firebase"
fi
