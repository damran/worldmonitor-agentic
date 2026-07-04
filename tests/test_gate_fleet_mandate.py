"""Gate 0 slice-0d — gate-fleet enforcement mandate guard.

ADR 0097 §5 adds a **standing duty** to the fleet checker and judge: for every
gate whose diff carries an ADR, reproduce the ADR's ``human_fork`` /
``person_affecting`` self-classification against the ACTUAL diff and FAIL/DENY
if the classification is dishonest or an un-cosigned waiver is present.

Slice 0d's builder transcribes the operative mandate text into:
- ``.claude/agents/checker.md`` — a new paragraph after the
  "confirm NO test was weakened" block.
- ``.claude/agents/judge.md`` — a new ``INVESTIGATE`` bullet plus a ``DENY``
  condition in ``RULE``.

This file is the pre-builder tripwire (written by the test-author agent).

Expected state on the CURRENT (pre-builder) tree
-------------------------------------------------
* ``test_checker_carries_person_affecting_mandate`` — **FAIL** (checker.md does
  not yet contain ``person_affecting``, ``human_fork``, or any cosign token;
  the mandate paragraph has not been added).
* ``test_judge_carries_person_affecting_deny`` — **FAIL** (judge.md does not
  yet contain ``person_affecting`` or any cosign token; the mandate
  INVESTIGATE bullet and matching DENY condition have not been added).
* ``test_adr0097_mandate_section_filled`` — **PASS** (the planner already
  wrote §5 before the builder ran; the stub sentinel is absent).

These tests must all be GREEN on the post-builder tree.  They encode the REAL
invariant — specific tokens that must be present — not just "no exception".
"""

from __future__ import annotations

from pathlib import Path

# Resolve repo root from THIS file's location: tests/ -> parents[1] = repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]

# All three target files are verified git-tracked and not gitignored (checked by
# the test-author before writing these assertions).
CHECKER_MD = ".claude/agents/checker.md"
JUDGE_MD = ".claude/agents/judge.md"
ADR_0097 = "docs/decisions/0097-adr-governance-index-automation.md"


def _read(rel: str) -> str:
    """Return the full text of a repo-relative file; fail loudly on bad path."""
    path = REPO_ROOT / rel
    assert path.is_file(), (
        f"Expected file {path} to exist. Verify the path is correct and the file is git-tracked."
    )
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1 — checker.md must carry the person-affecting enforcement mandate
# ---------------------------------------------------------------------------


def test_checker_carries_person_affecting_mandate() -> None:
    """checker.md must contain all four tokens required by ADR 0097 §5.

    The 0d builder adds a standing-duty paragraph that makes the checker
    reproduce the ADR's human_fork / person_affecting self-classification
    against the ACTUAL diff and FAIL the gate when:
      (a) the diff touches a person-affecting surface but the ADR self-tags
          person_affecting: false; OR
      (b) the ADR waives a human_fork or person_affecting tag without a
          human_cosign line.

    FAILS on the pre-builder tree: none of person_affecting, human_fork, or
    any cosign token are present in checker.md yet.
    """
    text = _read(CHECKER_MD)
    lower = text.lower()

    assert "person_affecting" in lower, (
        f"{CHECKER_MD} is MISSING 'person_affecting'. "
        "ADR 0097 §5 requires the checker to reproduce the ADR's "
        "person_affecting self-classification against the actual diff and "
        "FAIL the gate when the classification is dishonest. "
        "Builder: add the standing-duty paragraph to .claude/agents/checker.md."
    )

    assert "human_fork" in lower, (
        f"{CHECKER_MD} is MISSING 'human_fork'. "
        "ADR 0097 §5 requires the checker to verify the human_fork "
        "classification as part of the same check. "
        "Builder: add the standing-duty paragraph to .claude/agents/checker.md."
    )

    cosign_present = any(token in lower for token in ("human_cosign", "co-sign", "cosign"))
    assert cosign_present, (
        f"{CHECKER_MD} is MISSING a cosign token "
        "(any of: human_cosign / co-sign / cosign). "
        "ADR 0097 §4-§5 require the checker to verify that an un-cosigned "
        "person_affecting waiver FAILS the gate. "
        "Builder: add the standing-duty paragraph to .claude/agents/checker.md."
    )

    assert "fail" in lower, (
        f"{CHECKER_MD} is MISSING a failure token ('FAIL'). "
        "The mandate specifies that the checker FAILs the gate on a dishonest "
        "or un-cosigned ADR classification (ADR 0097 §5). "
        "Builder: ensure the standing-duty paragraph names the FAIL outcome."
    )


