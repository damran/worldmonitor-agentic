#!/usr/bin/env bash
# SubagentStop: block if the working tree WEAKENS tests vs origin/master. Exit 2 = block.
# No jq; fails safe (no baseline -> allow). Heuristic: net-removed asserts/tests, or a newly added
# skip/xfail, => the agent is loosening the oracle to pass. That is the one move we never allow.
set -uo pipefail

base="$(git rev-parse --verify -q origin/master || git rev-parse --verify -q master || true)"
[ -z "$base" ] && exit 0   # fresh repo / no baseline -> nothing to compare against

d="$(git diff "$base" -- '*test_*.py' '*_test.py' 'tests/' 2>/dev/null || true)"
[ -z "$d" ] && exit 0

rm_assert=$(printf '%s\n' "$d" | grep -cE '^-[[:space:]]*assert([[:space:](]|$)' || true)
add_assert=$(printf '%s\n' "$d" | grep -cE '^\+[[:space:]]*assert([[:space:](]|$)' || true)
rm_test=$(printf '%s\n'  "$d" | grep -cE '^-[[:space:]]*(async[[:space:]]+)?def[[:space:]]+test_' || true)
add_test=$(printf '%s\n' "$d" | grep -cE '^\+[[:space:]]*(async[[:space:]]+)?def[[:space:]]+test_' || true)
add_skip=$(printf '%s\n'  "$d" | grep -cE '^\+[[:space:]]*(@?(pytest\.mark\.(skip|xfail)|unittest\.skip)|pytest\.skip\()' || true)

if [ "$rm_assert" -gt "$add_assert" ] || [ "$rm_test" -gt "$add_test" ] || [ "$add_skip" -gt 0 ]; then
  echo "BLOCKED: tests weakened vs $base — removed asserts=$rm_assert (added=$add_assert), removed tests=$rm_test (added=$add_test), added skip/xfail=$add_skip. Strengthen tests; never weaken them to pass." >&2
  exit 2
fi
exit 0
