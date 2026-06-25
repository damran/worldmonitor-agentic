#!/usr/bin/env bash
# SubagentStop: keep a gate's edits inside its declared scope.
# Scope = path globs in .claude/gate.scope (the planner writes it). No file -> no constraint.
# The fleet's own .claude/ control files are never policed.
set -uo pipefail
scope="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || echo .)}/.claude/gate.scope"
[ -f "$scope" ] || exit 0
changed="$( { git diff --name-only HEAD; git diff --name-only --staged; } 2>/dev/null | sort -u | grep -v '^\.claude/' )"
[ -z "$changed" ] && exit 0
bad=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  ok=0
  while IFS= read -r glob; do
    [ -z "$glob" ] && continue
    # shellcheck disable=SC2254
    case "$f" in $glob) ok=1; break;; esac
  done < "$scope"
  [ "$ok" -eq 0 ] && bad="${bad}
  ${f}"
done <<< "$changed"
if [ -n "$bad" ]; then
  echo "BLOCKED: files outside the gate scope ($scope):${bad}" >&2
  echo "If the gate truly needs them, update .claude/gate.scope and flag it for the judge." >&2
  exit 2
fi
exit 0
