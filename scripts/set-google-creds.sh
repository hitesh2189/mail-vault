#!/usr/bin/env bash
# Safely write Google OAuth client credentials into .env without echoing the secret.
# Run this in YOUR terminal:  bash scripts/set-google-creds.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=".env"
[ -f "$ENV" ] || { echo "No .env found"; exit 1; }
read -rp "GOOGLE_CLIENT_ID: " CID
read -rsp "GOOGLE_CLIENT_SECRET (hidden): " CSEC; echo
# Update in place using python to avoid sed quoting issues; values never printed.
CID="$CID" CSEC="$CSEC" python3 - <<'PY'
import os, re
cid, csec = os.environ["CID"], os.environ["CSEC"]
p = ".env"
s = open(p).read()
s = re.sub(r'^GOOGLE_CLIENT_ID=.*$', 'GOOGLE_CLIENT_ID='+cid, s, flags=re.M)
s = re.sub(r'^GOOGLE_CLIENT_SECRET=.*$', 'GOOGLE_CLIENT_SECRET='+csec, s, flags=re.M)
open(p, "w").write(s)
os.chmod(p, 0o600)
print("Updated .env (client id len %d, secret len %d)" % (len(cid), len(csec)))
PY
