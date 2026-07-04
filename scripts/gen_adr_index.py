#!/usr/bin/env python3
"""Generate and check the ADR index in docs/decisions/README.md.

Usage:
  python scripts/gen_adr_index.py             # write mode: regenerate the generated region
  python scripts/gen_adr_index.py --check     # check mode: exit 0 if in-sync, 1 if drifted

Optional flags:
  --decisions-dir DIR    directory to scan for ADR files (default: docs/decisions)
  --readme FILE          README file to update (default: docs/decisions/README.md)

Defaults are resolved relative to the repository root (the parent of the scripts/ directory),
not the current working directory.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Sentinels — must match the test constants exactly
# ---------------------------------------------------------------------------

BEGIN_SENTINEL = "<!-- BEGIN GENERATED ADR INDEX (scripts/gen_adr_index.py) -->"
END_SENTINEL = "<!-- END GENERATED ADR INDEX -->"

# ADRs numbered below this are hand-maintained in the README; do not include them.
_ADR_MIN_NUMBER = 16

# ---------------------------------------------------------------------------
# Defaults derived from the repo root (scripts/ is one level below root)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DECISIONS_DIR = _REPO_ROOT / "docs" / "decisions"
_DEFAULT_README = _REPO_ROOT / "docs" / "decisions" / "README.md"

# ---------------------------------------------------------------------------
# Regex patterns for header parsing
# ---------------------------------------------------------------------------

_ADR_FILENAME_RE = re.compile(r"^(\d{4})-.*\.md$")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Blockquote dialect:  > Status: **TOKEN** ...
# Captures everything between the first pair of ** on that line.
_BLOCKQUOTE_STATUS_RE = re.compile(r">\s*Status:\s*\*\*([^*]+)\*\*", re.IGNORECASE)

# List dialect:  - **Status:** TOKEN ...
# Captures everything after the **Status:** marker.
_LIST_STATUS_RE = re.compile(r"-\s*\*\*Status:\*\*\s*(.+)", re.IGNORECASE)

# Number of header lines to parse for metadata (covers all known ADR header styles).
_HEADER_LINES = 15


# ---------------------------------------------------------------------------
# Helper: extract the leading status word
# ---------------------------------------------------------------------------


def _leading_word(text: str) -> str:
    """Return the first whitespace/punctuation-delimited token of *text*, uppercased.

    e.g. "LOCKED (v0)"  -> "LOCKED"
         "proposed"      -> "PROPOSED"
         "accepted"      -> "ACCEPTED"
    """
    m = re.match(r"([^\s\(\)—\.,;:]+)", text.strip())
    return m.group(1).upper() if m else text.strip().upper()


# ---------------------------------------------------------------------------
# Per-field parsers
# ---------------------------------------------------------------------------


def _parse_title(lines: list[str], number: int) -> str:  # noqa: ARG001
    """Return the H1 title, stripping any leading 'ADR NNNN — ' or 'NNNN — ' prefix."""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            # Strip "ADR NNNN — " prefix (with em-dash U+2014)
            title = re.sub(r"^ADR\s+\d+\s*—\s*", "", title)
            # Strip bare "NNNN — " prefix
            title = re.sub(r"^\d+\s*—\s*", "", title)
            return title
    return "—"


def _parse_status(header_lines: list[str]) -> str:
    """Extract and normalize the status token from the header block.

    Supports both dialects:
      blockquote:  > Status: **TOKEN** ...
      list:        - **Status:** token ...
    """
    for line in header_lines:
        m = _BLOCKQUOTE_STATUS_RE.search(line)
        if m:
            return _leading_word(m.group(1))
        m = _LIST_STATUS_RE.search(line)
        if m:
            return _leading_word(m.group(1))
    return "—"


def _parse_date(header_lines: list[str]) -> str:
    """Return the first YYYY-MM-DD date found in the header block."""
    for line in header_lines:
        m = _DATE_RE.search(line)
        if m:
            return m.group(0)
    return "—"


def _parse_human_fork(header_lines: list[str]) -> str:
    """Parse human_fork field.

    Returns 'true', 'false', 'mixed' (both appear, e.g. slice-based), or '—'.
    Searches each header line containing 'human_fork' for adjacent true/false.
    """
    has_true = False
    has_false = False
    for line in header_lines:
        if "human_fork" not in line.lower():
            continue
        lower = line.lower()
        if "true" in lower:
            has_true = True
        if "false" in lower:
            has_false = True
    if has_true and has_false:
        return "mixed"
    if has_true:
        return "true"
    if has_false:
        return "false"
    return "—"


def _parse_person_affecting(header_lines: list[str]) -> str:
    """Parse person_affecting: true|false field; returns '—' if absent."""
    for line in header_lines:
        # Match: **person_affecting:** true/false  OR  person_affecting: true/false
        m = re.search(r"person_affecting[^:]*:\**\s*(true|false)", line, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return "—"


# ---------------------------------------------------------------------------
# ADR file parser
# ---------------------------------------------------------------------------


def parse_adr(path: Path) -> dict[str, object]:
    """Parse a single ADR file and return its metadata as a dict."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    header_lines = lines[:_HEADER_LINES]

    m = _ADR_FILENAME_RE.match(path.name)
    number = int(m.group(1)) if m else 0

    return {
        "number": number,
        "filename": path.name,
        "title": _parse_title(lines, number),
        "status": _parse_status(header_lines),
        "date": _parse_date(header_lines),
        "human_fork": _parse_human_fork(header_lines),
        "person_affecting": _parse_person_affecting(header_lines),
    }


