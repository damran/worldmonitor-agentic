"""Gate 0 Slice 0b — Documentation truth-up PRIMARY acceptance test.

Asserts ABSENCE of known-stale strings in live documentation files and PRESENCE
of required structural markers, BEFORE the builder edits any docs.  A GREEN run
proves slice 0b is complete.

Scoping rules (from .claude/gate.scope):
- Excluded (historical/immutable): docs/decisions/0*.md, docs/reviews/**,
  docs/fable-review/**, docs/runbooks/smoke-run-report-*, CLAUDE.md, AGENTS.md,
  .clinerules.
- docs/decisions/README.md: only the PRE-SENTINEL region (above the line
  containing ``<!-- BEGIN GENERATED ADR INDEX``) is checked; the generated region
  may contain historical ADR titles (e.g. "Tenant isolation…", "system of
  record…") and must NOT be touched by hand.
- docs/40_ROADMAP.md Phase-6 ``multi-tenant RBAC`` is an intentional allowed
  exception — never assert its absence.

The test currently FAILS because live docs still carry stale language that
0b is charged with removing.  Each failure names the file and the offending
fragment so the builder knows exactly what to fix.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Infrastructure helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]

# Splits docs/decisions/README.md into hand-maintained vs. machine-generated.
_README_SENTINEL = "<!-- BEGIN GENERATED ADR INDEX"


def read_live(rel: str) -> str:
    """Read a live doc file; raise AssertionError if it does not exist."""
    path = REPO_ROOT / rel
    assert path.is_file(), f"expected live doc at {path!s}"
    return path.read_text(encoding="utf-8")


def pre_sentinel(text: str) -> str:
    """Return the hand-maintained region above the generated-index sentinel."""
    idx = text.find(_README_SENTINEL)
    return text[:idx] if idx != -1 else text


def extract_phase3_section(roadmap: str) -> str:
    """Extract Phase-3 section text from its ## heading to the next ## heading."""
    marker = "## Phase 3"
    start = roadmap.find(marker)
    if start == -1:
        return ""
    after = roadmap[start + len(marker) :]
    end = after.find("\n## ")
    if end == -1:
        return roadmap[start:]
    return roadmap[start : start + len(marker) + end]


# ---------------------------------------------------------------------------
# A. Tenancy — ADR 0042: single-tenant, all per-tenant scoping removed
# ---------------------------------------------------------------------------


def test_a1_ontology_no_tenant_id() -> None:
    """`tenant_id` (backtick-wrapped) must be absent from docs/20_ONTOLOGY.md.

    §2 store description (line ~28) writes
    ``every node/edge carries provenance + `tenant_id``` — stale after ADR 0042.
    """
    text = read_live("docs/20_ONTOLOGY.md")
    stale = "`tenant_id`"
    assert stale not in text, (
        f"docs/20_ONTOLOGY.md still contains {stale!r} "
        "(stale after ADR 0042 single-tenancy teardown — "
        "remove the tenant_id reference from the store description in §2)"
    )


def test_a2_plugin_framework_no_tenant_scoping() -> None:
    """``tenant-scoped`` and ``tenant_id`` must be absent from docs/30_PLUGIN_FRAMEWORK.md.

    §1 describes plugin instances as a "tenant-scoped … row"; §9 cross-cutting
    rules list ``tenant-scoped (`tenant_id`)`` — both stale after ADR 0042.
    """
    text = read_live("docs/30_PLUGIN_FRAMEWORK.md")
    stale = ["tenant-scoped", "tenant_id"]
    hits = [s for s in stale if s in text]
    assert not hits, (
        f"docs/30_PLUGIN_FRAMEWORK.md still contains tenant-scoping language: {hits!r} "
        "(ADR 0042: remove all tenant-scoped / tenant_id references from §1 and §9)"
    )