# ---------------------------------------------------------------------------
# Test 2 — judge.md must carry the person-affecting DENY condition
# ---------------------------------------------------------------------------


def test_judge_carries_person_affecting_deny() -> None:
    """judge.md must contain person_affecting, DENY, and a cosign token.

    ADR 0097 §5 adds to judge.md:
      - a new INVESTIGATE bullet: verify the ADR's human_fork /
        person_affecting classification against the actual diff.
      - a matching DENY condition in RULE: a person-affecting-but-untagged
        ADR, or an un-cosigned waiver, is a merge blocker (DENY), not a
        style nit and not a backlog item.

    FAILS on the pre-builder tree: person_affecting and cosign tokens are
    absent from judge.md (DENY is present for pre-existing reasons, but the
    mandate-specific person_affecting check and cosign verification are not).
    """
    text = _read(JUDGE_MD)
    lower = text.lower()

    assert "person_affecting" in lower, (
        f"{JUDGE_MD} is MISSING 'person_affecting'. "
        "ADR 0097 §5 requires the judge to carry an INVESTIGATE bullet that "
        "verifies the ADR's person_affecting classification. "
        "Builder: add the mandate bullet to .claude/agents/judge.md."
    )

    assert "deny" in lower, (
        f"{JUDGE_MD} is MISSING 'deny'. "
        "ADR 0097 §5 requires the judge to DENY a merge when the ADR's "
        "person_affecting tag is dishonest or an un-cosigned waiver is present. "
        "Builder: add the DENY condition to .claude/agents/judge.md."
    )

    cosign_present = any(token in lower for token in ("human_cosign", "co-sign", "cosign"))
    assert cosign_present, (
        f"{JUDGE_MD} is MISSING a cosign token "
        "(any of: human_cosign / co-sign / cosign). "
        "ADR 0097 §4-§5 require the judge to verify the human_cosign line is "
        "present for un-cosigned waivers. "
        "Builder: add the mandate bullet to .claude/agents/judge.md."
    )


# ---------------------------------------------------------------------------
# Test 3 — ADR 0097 §5 must be filled (planner-done; stub must be absent)
# ---------------------------------------------------------------------------


def test_adr0097_mandate_section_filled() -> None:
    """ADR 0097 must contain the §5 heading and must not contain a stub sentinel.

    The planner wrote §5 'Gate-fleet enforcement mandate' before the builder
    ran. This test guards that the substantive text stays in place and neither
    form of the stub placeholder is present.

    PASSES on the pre-builder tree (planner already filled §5).
    """
    text = _read(ADR_0097)

    assert "Gate-fleet enforcement mandate" in text, (
        f"{ADR_0097} is MISSING the §5 heading 'Gate-fleet enforcement mandate'. "
        "The planner was supposed to fill this section before the builder ran. "
        "Do not replace the substantive text with a stub or remove the heading."
    )

    # Em-dash form: "STUB — to be filled"
    assert "STUB — to be filled" not in text, (
        f"{ADR_0097} §5 still contains the em-dash stub sentinel "
        "'STUB — to be filled'. "
        "The section must contain the substantive mandate text, not a placeholder."
    )

    # Hyphen fallback: "STUB - to be filled"
    assert "STUB - to be filled" not in text, (
        f"{ADR_0097} §5 still contains the hyphen stub sentinel "
        "'STUB - to be filled'. "
        "The section must contain the substantive mandate text, not a placeholder."
    )