# ---------------------------------------------------------------------------
# Table generation
# ---------------------------------------------------------------------------


def _generate_table(adrs: list[dict[str, object]]) -> str:
    """Produce a markdown table for the given ADR metadata list."""
    header = "| ADR | Title | Status | Date | human_fork | person_affecting |"
    separator = "|-----|-------|--------|------|------------|-----------------|"
    rows: list[str] = [header, separator]
    for adr in adrs:
        num_str = f"{adr['number']:04d}"
        link = f"[{num_str}]({adr['filename']})"
        rows.append(
            f"| {link} | {adr['title']} | {adr['status']} | {adr['date']}"
            f" | {adr['human_fork']} | {adr['person_affecting']} |"
        )
    return "\n".join(rows)


def generate_region(decisions_dir: Path) -> str:
    """Scan *decisions_dir* and produce the content between the two sentinels (exclusive).

    Returns a string that starts and ends with '\\n', so inserting it directly
    after the BEGIN sentinel line and before the END sentinel line yields a
    well-formed markdown table delimited by blank-line separators.
    """
    adrs: list[dict[str, object]] = []
    for path in decisions_dir.iterdir():
        m = _ADR_FILENAME_RE.match(path.name)
        if not m:
            continue
        number = int(m.group(1))
        if number < _ADR_MIN_NUMBER:
            continue
        adrs.append(parse_adr(path))

    adrs.sort(key=lambda a: a["number"])
    table = _generate_table(adrs)
    return f"\n{table}\n"


# ---------------------------------------------------------------------------
# README read / write
# ---------------------------------------------------------------------------


def _sentinel_positions(text: str, readme_path: Path) -> tuple[int, int]:
    """Return (after_begin, before_end) character positions.

    *after_begin* is the index immediately after the BEGIN sentinel string.
    *before_end* is the index of the first character of the END sentinel string.
    Exits non-zero on missing sentinel.
    """
    begin_pos = text.find(BEGIN_SENTINEL)
    if begin_pos == -1:
        print(
            f"ERROR: BEGIN sentinel not found in {readme_path}.\nExpected: {BEGIN_SENTINEL!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    after_begin = begin_pos + len(BEGIN_SENTINEL)

    end_pos = text.find(END_SENTINEL, after_begin)
    if end_pos == -1:
        print(
            f"ERROR: END sentinel not found after BEGIN in {readme_path}.\n"
            f"Expected: {END_SENTINEL!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    return after_begin, end_pos


def write_mode(decisions_dir: Path, readme_path: Path) -> None:
    """Regenerate the generated region of *readme_path* in place."""
    text = readme_path.read_text(encoding="utf-8")
    after_begin, before_end = _sentinel_positions(text, readme_path)

    region = generate_region(decisions_dir)
    new_text = text[:after_begin] + region + text[before_end:]
    readme_path.write_text(new_text, encoding="utf-8")


def check_mode(decisions_dir: Path, readme_path: Path) -> int:
    """Compare the committed README region against a fresh regeneration.

    Returns 0 if identical, 1 if drifted (prints a unified diff to stderr).
    """
    text = readme_path.read_text(encoding="utf-8")
    after_begin, before_end = _sentinel_positions(text, readme_path)

    current_region = text[after_begin:before_end]
    expected_region = generate_region(decisions_dir)

    if current_region == expected_region:
        return 0

    diff = difflib.unified_diff(
        current_region.splitlines(keepends=True),
        expected_region.splitlines(keepends=True),
        fromfile="README.md (committed)",
        tofile="README.md (regenerated)",
    )
    print("".join(diff), file=sys.stderr, end="")
    return 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--decisions-dir",
        type=Path,
        default=_DEFAULT_DECISIONS_DIR,
        metavar="DIR",
        help="Directory containing ADR files (default: docs/decisions)",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=_DEFAULT_README,
        metavar="FILE",
        help="README file to update (default: docs/decisions/README.md)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check mode: exit 0 if in-sync, 1 if drifted (prints diff to stderr)",
    )
    args = parser.parse_args()

    if args.check:
        sys.exit(check_mode(args.decisions_dir, args.readme))
    else:
        write_mode(args.decisions_dir, args.readme)
        sys.exit(0)


if __name__ == "__main__":
    main()
