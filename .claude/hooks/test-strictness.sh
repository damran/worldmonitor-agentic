#!/usr/bin/env bash
# SubagentStop: block if the working tree WEAKENS tests vs origin/master. Exit 2 = block.
# No jq; fails safe (no baseline -> allow). Heuristic: net-removed asserts/tests, or a newly added
# skip/xfail, => the agent is loosening the oracle to pass. That is the one move we never allow.
#
# EXCEPTION — inverted / DELETION gates. A deletion gate (e.g. Gate 0 single-tenancy teardown)
# legitimately removes exactly the tests that prove the property being deleted. There the
# net-removal heuristic is a false positive, so when .claude/gate.scope marks the gate inverted we
# skip the net-removal check — the judge enforces "ONLY the declared (spec §6) tests were removed"
# via `git diff origin/master...HEAD -- tests/` instead. We STILL block any added skip/xfail: that
# is never an acceptable way to pass, deletion gate or not.
set -uo pipefail

base="$(git rev-parse --verify -q origin/master || git rev-parse --verify -q master || true)"
[ -z "$base" ] && exit 0   # fresh repo / no baseline -> nothing to compare against

d="$(git diff "$base" -- '*test_*.py' '*_test.py' 'tests/' 2>/dev/null || true)"
[ -z "$d" ] && exit 0

# An added skip/xfail is forbidden in ALL gates (checked before the inverted-gate early-out).
add_skip=$(printf '%s\n'  "$d" | grep -cE '^\+[[:space:]]*(@?(pytest\.mark\.(skip|xfail)|unittest\.skip)|pytest\.skip\()' || true)
if [ "$add_skip" -gt 0 ]; then
  echo "BLOCKED: added skip/xfail ($add_skip) — never weaken the oracle, even in a deletion gate." >&2
  exit 2
fi

# Inverted/deletion gate? gate.scope is the per-gate contract (it may be gitignored; read from disk).
scope="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || echo .)}/.claude/gate.scope"
if [ -f "$scope" ] && grep -qiE 'inverted_gate:[[:space:]]*true|INVERTED \(DELETION\) gate' "$scope"; then
  exit 0   # deletion gate: net-removed tests/asserts are expected; the judge verifies the §6 list.
fi

rm_assert=$(printf '%s\n' "$d" | grep -cE '^-[[:space:]]*assert([[:space:](]|$)' || true)
add_assert=$(printf '%s\n' "$d" | grep -cE '^\+[[:space:]]*assert([[:space:](]|$)' || true)
rm_test=$(printf '%s\n'  "$d" | grep -cE '^-[[:space:]]*(async[[:space:]]+)?def[[:space:]]+test_' || true)
add_test=$(printf '%s\n' "$d" | grep -cE '^\+[[:space:]]*(async[[:space:]]+)?def[[:space:]]+test_' || true)

if [ "$rm_assert" -gt "$add_assert" ] || [ "$rm_test" -gt "$add_test" ]; then
  echo "BLOCKED: tests weakened vs $base — removed asserts=$rm_assert (added=$add_assert), removed tests=$rm_test (added=$add_test). Strengthen tests; never weaken them to pass." >&2
  exit 2
fi
exit 0