def test_a3_roadmap_no_tenant_scoping() -> None:
    """Multiple stale tenant strings must be absent from docs/40_ROADMAP.md.

    Present now:
    - ``= first tenant``     — Phase-0 Zitadel org item
    - ``tenant-context middleware`` — Phase-0 FastAPI item
    - ``tenant-aware``       — Phase-0 done-when line
    - ``tenant-scoped``      — repo layout api/ comment + rules-engine text
    - ``tenant context``     — repo layout authz/ comment (no hyphen)
    - ``tenant_id``          — Phase-1 checkbox + repo layout comment

    NOTE: ``multi-tenant RBAC`` in the Phase-6 deferred cloud-tier REMAINS ALLOWED.
    """
    text = read_live("docs/40_ROADMAP.md")
    stale = [
        "= first tenant",
        "tenant-context middleware",
        "tenant-aware",
        "tenant-scoped",
        "tenant context",
        "`tenant_id`",
    ]
    hits = [s for s in stale if s in text]
    assert not hits, (
        f"docs/40_ROADMAP.md still contains stale tenant-scoping fragments: {hits!r} "
        "(ADR 0042: remove per-tenant-scoping language from Phase-0/1 items and repo "
        "layout; Phase-6 'multi-tenant RBAC' may remain)"
    )


def test_a4_smoke_runbook_no_tenant_scoping() -> None:
    """Tenant-scoping strings must be absent from docs/runbooks/smoke-run.md.

    The seed script uses ``TENANT = "smoke"`` and ``tenant_id=TENANT``; the
    review CLI uses ``--tenant smoke`` — all stale post-ADR 0042.
    """
    text = read_live("docs/runbooks/smoke-run.md")
    stale = [
        "--tenant",
        "TENANT =",
        'TENANT="smoke"',
        "tenant_id",
    ]
    hits = [s for s in stale if s in text]
    assert not hits, (
        f"docs/runbooks/smoke-run.md still contains tenant-scoping fragments: {hits!r} "
        "(ADR 0042: remove --tenant CLI flags, TENANT variable, and tenant_id "
        "references from the runbook — single-tenant system has no per-tenant scoping)"
    )


def test_a5_decisions_readme_presential_no_tenant_scoping() -> None:
    """The hand-maintained region of docs/decisions/README.md must shed tenant-scoping.

    Row 14 currently reads: ``org model = tenants``.
    After stripping the allowed ``single-tenant`` occurrences, no residual ``tenant``
    token should remain in the pre-sentinel region.

    Only the pre-sentinel region is checked; the generated region holds historical
    ADR titles that may mention tenancy.
    """
    full = read_live("docs/decisions/README.md")
    region = pre_sentinel(full)

    assert "org model = tenants" not in region, (
        "docs/decisions/README.md pre-sentinel region still contains "
        "'org model = tenants' in row 14 — rewrite row 14 to reflect "
        "ADR 0042 single-tenant (remove the 'org model = tenants' clause)"
    )

    # Strip the allowed compound "single-tenant", then assert no bare "tenant" remains.
    cleaned = region.replace("single-tenant", "")
    assert "tenant" not in cleaned, (
        "docs/decisions/README.md pre-sentinel region still contains 'tenant' "
        "outside of a 'single-tenant' phrase — rewrite row 14 to remove all "
        "per-tenant-scoping language "
        "('single-tenant' is allowed to remain; bare 'tenant'/'tenants' is not)"
    )


# ---------------------------------------------------------------------------
# B. Store framing — ADR 0095: Postgres statement-log = SoR; Neo4j = projection
# ---------------------------------------------------------------------------


def test_b1_ontology_no_neo4j_sor_framing() -> None:
    """Stale Neo4j-as-SoR language must be absent from docs/20_ONTOLOGY.md.

    §2 labels the store as ``Store = Neo4j`` and §5 says
    ``WorldMonitor's graph stays the system of record`` — both must be reframed
    per ADR 0095 (Postgres statement-log = SoR; Neo4j = derived projection).
    """
    text = read_live("docs/20_ONTOLOGY.md")
    stale = [
        "Store = Neo4j",
        "graph stays the system of record",
    ]
    hits = [s for s in stale if s in text]
    assert not hits, (
        f"docs/20_ONTOLOGY.md still contains Neo4j-as-SoR framing: {hits!r} "
        "(ADR 0095: Postgres statement-log is the SoR; Neo4j is a derived, "
        "rebuildable projection — reframe §2 store description and §5 STIX paragraph)"
    )


