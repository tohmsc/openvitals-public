#!/usr/bin/env bash
# Deploy the Vitals cloud dashboard to your Cloudflare account.
#   ./deploy.sh                 # name "vitals", reads ../.env (or $WHOOP_HOME/.env)
#   WORKER_NAME=my-vitals ./deploy.sh
# Needs: wrangler logged in (npx wrangler login) or CLOUDFLARE_API_TOKEN with Workers Scripts:Edit + D1:Edit,
#        and CF_D1_DATABASE_ID in .env (the D1 database cloud_sync.py pushes to).
set -euo pipefail
cd "$(dirname "$0")"
ENV_DIR="${WHOOP_HOME:-..}"
NAME="${WORKER_NAME:-vitals}"
DBID="$(grep '^CF_D1_DATABASE_ID=' "$ENV_DIR/.env" | cut -d= -f2-)"
DBNAME="${D1_NAME:-whoop-backup}"
[ -n "$DBID" ] || { echo "CF_D1_DATABASE_ID missing in $ENV_DIR/.env"; exit 1; }

# Assets: the same static front end the local dashboard serves.
mkdir -p dist/static
cp ../static/*.* dist/static/
cp ../static/index.html dist/index.html
sed -e "s/__NAME__/$NAME/" -e "s/__DBID__/$DBID/" -e "s/__DBNAME__/$DBNAME/" wrangler.template.jsonc > wrangler.jsonc

WR="npx -y wrangler@4"
# First deploy creates the Worker; secrets are added after it exists.
$WR deploy

SECRETS="$($WR secret list 2>/dev/null || echo '[]')"
if ! grep -q SESSION_SECRET <<<"$SECRETS"; then
  openssl rand -hex 32 | $WR secret put SESSION_SECRET >/dev/null
  echo "set SESSION_SECRET"
fi
if ! grep -q DASH_PASSWORD <<<"$SECRETS"; then
  PW_FILE="$ENV_DIR/.dash_password"
  if [ ! -s "$PW_FILE" ]; then
    (umask 077; openssl rand -base64 18 | tr -d '/+=' | cut -c1-20 > "$PW_FILE")
  fi
  $WR secret put DASH_PASSWORD < "$PW_FILE" >/dev/null
  echo "set DASH_PASSWORD (saved in $PW_FILE; open it to read the password)"
fi
echo "done. Open the workers.dev URL printed above and sign in."
