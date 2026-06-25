"""Gate 0 slice-2 PRIMARY invariant — the durable *contract-drift* guard.

This is the tripwire that turns "the always-loaded ground truth contradicts the
code" from a silent landmine into a RED BUILD. It touches only files (no
database / Neo4j), so it is intentionally NOT marked ``@pytest.mark.integration``
and lives at the repo-root ``tests/`` dir so it runs in the ``quality`` CI job
(``pytest -m "not integration"``).

It encodes two invariants the project's own ground-truth asserts:

1. **mirror-sync** — ``CLAUDE.md``, ``AGENTS.md`` and ``.clinerules`` must be
   BYTE-IDENTICAL. CLAUDE.md's own header commands: "mirror verbatim into
   ``AGENTS.md`` and ``.clinerules``". If the three drift, an agent reloading a
   different mirror reloads a different ground truth.

2. **claim<->code tenancy consistency (bidirectional)** — the contract's tenancy
   claim must match the code. If ``src/`` still scopes by ``tenant_id`` the
   contract must claim multi-tenant; if ``src/`` is clean of live ``tenant_id``
   the contract must claim single-tenant. The two must never disagree.

Both assertions are designed to FAIL on the current tree (proving the guard is
non-vacuous) and to PASS only once Gate 0 slice-1 (functional teardown) and
slice-2 (contract amendment) have both landed. See ``docs/reviews/GATE_0_SPEC.md``
§8.2.
"""

from __future__ import annotations

import difflib
from pathlib import Path

# Resolve the repo root robustly from THIS file's location, not the cwd:
# tests/test_contract_consistency.py  ->  parents[1] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]

# The three always-loaded ground-truth mirrors that MUST be byte-identical.
MIRROR_FILES = ("CLAUDE.md", "AGENTS.md", ".clinerules")

# The source tree whose live tenancy posture must agree with the contract.
SRC_DIR = REPO_ROOT / "src" / "worldmonitor"

# Migration history legitimately references the dropped column name (0001-0004
# `op.drop_column("...", "tenant_id")` etc.), so it is EXCLUDED from the
# "live tenant_id" definition. A path is excluded iff this segment appears in it.
MIGRATIONS_EXCLUDE = ("db", "migrations", "versions")

# The literal token a live multi-tenant scoping site uses in code.
TENANT_TOKEN = "tenant_id"

# Sentinel phrases in the contract (CLAUDE.md) that encode the tenancy CLAIM.
# The multi-tenant claim is asserted by EITHER of these substrings; the
# single-tenant claim by the "single-tenant" substring.
MULTI_TENANT_PHRASES = ("tenant_id` everywhere", "tenant-scoped from day one")
SINGLE_TENANT_PHRASE = "single-tenant"


def _read_text(name: str) -> str:
    path = REPO_ROOT / name
    assert path.is_file(), f"expected ground-truth mirror {path} to exist"
    return path.read_text(encoding="utf-8")