def test_b2_architecture_no_neo4j_sor_framing() -> None:
    """Specific Neo4j-as-SoR labels must be absent from docs/10_ARCHITECTURE.md.

    L4 row of the layered-model diagram reads ``property-graph SYSTEM OF RECORD``.
    Stack table cell reads ``Property-graph SoR``.  Both must be reframed per ADR 0095.

    NOTE: ``not the system of record`` (OpenCTI demotion) and other historical rows
    are ALLOWED — only the two specific Neo4j-as-SoR substrings are forbidden.
    """
    text = read_live("docs/10_ARCHITECTURE.md")
    stale = [
        "property-graph SYSTEM OF RECORD",
        "Property-graph SoR",
    ]
    hits = [s for s in stale if s in text]
    assert not hits, (
        f"docs/10_ARCHITECTURE.md still contains Neo4j-as-SoR labels: {hits!r} "
        "(ADR 0095: reframe the L4 diagram label and the stack-table Neo4j row; "
        "OpenCTI 'not the system of record' lines are allowed to remain)"
    )


def test_b3_decisions_readme_presential_sor_reframed() -> None:
    """docs/decisions/README.md pre-sentinel row 2 must no longer assert Neo4j as plain SoR.

    Currently row 2 reads: ``Property graph (Neo4j + GDS) as system of record``.
    After reframing the pre-sentinel region must:
      (1) NOT contain ``as system of record``
      (2) Reference ``0095`` (positive anchor that the row was updated)
    """
    full = read_live("docs/decisions/README.md")
    region = pre_sentinel(full)

    assert "as system of record" not in region, (
        "docs/decisions/README.md pre-sentinel region still contains "
        "'as system of record' in row 2 — reframe to reflect ADR 0095 "
        "(Postgres statement-log = SoR, Neo4j = derived rebuildable projection)"
    )

    assert "0095" in region, (
        "docs/decisions/README.md pre-sentinel region does not reference ADR 0095 — "
        "update row 2 to point to ADR 0095 after the SoR reframing so the "
        "foundational table self-consistently reflects the current decision"
    )


# ---------------------------------------------------------------------------
# C. Egress identity — ADR 0094 D2: sovereignty per-workload, not absolute
# ---------------------------------------------------------------------------


def test_c1_ui_no_absolute_perimeter_claim() -> None:
    """``data never leaves the perimeter`` must be absent from docs/70_UI_AND_EXPERIENCE.md.

    §4A currently carries the absolute sovereignty claim; ADR 0094 D2 replaces it
    with per-workload/local-default framing (e.g. the basemap tile comment is fine
    but the categorical identity claim must go).
    """
    text = read_live("docs/70_UI_AND_EXPERIENCE.md")
    stale = "data never leaves the perimeter"
    assert stale not in text, (
        f"docs/70_UI_AND_EXPERIENCE.md still contains {stale!r} "
        "(ADR 0094 D2: replace the absolutist perimeter claim with per-workload "
        "sovereignty framing in §4A)"
    )


# ---------------------------------------------------------------------------
# D. Ontology — wm:Article dropped; use FtM-native Article
# ---------------------------------------------------------------------------


def test_d1_ontology_no_wm_article() -> None:
    """`wm:Article` must be absent from docs/20_ONTOLOGY.md.

    The §4 extensions table still proposes ``wm:Article`` — this entry was dropped
    in favour of using FtM-native ``Article`` (already produced by FeedConnector).
    """
    text = read_live("docs/20_ONTOLOGY.md")
    stale = "wm:Article"
    assert stale not in text, (
        f"docs/20_ONTOLOGY.md still contains {stale!r} "
        "(wm:Article was dropped — use FtM-native Article; "
        "remove the wm:Article entry from the §4 extensions table)"
    )


# ---------------------------------------------------------------------------
# E. Positive / structural assertions
# ---------------------------------------------------------------------------


def test_e1_gate_ledger_is_tombstone() -> None:
    """docs/GATE_LEDGER.md must be a short tombstone, not the full operational ledger.

    Currently the file is the full ~100+ non-empty-line ledger.  After 0b it must:
      - Be small (≤ 40 non-empty lines)
      - Reference docs/40_ROADMAP.md (so readers find the live gate tracking)
      - Carry a retirement marker (tombstone / retired / superseded / no longer maintained)
    """
    text = read_live("docs/GATE_LEDGER.md")

    non_empty = [ln for ln in text.splitlines() if ln.strip()]
    count = len(non_empty)
    assert count <= 40, (
        f"docs/GATE_LEDGER.md has {count} non-empty lines — expected ≤ 40 for a "
        "tombstone.  Replace the full ledger content with a short retirement notice "
        "that points to docs/40_ROADMAP.md and the ADR index."
    )

    assert "40_ROADMAP.md" in text, (
        "docs/GATE_LEDGER.md (tombstone) must reference docs/40_ROADMAP.md "
        "so readers know where the live gate tracking lives."
    )

    markers = ["tombstone", "retired", "superseded", "no longer maintained"]
    assert any(m in text.lower() for m in markers), (
        f"docs/GATE_LEDGER.md (tombstone) must contain a retirement marker — "
        f"one of: {markers!r} (case-insensitive)"
    )


