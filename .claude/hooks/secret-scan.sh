#!/usr/bin/env bash
# SubagentStop: block if the working diff introduces a secret. Exit 2 = block.
set -uo pipefail
diff="$( { git diff HEAD; git diff --staged; } 2>/dev/null )"
[ -z "$diff" ] && exit 0
if printf '%s' "$diff" | grep -nEi \
  -e 'sk-ant-[A-Za-z0-9_-]{20,}' \
  -e 'sk-or-v1-[A-Za-z0-9_-]{20,}' \
  -e 'sk-[A-Za-z0-9]{32,}' \
  -e '(api[_-]?key|secret|token|passwd|password)["'"'"' ]*[:=]["'"'"' ]*[A-Za-z0-9/+._-]{16,}' \
  -e '-----BEGIN (RSA|EC|OPENSSH|PGP) PRIVATE KEY-----' >/dev/null 2>&1; then
  echo "BLOCKED: a potential secret is present in the diff. Secrets live in .env (gitignored), never in tracked files." >&2
  exit 2
fi
exit 0
