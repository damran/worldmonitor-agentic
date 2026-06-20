#!/bin/bash
# Provision the WorldMonitor OIDC applications in Zitadel.
#
# Prereqs: the core stack is `up` and Zitadel is healthy. The compose file makes
# Zitadel write a machine-user PAT to its `zitadel-machinekey` volume; this
# script reads it and uses the Management API to create:
#   - a "WorldMonitor" project
#   - an OIDC app for the API  -> prints ZITADEL_CLIENT_ID
#   - an OIDC app for Hermes    (service principal)
#
# Run from the repo root:  ./scripts/dev/zitadel_provision.sh
set -euo pipefail

COMPOSE="docker compose -f deploy/compose.yaml"
BASE="${ZITADEL_BASE_URL:-http://localhost:8080}"

echo "Waiting for Zitadel to be healthy..."
for _ in $(seq 1 60); do
	if curl -sf "${BASE}/debug/healthz" >/dev/null 2>&1; then break; fi
	sleep 2
done

PAT="$(${COMPOSE} exec -T zitadel cat /machinekey/pat.txt)"
if [ -z "${PAT}" ]; then
	echo "Could not read provisioner PAT from the zitadel-machinekey volume." >&2
	exit 1
fi
AUTH=(-H "Authorization: Bearer ${PAT}" -H "Content-Type: application/json")

echo "Creating project 'WorldMonitor'..."
PROJECT_ID="$(curl -sf "${AUTH[@]}" -X POST "${BASE}/management/v1/projects" \
	-d '{"name":"WorldMonitor"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"

create_oidc_app() {
	local name="$1"
	curl -sf "${AUTH[@]}" -X POST \
		"${BASE}/management/v1/projects/${PROJECT_ID}/apps/oidc" \
		-d "{
			\"name\": \"${name}\",
			\"redirectUris\": [\"http://localhost:8000/auth/callback\"],
			\"responseTypes\": [\"OIDC_RESPONSE_TYPE_CODE\"],
			\"grantTypes\": [\"OIDC_GRANT_TYPE_AUTHORIZATION_CODE\"],
			\"appType\": \"OIDC_APP_TYPE_WEB\",
			\"authMethodType\": \"OIDC_AUTH_METHOD_TYPE_BASIC\",
			\"accessTokenType\": \"OIDC_TOKEN_TYPE_JWT\"
		}"
}

echo "Creating OIDC app 'worldmonitor-api'..."
API_CLIENT_ID="$(create_oidc_app "worldmonitor-api" \
	| python3 -c 'import sys,json;print(json.load(sys.stdin)["clientId"])')"

echo "Creating OIDC app 'hermes'..."
create_oidc_app "hermes" >/dev/null

echo
echo "Done. Put this in your .env:"
echo "  ZITADEL_DOMAIN=${BASE#http://}"
echo "  ZITADEL_CLIENT_ID=${API_CLIENT_ID}"