def test_e2_orient_no_gate_ledger_reference() -> None:
    """.claude/agents/orient.md must not instruct reading GATE_LEDGER.

    Step 2 currently says ``Read `docs/GATE_LEDGER.md```; once the ledger is
    retired the orient agent must no longer require it.
    """
    text = read_live(".claude/agents/orient.md")
    assert "GATE_LEDGER" not in text, (
        ".claude/agents/orient.md still references GATE_LEDGER — "
        "remove the GATE_LEDGER read instruction from step 2 "
        "(the ledger is retired; orient should read the roadmap and ADR index instead)"
    )


def test_e3_architecture_review_has_frozen_banner() -> None:
    """docs/ARCHITECTURE_REVIEW.md must carry a dated/frozen snapshot banner near the top.

    Currently the file opens with a plain title + description — no frozen/snapshot
    marker and no date.  The first 12 lines must contain a ``2026`` date AND one of
    ``snapshot`` / ``frozen`` (case-insensitive).
    """
    text = read_live("docs/ARCHITECTURE_REVIEW.md")
    first_12 = "\n".join(text.splitlines()[:12])
    lower_12 = first_12.lower()

    has_date = "2026" in first_12
    has_frozen = "snapshot" in lower_12 or "frozen" in lower_12

    assert has_date and has_frozen, (
        "docs/ARCHITECTURE_REVIEW.md is missing a dated/frozen snapshot banner in the "
        f"first 12 lines (has_2026={has_date!r}, has_snapshot_or_frozen={has_frozen!r}). "
        "Add a banner near the top marking the file as a frozen point-in-time review "
        "with a 2026 date."
    )


def test_e4_roadmap_phase3_checkboxes_unticked() -> None:
    """The Phase-3 operational checkboxes in docs/40_ROADMAP.md must remain unticked.

    The 4 Hermes deployment items must stay ``- [ ]`` — the doc truth-up must NOT
    tick them (they track actual deployment status, not documentation hygiene).
    """
    text = read_live("docs/40_ROADMAP.md")
    p3 = extract_phase3_section(text)
    assert p3, "Phase-3 section ('## Phase 3') not found in docs/40_ROADMAP.md"

    assert "- [x]" not in p3, (
        "docs/40_ROADMAP.md Phase-3 section contains a ticked checkbox '- [x]' — "
        "slice 0b must NOT tick Phase-3 operational checkboxes; only add shipped-infra "
        "notes (S1/S3b/ADRs), do not mark deployment items complete."
    )


def test_e5_roadmap_phase3_references_shipped_infra() -> None:
    """docs/40_ROADMAP.md Phase-3 section must reference at least one shipped infra slice.

    The shipped Phase-3 infrastructure (S1 MCP-auth, S2 LiteLLM, S3a HTTP-shim,
    S3b Hermes compose; ADRs 0089-0093; PRs #149-#153) must be mentioned in the
    section so the roadmap reflects reality.  Currently none of the anchors appear.
    """
    text = read_live("docs/40_ROADMAP.md")
    p3 = extract_phase3_section(text)
    assert p3, "Phase-3 section ('## Phase 3') not found in docs/40_ROADMAP.md"

    anchors = ["S1", "S3b", "0089", "#149"]
    assert any(a in p3 for a in anchors), (
        "docs/40_ROADMAP.md Phase-3 section does not reference any shipped infra slice — "
        f"expected at least one of {anchors!r} to appear in the section. "
        "Add a note about the shipped Phase-3 infrastructure "
        "(S1 MCP-auth / S2 LiteLLM / S3a HTTP-shim / S3b Hermes compose, "
        "ADRs 0089-0093, PRs #149-#153)."
    )
