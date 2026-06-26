#!/usr/bin/env bash
# Gate B-4b (ADR 0050) — thin ops entrypoint for a cross-store backup.
# ALL logic lives in worldmonitor.backup; this wrapper carries none. It builds the three stores
# from process settings (env / .env) and dumps them to the directory passed as "$@", e.g.:
#
#   deploy/backup/backup.sh /var/backups/worldmonitor/$(date -u +%Y%m%dT%H%M%SZ)
#
# Run in a low-activity window: the Neo4j export is an ONLINE logical Cypher read and is not
# point-in-time consistent under concurrent writes (pause the resolve cadence — see the runbook).
set -euo pipefail
exec uv run python -m worldmonitor.backup backup "$@"
