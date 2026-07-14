#!/usr/bin/env bash
# Deploy the metal-notifier stack to Portainer from this Git repo, via the API.
# Automates "Stacks -> Add stack -> Repository". It does NOT do the one-time
# Spotify login — after this runs, open the container Console and run
#   python main.py auth
# (see README -> "Deploy on Portainer").
#
# Requires: bash, curl, jq.  Nothing here is stored in the repo — all secrets
# come from environment variables.
#
# Usage:
#   export PORTAINER_URL=https://portainer.mylan          # no trailing slash
#   export PORTAINER_API_KEY=ptr_xxx                       # UI: My account -> Access tokens
#   export GITHUB_PAT=github_pat_xxx                        # read access to the private repo
#   export SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... LASTFM_API_KEY=...
#   export NTFY_TOPIC=metal-releases-fFshNydJCobor1tBHBPYrd5N
#   export ENDPOINT_ID=1        # your Docker env id (run once without it to list them)
#   # export INSECURE=1         # if Portainer uses a self-signed HTTPS cert
#   ./deploy-portainer.sh
set -euo pipefail

: "${PORTAINER_URL:?e.g. https://portainer.mylan (no trailing slash)}"
: "${PORTAINER_API_KEY:?Portainer access token (UI -> My account -> Access tokens)}"
: "${GITHUB_PAT:?GitHub token with read access to the private repo}"
: "${SPOTIFY_CLIENT_ID:?}"
: "${SPOTIFY_CLIENT_SECRET:?}"
: "${LASTFM_API_KEY:?}"
: "${NTFY_TOPIC:?}"

STACK_NAME="${STACK_NAME:-metal-notifier}"
GITHUB_USER="${GITHUB_USER:-phoffmann12}"
REPO_URL="https://github.com/phoffmann12/release-discovery"
REPO_REF="refs/heads/main"
COMPOSE_PATH="portainer-stack.yml"

INSECURE_FLAG=""
[ "${INSECURE:-0}" = "1" ] && INSECURE_FLAG="-k"
api() { curl -fsSL $INSECURE_FLAG -H "X-API-Key: ${PORTAINER_API_KEY}" "$@"; }

command -v jq >/dev/null || { echo "jq is required (apt install jq)"; exit 1; }

echo "== Portainer status =="
api "${PORTAINER_URL}/api/status" | jq -r '"version: \(.Version)"'

echo "== Docker environments (use the Id of your Docker host) =="
api "${PORTAINER_URL}/api/endpoints" \
  | jq -r '.[] | "  Id=\(.Id)  Name=\(.Name)  Type=\(.Type)  (1=local docker, 2=agent)"'

: "${ENDPOINT_ID:?Set ENDPOINT_ID to an Id above (usually 1) and re-run}"

echo "== Creating stack '${STACK_NAME}' on endpoint ${ENDPOINT_ID} =="
payload="$(jq -n \
  --arg name "$STACK_NAME" --arg url "$REPO_URL" --arg ref "$REPO_REF" \
  --arg cf "$COMPOSE_PATH" --arg gu "$GITHUB_USER" --arg gp "$GITHUB_PAT" \
  --arg a "$SPOTIFY_CLIENT_ID" --arg b "$SPOTIFY_CLIENT_SECRET" \
  --arg c "$LASTFM_API_KEY" --arg d "$NTFY_TOPIC" \
  '{
     name: $name,
     repositoryURL: $url,
     repositoryReferenceName: $ref,
     composeFile: $cf,
     repositoryAuthentication: true,
     repositoryUsername: $gu,
     repositoryPassword: $gp,
     env: [
       {name:"SPOTIFY_CLIENT_ID",     value:$a},
       {name:"SPOTIFY_CLIENT_SECRET", value:$b},
       {name:"LASTFM_API_KEY",        value:$c},
       {name:"NTFY_TOPIC",            value:$d}
     ]
   }')"

# Modern Portainer (>= 2.19). If this 404s on an older version, use instead:
#   ${PORTAINER_URL}/api/stacks?type=2&method=repository&endpointId=${ENDPOINT_ID}
api -X POST -H "Content-Type: application/json" -d "$payload" \
  "${PORTAINER_URL}/api/stacks/create/standalone/repository?endpointId=${ENDPOINT_ID}" \
  | jq -r '"created stack Id=\(.Id) name=\(.Name)"'

echo
echo "Deployed. One-time interactive step still required:"
echo "  Portainer -> Containers -> ${STACK_NAME}-notifier-1 -> Console (/bin/sh) -> python main.py auth"
echo "then Recreate the container so it picks up the Spotify token."
