"""Tests for scripts/gen_adr_index.py — ADR index generator (Gate 0a).

Tests 1–3 exercise the generator against temp fixtures (--decisions-dir / --readme);
they will pass once the generator script is written.  Test 4 is the ACCEPTANCE GUARD:
it runs against the real repo and goes green only once the builder has:

  1. Written scripts/gen_adr_index.py.
  2. Flipped the 22 stale-PROPOSED ADR files to ACCEPTED (or appropriate non-PROPOSED status).
  3. Created docs/decisions/0097-*.md (the ADR for this gate).
  4. Regenerated docs/decisions/README.md in place so --check exits 0.

These tests are TEST-FIRST — they fail on the current tree for the right reason (script absent /
README not yet regenerated), and they cannot be satisfied by weakening the generator behaviour.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gen_adr_index.py"
REAL_README = REPO_ROOT / "docs" / "decisions" / "README.md"

BEGIN_SENTINEL = "<!-- BEGIN GENERATED ADR INDEX (scripts/gen_adr_index.py) -->"
END_SENTINEL = "<!-- END GENERATED ADR INDEX -->"


def _run(*extra_args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the generator script via the current interpreter."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *extra_args],
        capture_output=True,
        text=True,
    )


def _run_fixture(
    decisions_dir: Path, readme: Path, *extra_args: str
) -> subprocess.CompletedProcess[str]:
    """Invoke the generator against a specific fixture directory and README."""
    return _run(
        "--decisions-dir",
        str(decisions_dir),
        "--readme",
        str(readme),
        *extra_args,
    )


def _extract_generated_region(readme_text: str) -> str:
    """Return text strictly BETWEEN the two sentinels (exclusive of sentinel lines).

    Raises ValueError (-> test error) if either sentinel is missing — which is itself
    a meaningful assertion failure: the generator must emit both sentinels.
    """
    try:
        start = readme_text.index(BEGIN_SENTINEL) + len(BEGIN_SENTINEL)
    except ValueError as exc:
        raise ValueError(
            f"BEGIN sentinel not found in README.\nExpected: {BEGIN_SENTINEL!r}"
        ) from exc
    try:
        end = readme_text.index(END_SENTINEL, start)
    except ValueError as exc:
        raise ValueError(
            f"END sentinel not found after BEGIN in README.\nExpected: {END_SENTINEL!r}"
        ) from exc
    return readme_text[start:end]


def _write_adr(directory: Path, filename: str, h1_suffix: str, status_block: str) -> None:
    """Write a minimal ADR fixture file with a guaranteed H1 and status block."""
    (directory / filename).write_text(
        f"# {h1_suffix}\n\n{status_block}\n\n## Context\n\nTest fixture.\n"
    )


def _write_readme(path: Path, extra_above: str = "") -> Path:
    """Write a minimal README fixture containing the two sentinels."""
    readme = path / "README.md"
    prefix = f"{extra_above}\n" if extra_above else ""
    readme.write_text(f"{prefix}{BEGIN_SENTINEL}\n{END_SENTINEL}\n")
    return readme


# ---------------------------------------------------------------------------
# Test 1 — Both status dialects are parsed and normalized to uppercase
# ---------------------------------------------------------------------------


def test_both_status_dialects_parse(tmp_path: Path) -> None:
    """
    Blockquote dialect (> Status: **TOKEN** . ...) and list dialect
    (- **Status:** token ...) are both parsed; the leading status word is
    normalized to uppercase in the generated region.

    Fixture:
      0020-blockquote.md  -> blockquote dialect -> expected PROPOSED in output
      0021-list.md        -> list dialect (lowercase 'accepted') -> expected ACCEPTED in output
    """
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()

    # Blockquote dialect — generator must extract 'PROPOSED'
    _write_adr(
        decisions_dir,
        "0020-blockquote.md",
        "ADR 0020 — Blockquote Dialect Example",
        "> Status: **PROPOSED** · 2026-07-04 · Some context.",
    )
    # List dialect with lowercase — generator must normalize to 'ACCEPTED'
    _write_adr(
        decisions_dir,
        "0021-list.md",
        "0021 — List Dialect Example",
        "- **Status:** accepted (2026-07-04)",
    )

    readme = _write_readme(tmp_path)

    result = _run_fixture(decisions_dir, readme)
    assert result.returncode == 0, (
        f"Generator (write mode) exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    region = _extract_generated_region(readme.read_text())

    # Find lines that reference each ADR (by 4-digit number in the link text)
    lines_0020 = [ln for ln in region.splitlines() if "0020" in ln]
    lines_0021 = [ln for ln in region.splitlines() if "0021" in ln]

    assert lines_0020, "ADR 0020 not found in generated region (blockquote dialect ADR is missing)"
    assert lines_0021, "ADR 0021 not found in generated region (list dialect ADR is missing)"

    # Blockquote ADR must show PROPOSED (not ACCEPTED)
    assert any("PROPOSED" in ln for ln in lines_0020), (
        f"ADR 0020 (blockquote dialect) must show 'PROPOSED' in the generated region.\n"
        f"Lines referencing 0020: {lines_0020}"
    )
    assert not any("ACCEPTED" in ln for ln in lines_0020), (
        f"ADR 0020 must NOT show 'ACCEPTED' — its status is PROPOSED.\n"
        f"Lines referencing 0020: {lines_0020}"
    )

    # List ADR must show ACCEPTED (normalized from lowercase 'accepted')
    assert any("ACCEPTED" in ln for ln in lines_0021), (
        f"ADR 0021 (list dialect, lowercase input) must show 'ACCEPTED' in the generated region.\n"
        f"Lines referencing 0021: {lines_0021}"
    )
    assert not any("PROPOSED" in ln for ln in lines_0021), (
        f"ADR 0021 must NOT show 'PROPOSED' — its status is ACCEPTED.\n"
        f"Lines referencing 0021: {lines_0021}"
    )


# ---------------------------------------------------------------------------
# Test 2 — --check is idempotent and detects drift
# ---------------------------------------------------------------------------


def test_check_idempotent_and_detects_drift(tmp_path: Path) -> None:
    """
    After write-mode generation, --check must exit 0 (idempotent: regenerating
    the same inputs yields bit-identical output).  After a manual mutation of the
    region content, --check must exit non-zero.
    """
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    _write_adr(
        decisions_dir,
        "0020-example.md",
        "ADR 0020 — Example",
        "> Status: **LOCKED** · 2026-07-04",
    )
    readme = _write_readme(tmp_path)

    # Write mode must succeed
    result = _run_fixture(decisions_dir, readme)
    assert result.returncode == 0, (
        f"Generator (write mode) exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    # --check immediately after write → idempotent → must exit 0
    check_clean = _run_fixture(decisions_dir, readme, "--check")
    assert check_clean.returncode == 0, (
        f"--check must exit 0 immediately after write-mode generation (idempotent).\n"
        f"returncode={check_clean.returncode}\nstderr:\n{check_clean.stderr}"
    )

    # Corrupt the region: replace the generated content between the sentinels
    text = readme.read_text()
    begin_pos = text.index(BEGIN_SENTINEL) + len(BEGIN_SENTINEL)
    end_pos = text.index(END_SENTINEL, begin_pos)
    corrupted = (
        text[:begin_pos]
        + "\nCORRUPTED ROW — this does not match the freshly generated content\n"
        + text[end_pos:]
    )
    readme.write_text(corrupted)

    # --check on the corrupted README → must exit non-zero
    check_drifted = _run_fixture(decisions_dir, readme, "--check")
    assert check_drifted.returncode != 0, (
        "--check must exit non-zero when the committed README has drifted from "
        "a fresh re-generation.  returncode was unexpectedly 0."
    )


# ---------------------------------------------------------------------------
# Test 3 — Range coverage (≥16 included, <16 excluded) + sentinel preservation
# ---------------------------------------------------------------------------


def test_range_coverage_and_foundational_preservation(tmp_path: Path) -> None:
    """
    ADRs with number < 16 must NOT appear in the generated region.
    ADRs with number >= 16 must appear.
    Text above the BEGIN sentinel (the hand-maintained #1-15 block) must be
    preserved verbatim and positioned BEFORE the sentinel.
    """
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()

    # Below the cut-off — must not appear in the generated region
    _write_adr(
        decisions_dir,
        "0009-old-foundational.md",
        "ADR 0009 — Old Foundational Decision",
        "- **Status:** LOCKED",
    )
    # At the boundary and above — both must appear
    _write_adr(
        decisions_dir,
        "0016-boundary.md",
        "ADR 0016 — Boundary Decision",
        "> Status: **ACCEPTED** · 2026-07-04",
    )
    _write_adr(
        decisions_dir,
        "0025-above-boundary.md",
        "0025 — Above Boundary Decision",
        "- **Status:** proposed",
    )

    preserved_line = "| 1-15 | Hand-maintained foundational decisions — do not remove |"
    readme = _write_readme(tmp_path, extra_above=preserved_line)

    result = _run_fixture(decisions_dir, readme)
    assert result.returncode == 0, (
        f"Generator (write mode) exited {result.returncode}.\nstderr:\n{result.stderr}"
    )

    full_text = readme.read_text()
    region = _extract_generated_region(full_text)

    # (a) The preserved line is still present verbatim in the full README
    assert preserved_line in full_text, (
        "The hand-maintained line above the BEGIN sentinel was lost after generation."
    )

    # (b) The preserved line appears BEFORE the BEGIN sentinel (not inside the region)
    begin_pos = full_text.index(BEGIN_SENTINEL)
    preserved_pos = full_text.index(preserved_line)
    assert preserved_pos < begin_pos, (
        "Hand-maintained line must appear BEFORE the BEGIN sentinel, "
        f"but preserved_pos={preserved_pos} >= begin_pos={begin_pos}."
    )

    # (c) ADR 0009 (number < 16) must NOT appear in the generated region
    assert "0009" not in region, (
        "ADR 0009 (number < 16) must be excluded from the generated region, but it was found there."
    )

    # (d) ADR 0016 and ADR 0025 (numbers >= 16) must appear in the generated region
    assert "0016" in region, (
        "ADR 0016 (at the boundary, number = 16) must appear in the generated region."
    )
    assert "0025" in region, "ADR 0025 (number > 16) must appear in the generated region."


# ---------------------------------------------------------------------------
# Test 4 — ACCEPTANCE GUARD: real-repo end-to-end (RED until builder finishes)
# ---------------------------------------------------------------------------

# These 22 ADRs are merged-and-accepted in the repo but currently carry status PROPOSED
# in their source files.  The builder must flip them.  Any that still show PROPOSED in
# the generated region after the builder's work means the builder did not finish.
_STALE_PROPOSED_ADRS = [
    "0040",
    "0041",
    "0042",
    "0043",
    "0044",
    "0046",
    "0048",
    "0051",
    "0052",
    "0053",
    "0054",
    "0055",
    "0056",
    "0057",
    "0058",
    "0059",
    "0060",
    "0061",
    "0086",
    "0087",
    "0089",
    "0090",
]


# ACCEPTANCE GUARD — this test is intentionally RED until the builder:
#   1. writes scripts/gen_adr_index.py
#   2. flips the 22 stale-PROPOSED ADR files to ACCEPTED
#   3. creates docs/decisions/0097-*.md
#   4. regenerates docs/decisions/README.md in place
def test_real_repo_acceptance_guard() -> None:
    """
    End-to-end acceptance gate against the real repository.

    Assertions (all must hold simultaneously):
      A. '--check' exits 0 -> the committed README is in sync with the ADR files.
      B. ADR 0016 appears in the generated region (proves the >=16 boundary is live).
      C. ADR 0097 appears in the generated region (the gate's own ADR, created by the builder).
      D. None of the 22 merged-and-accepted ADRs show status PROPOSED in the generated region.
    """
    # A. --check must exit 0
    check = _run("--check")
    assert check.returncode == 0, (
        f"scripts/gen_adr_index.py --check exited {check.returncode}.\n"
        "The committed README.md has drifted from a fresh re-generation, "
        "OR the script does not exist yet.\n"
        f"stderr:\n{check.stderr}"
    )

    # B + C + D: inspect the generated region of the real README
    readme_text = REAL_README.read_text()

    assert BEGIN_SENTINEL in readme_text, (
        "docs/decisions/README.md is missing the BEGIN sentinel.\n"
        "Has the generator been run yet?\n"
        f"Expected to find: {BEGIN_SENTINEL!r}"
    )

    region = _extract_generated_region(readme_text)

    # B. ADR 0016 must appear in the generated region (>=16 boundary)
    adr16_lines = [ln for ln in region.splitlines() if "0016" in ln]
    assert adr16_lines, (
        "ADR 0016 must appear in the generated region of README.md "
        "(it is numbered 16, exactly at the >=16 boundary).\n"
        "No line in the generated region contains '0016'."
    )

    # C. ADR 0097 must appear in the generated region (builder creates it for Gate 0a)
    adr97_lines = [ln for ln in region.splitlines() if "0097" in ln]
    assert adr97_lines, (
        "ADR 0097 must appear in the generated region — "
        "the builder must create docs/decisions/0097-*.md for Gate 0a.\n"
        "No line in the generated region contains '0097'."
    )

    # D. None of the 22 merged-and-accepted ADRs may show PROPOSED
    for adr_num in _STALE_PROPOSED_ADRS:
        short = str(int(adr_num))  # "0040" -> "40"
        adr_lines = [
            ln
            for ln in region.splitlines()
            if adr_num in ln or f"| {short} " in ln or f"| {short}|" in ln
        ]
        assert adr_lines, (
            f"ADR {adr_num} not found in the generated region.\n"
            "It is a merged-and-accepted ADR and must be listed."
        )
        for ln in adr_lines:
            assert "PROPOSED" not in ln, (
                f"ADR {adr_num} still shows 'PROPOSED' in the generated region.\n"
                "The builder must flip its source file to ACCEPTED "
                f"(or another non-PROPOSED status).\nOffending line: {ln!r}"
            )