def _live_tenant_id_hits() -> list[str]:
    """Return ``"<relpath>:<lineno>: <line>"`` for every LIVE tenant_id site.

    "Live" = a ``tenant_id`` occurrence in ``src/worldmonitor`` that is NOT
    under ``db/migrations/versions/`` (migration history may reference the
    dropped column name). This mirrors the gate's grep gate:
        grep -rn 'tenant_id' src/worldmonitor/ --include='*.py'
    minus the migrations carve-out. Walking with pathlib keeps it stdlib-only
    and avoids shell exit-code handling.
    """
    assert SRC_DIR.is_dir(), f"expected source tree {SRC_DIR} to exist"
    hits: list[str] = []
    for py_file in sorted(SRC_DIR.rglob("*.py")):
        rel = py_file.relative_to(REPO_ROOT)
        # Skip the migration-version history (legit references to the column).
        parts = rel.parts
        if any(
            parts[i : i + len(MIGRATIONS_EXCLUDE)] == MIGRATIONS_EXCLUDE for i in range(len(parts))
        ):
            continue
        for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            if TENANT_TOKEN in line:
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_ground_truth_mirrors_are_byte_identical() -> None:
    """CLAUDE.md, AGENTS.md and .clinerules must be byte-for-byte identical.

    CLAUDE.md's own header mandates "mirror verbatim into AGENTS.md and
    .clinerules". If any pair diverges, agents loading different mirrors load a
    different ground truth. We compare every mirror against the first
    (CLAUDE.md, the source of truth) and name the diverging pair with a unified
    diff so the fix is obvious.
    """
    reference_name = MIRROR_FILES[0]
    reference_bytes = (REPO_ROOT / reference_name).read_bytes()
    reference_text = (REPO_ROOT / reference_name).read_text(encoding="utf-8")

    mismatches: list[str] = []
    for other_name in MIRROR_FILES[1:]:
        other_bytes = (REPO_ROOT / other_name).read_bytes()
        if other_bytes == reference_bytes:
            continue
        other_text = (REPO_ROOT / other_name).read_text(encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                reference_text.splitlines(keepends=True),
                other_text.splitlines(keepends=True),
                fromfile=reference_name,
                tofile=other_name,
                n=1,
            )
        )
        mismatches.append(
            f"{reference_name} and {other_name} DIVERGE "
            f"({len(reference_bytes)} vs {len(other_bytes)} bytes):\n{diff}"
        )

    assert not mismatches, (
        "Ground-truth mirrors are NOT byte-identical, violating CLAUDE.md's own "
        '"mirror verbatim into AGENTS.md and .clinerules" rule:\n\n' + "\n".join(mismatches)
    )


def test_contract_tenancy_claim_matches_code() -> None:
    """The CLAUDE.md tenancy CLAIM must agree with the live ``src/`` code.

    Bidirectional:
      * code has live ``tenant_id``  -> CLAUDE.md MUST claim multi-tenant
        ("tenant_id` everywhere" / "tenant-scoped from day one") and MUST NOT
        say "single-tenant".
      * code has NO live ``tenant_id`` -> CLAUDE.md MUST say "single-tenant" and
        MUST NOT carry the multi-tenant "tenant_id` everywhere" claim.

    A mismatch is a standing contradiction between the always-loaded ground
    truth and the code — exactly the landmine this gate exists to detonate at
    build time.
    """
    claude = _read_text("CLAUDE.md")

    code_is_multi_tenant = bool(_live_tenant_id_hits())
    live_hits = _live_tenant_id_hits()

    contract_claims_multi = any(phrase in claude for phrase in MULTI_TENANT_PHRASES)
    contract_claims_single = SINGLE_TENANT_PHRASE in claude

    if code_is_multi_tenant:
        # Code scopes by tenant_id -> contract must claim multi-tenant, must not
        # claim single-tenant.
        assert contract_claims_multi and not contract_claims_single, (
            "CONTRADICTION: src/ has live tenant_id scoping "
            f"({len(live_hits)} site(s), e.g. {live_hits[0]!r}) so CLAUDE.md "
            "must make the multi-tenant claim and must NOT say 'single-tenant'. "
            f"Found: multi_tenant_claim={contract_claims_multi}, "
            f"single_tenant_claim={contract_claims_single}."
        )
    else:
        # Code is single-tenant (no live tenant_id) -> contract must say
        # single-tenant and must NOT carry the multi-tenant claim.
        assert contract_claims_single and not contract_claims_multi, (
            "CONTRADICTION: src/worldmonitor has NO live tenant_id (the "
            "single-tenancy teardown removed it; only db/migrations/versions/ "
            "references the dropped column name), yet CLAUDE.md still asserts "
            "the multi-tenant contract. CLAUDE.md must say 'single-tenant' and "
            "must NOT carry the 'tenant_id` everywhere' / 'tenant-scoped from "
            "day one' multi-tenant claim. "
            f"Found: single_tenant_claim={contract_claims_single}, "
            f"multi_tenant_claim={contract_claims_multi} "
            f"(phrases present: "
            f"{[p for p in MULTI_TENANT_PHRASES if p in claude]})."
        )
