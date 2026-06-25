#!/usr/bin/env bash
# PreToolUse(Bash): refuse git writes to master|main. Exit 2 = block. Constrains the agent only.
# Parse-free on purpose (no jq/python dependency) so it can never fail OPEN: it greps the raw
# tool-call JSON on stdin. A rare false-positive on master is acceptable; a missed block is not.
set -uo pipefail
input="$(cat || true)"
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
if [ "$branch" = "master" ] || [ "$branch" = "main" ]; then
  if printf '%s' "$input" | grep -qE 'git[[:space:]]+(commit|push|merge)'; then
    echo "BLOCKED: refusing a git write on protected branch '$branch'. Cut a gate/* branch first." >&2
    exit 2
  fi
fi
if printf '%s' "$input" | grep -qE 'git[[:space:]]+push[^|]*\b(master|main)\b'; then
  echo "BLOCKED: the fleet does not push to master/main. The human merges (git merge --ff-only)." >&2
  exit 2
fi
exit 0
