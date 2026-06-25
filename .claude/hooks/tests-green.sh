#!/usr/bin/env bash
# Stop (builder) -> auto-converted to SubagentStop: refuse "done" while tests are red. Exit 2 = block.
set -uo pipefail
input="$(cat || true)"
# avoid the block loop: if we're already in a stop-hook re-entry, let it pass
printf '%s' "$input" | grep -q '"stop_hook_active"[[:space:]]*:[[:space:]]*true' && exit 0
if ! uv run pytest -q --no-header -x >/tmp/wm_pytest.out 2>&1; then
  echo "BLOCKED: tests are red — fix before completing." >&2
  tail -30 /tmp/wm_pytest.out >&2
  exit 2
fi
exit 0
