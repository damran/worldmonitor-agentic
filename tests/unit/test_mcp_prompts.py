"""Unit tests — Gate F-4: MCP prompts as analyst playbooks (ADR 0125).

Drives ``build_server(...)`` (stdio) directly against a fake Neo4j client that RAISES if
touched at all — prompts are pure text (spec §0/§3: no session, no Cypher) — plus the two
thin, module-level prompt functions (``prompt_entity_workup`` / ``prompt_freshness_audit``),
mirroring the existing tool-test idiom in ``test_mcp_server.py``. No JSON-RPC loop; wire-level
coverage lives in the integration suites.

CONTRACT ASSUMED (the builder MUST match these names/signatures exactly — spec §4/§5/§6):
    worldmonitor.mcp.server
        prompt_entity_workup(entity_id: str) -> str
        prompt_freshness_audit(connector_id: str = "") -> str
        _register_prompts(server: FastMCP) -> None          # registers both prompts, no client
        _PROMPT_ARG_MAX_LEN: int == 256
        _ENTITY_WORKUP_TEMPLATE / _FRESHNESS_AUDIT_TEMPLATE  # the exact contract text, spec §4
    Both ``build_server`` and ``build_http_app`` call ``_register_prompts`` after registering
    the five read tools (one shared registration site — no drift between transports).

Symbols that do not exist on the current tree (``prompt_entity_workup``,
``prompt_freshness_audit``, ``_register_prompts``) are imported LOCALLY inside each test that
needs them (fail-soft, mirroring the existing local-import idiom already used in
``test_mcp_server.py`` for F-3/F-5): a missing symbol fails ONLY that test, not the whole
module's collection — ``worldmonitor.mcp.server`` itself already exists (F-1..F-5 tools), so a
bare top-level import of the *module* never raises.

RED today: no prompt is registered anywhere in ``mcp/server.py`` — ``build_server(...).
list_prompts()`` returns ``[]``, and ``server.get_prompt("entity-workup", ...)`` raises
``ValueError("Unknown prompt: entity-workup")`` instead of rendering a playbook.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from worldmonitor.mcp.server import build_server

# ------------------------------------------------------------------------------------------
# Contract-pinned constants (this test's OWN oracle — NOT imported from prod, since the
# production constant does not exist yet). Spec §5.1 locks the cap at exactly 256.
# ------------------------------------------------------------------------------------------
_PROMPT_ARG_MAX_LEN = 256

_INJECTION_ID = '") DETACH DELETE n //'  # same hostile shape used across the MCP test suite

_ALL_TOOL_NAMES = frozenset(
    {"get_entity", "get_neighbors", "get_provenance", "find_paths", "get_entity_dossier"}
)
_ALL_PROMPT_NAMES = frozenset({"entity-workup", "freshness-audit"})

_SIX_FRESHNESS_STATES = ("disabled", "error", "no_data", "very_stale", "stale", "fresh")
_FIVE_TOOLS_IN_ORDER = (
    "get_entity",
    "get_provenance",
    "get_neighbors",
    "find_paths",
    "get_entity_dossier",
)

# The two prompt templates, VERBATIM from docs/reviews/GATE_F4_MCP_PROMPTS_SPEC.md §4 —
# this test file's own copy of the contract, independent of whatever the builder writes in
# src/worldmonitor/mcp/server.py. `{{count, sample}}` is the doubled-brace escape (renders to
# the literal `{count, sample}` after `.format()`); the only real substitution fields are
# `{entity_id}` / `{scope_line}`.
_ENTITY_WORKUP_TEMPLATE = """Entity workup playbook — entity_id={entity_id}

Purpose: assemble a provenance-complete picture of one resolved entity as ranked leads
for a human analyst. This is a read-only orientation aid, never an automated verdict.

Each step names one graph-read tool, its purpose, and the argument shape to pass. Work
through them in order.

Step 1 - Anchor the entity.
  Tool: get_entity(entity_id={entity_id})
  Purpose: confirm the node exists and read its properties and provenance
  (prov_source_id, prov_retrieved_at, prov_reliability, prov_source_record). If the tool
  reports "entity not found", stop: this id is not in the resolved graph.

Step 2 - Read the provenance.
  Tool: get_provenance(entity_id={entity_id})
  Purpose: record where each fact about this entity came from before drawing any
  inference. Every node carries provenance; weigh a single-source or low-reliability fact
  as a weaker lead.

Step 3 - Map the immediate neighbourhood.
  Tool: get_neighbors(entity_id={entity_id}, hops=1)
  Purpose: list the entities one edge away and how they connect. On a high-degree node,
  pass summary=true first for a {{count, sample}} taste before requesting the full list.

Step 4 - Trace a specific connection.
  Tool: find_paths(from_id={entity_id}, to_id=<a second resolved id>, max_hops=<within
  the hop cap>)
  Purpose: when you have a hypothesis linking this entity to another, look for bounded
  paths between them. No path within the hop bound is not proof of no relationship.

Step 5 - Assemble the dossier.
  Tool: get_entity_dossier(entity_id={entity_id}, hops=1)
  Purpose: retrieve the deterministic entity + neighbours + provenance + merge_history
  bundle in a single call for the written workup.

Framing: report ranked hypotheses with their provenance and confidence for human review;
do not merge, attribute, or label a person from this workup. Surface the leads and their
sources and let a human decide."""

_FRESHNESS_AUDIT_TEMPLATE = """Source freshness audit playbook

{scope_line}

Purpose: assess how current the ingested sources are, so an analyst can weight findings by
the freshness of their underlying feeds. A stale or missing source is a lead about a
collection gap, not a verdict about the world.

Freshness is served by the read-only REST endpoint GET /sources/freshness (auth-gated,
single-tenant). There is no freshness MCP tool in this version - use the REST surface. The
response lists, per connector instance: connector_id, an opaque instance_id, the raw
status, the derived freshness_status, last_success_at, age_seconds, plus a summary
count-by-state and the configured staleness budget.

Step 1 - Pull the freshness surface.
  Call: GET /sources/freshness
  Purpose: read the current per-instance freshness_status and the summary counts.

Step 2 - Read each instance's freshness_status. It is one of six values, in priority
order:
  - disabled    administratively off; expect no data, not a fault.
  - error       auto-hard-disabled after repeated failures; a collection gap to escalate
                to a human operator.
  - no_data     active but never had a successful ingest; treat downstream coverage as
                absent.
  - very_stale  last success older than the very-stale budget; findings may be badly out
                of date.
  - stale       last success older than the stale budget; weight findings accordingly.
  - fresh       last success within budget.

Step 3 - Summarise the gaps.
  Purpose: from the summary counts, report how many instances are error / no_data /
  very_stale / stale versus fresh. Rank error and no_data instances first - those are the
  collection gaps most likely to bias an analysis.

Framing: freshness is operational metadata about pipelines, never data about a person.
Read it as evidence of collection coverage and gaps and surface those gaps to a human; do
not treat a stale or missing source as a factual claim about any entity."""


# --------------------------------------------------------------------------------------
# A Neo4j-shaped fake that RAISES if touched AT ALL. Passing this to build_server proves
# STRUCTURALLY (not merely by absence of an assertion) that registering + rendering
# prompts never opens a session and never issues Cypher (spec §3 "Append-only / read-only").
# --------------------------------------------------------------------------------------
class _NoTouchFake:
    def execute_read(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a prompt must NEVER call execute_read")

    def execute_write(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a prompt must NEVER call execute_write")

    def session(self) -> Any:
        raise AssertionError("a prompt must NEVER open a session")

    def verify(self) -> None:
        raise AssertionError("build_server must not verify/connect an injected client")


def _tool_names(server: Any) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


def _prompt_names(server: Any) -> set[str]:
    return {p.name for p in asyncio.run(server.list_prompts())}


def _get_prompt(server: Any, name: str, arguments: dict[str, str]) -> Any:
    return asyncio.run(server.get_prompt(name, arguments))


def _prompt_text(result: Any) -> str:
    assert len(result.messages) == 1, f"a prompt must render exactly one message: {result!r}"
    message = result.messages[0]
    assert message.role == "user", (
        f"a str-returning prompt fn must render a user message: {message!r}"
    )
    assert message.content.type == "text", f"prompt content must be text: {message.content!r}"
    return message.content.text


# ========================================================================================
# Prompt set + tool-surface non-disturbance.
# ========================================================================================
def test_prompt_set_is_exactly_the_two() -> None:
    """prompts/list exposes EXACTLY {'entity-workup', 'freshness-audit'} — no more, no fewer.

    RED today: no prompt is registered anywhere in mcp/server.py, so
    ``build_server(...).list_prompts()`` returns an empty list.
    """
    server = build_server(neo4j_client=_NoTouchFake())
    assert _prompt_names(server) == _ALL_PROMPT_NAMES


def test_prompts_do_not_disturb_the_five_tools() -> None:
    """Registering the two prompts must not add, remove, or rename any tool.

    GREEN today (there is nothing yet to disturb) and MUST stay green once the builder
    adds ``_register_prompts`` — this is the regression pin proving prompts are a
    genuinely separate MCP surface from tools (spec §0 NON-goal: "no new tool").
    """
    server = build_server(neo4j_client=_NoTouchFake())
    assert _tool_names(server) == _ALL_TOOL_NAMES


def test_prompt_args_declared() -> None:
    """entity-workup declares entity_id required=True; freshness-audit declares
    connector_id required=False (spec §1 "func_metadata" argument derivation)."""
    server = build_server(neo4j_client=_NoTouchFake())
    prompts = {p.name: p for p in asyncio.run(server.list_prompts())}

    assert "entity-workup" in prompts, "entity-workup prompt not registered"
    entity_args = {a.name: a.required for a in (prompts["entity-workup"].arguments or [])}
    assert entity_args == {"entity_id": True}, f"unexpected entity-workup args: {entity_args!r}"

    assert "freshness-audit" in prompts, "freshness-audit prompt not registered"
    freshness_args = {a.name: a.required for a in (prompts["freshness-audit"].arguments or [])}
    assert freshness_args == {"connector_id": False}, (
        f"unexpected freshness-audit args: {freshness_args!r}"
    )


# ========================================================================================
# entity-workup — rendering, step order, leads-not-verdicts framing, F-5 reference.
# ========================================================================================
def test_entity_workup_renders_all_five_tools_and_id() -> None:
    server = build_server(neo4j_client=_NoTouchFake())
    text = _prompt_text(_get_prompt(server, "entity-workup", {"entity_id": "Q42"}))

    assert "Q42" in text, "the validated entity_id must appear verbatim in the rendered text"
    for tool_name in _FIVE_TOOLS_IN_ORDER:
        assert tool_name in text, f"{tool_name} missing from the entity-workup playbook text"

    # Step order is load-bearing: each tool name's first occurrence must appear in the SAME
    # order as the spec's 5-step sequence (a builder who lists the five tools but scrambles
    # the order must fail this).
    positions = [text.index(name) for name in _FIVE_TOOLS_IN_ORDER]
    assert positions == sorted(positions), f"tool step order scrambled: {positions}"

    # Leads-not-verdicts framing (verbatim clause from the Framing paragraph, spec §4.1).
    assert "do not merge, attribute, or label a person from this workup." in text
    assert "never an automated verdict" in text

    # F-5 reference (spec §4.1 Step 3 — the entity-workup playbook names the summary=true
    # flag on get_neighbors).
    assert "summary=true" in text


def test_entity_workup_text_is_verbatim_template() -> None:
    """Contract pin: the rendered text is byte-identical to the spec's §4.1 template."""
    server = build_server(neo4j_client=_NoTouchFake())
    text = _prompt_text(_get_prompt(server, "entity-workup", {"entity_id": "Q42"}))
    assert text == _ENTITY_WORKUP_TEMPLATE.format(entity_id="Q42")


def test_entity_workup_braces_render_as_literal_text() -> None:
    """The doubled-brace escape `{{count, sample}}` must render as the literal text
    `{count, sample}` — never leak the doubled form, and never leave any OTHER stray
    brace (the only real substitution field is `{entity_id}`)."""
    server = build_server(neo4j_client=_NoTouchFake())
    text = _prompt_text(_get_prompt(server, "entity-workup", {"entity_id": "Q42"}))

    assert "{count, sample}" in text, "the {{...}} escape did not render to literal text"
    assert "{{count, sample}}" not in text, "the doubled-brace escape leaked verbatim"
    # Exactly one literal '{' and '}' survive .format(): the {count, sample} phrase (every
    # {entity_id} field has been substituted away by this point).
    assert text.count("{") == 1 and text.count("}") == 1, (
        f"expected exactly one literal brace pair (from {{count, sample}}); "
        f"got {{: {text.count('{')}, }}: {text.count('}')}"
    )


# ========================================================================================
# freshness-audit — all-instances vs scoped, six states, REST reference.
# ========================================================================================
def test_freshness_audit_all_and_scoped() -> None:
    server = build_server(neo4j_client=_NoTouchFake())

    all_text = _prompt_text(_get_prompt(server, "freshness-audit", {}))
    assert "Scope: all connector instances." in all_text
    assert "GET /sources/freshness" in all_text
    for state in _SIX_FRESHNESS_STATES:
        assert state in all_text, f"freshness state {state!r} missing from the all-instances text"

    scoped_text = _prompt_text(
        _get_prompt(server, "freshness-audit", {"connector_id": "threatfox"})
    )
    assert "connector_id == threatfox" in scoped_text
    for state in _SIX_FRESHNESS_STATES:
        assert state in scoped_text, f"freshness state {state!r} missing from the scoped text"


def test_freshness_audit_text_is_verbatim_template() -> None:
    """Contract pin: both the all-instances and scoped renderings are byte-identical to
    the spec's §4.2 template with the documented `{scope_line}` computation."""
    server = build_server(neo4j_client=_NoTouchFake())

    all_text = _prompt_text(_get_prompt(server, "freshness-audit", {}))
    expected_all = _FRESHNESS_AUDIT_TEMPLATE.format(scope_line="Scope: all connector instances.")
    assert all_text == expected_all

    scoped_text = _prompt_text(
        _get_prompt(server, "freshness-audit", {"connector_id": "threatfox"})
    )
    expected_scoped = _FRESHNESS_AUDIT_TEMPLATE.format(
        scope_line="Scope: focus on connector instances whose connector_id == threatfox."
    )
    assert scoped_text == expected_scoped


# ========================================================================================
# Hostile args — length cap FIRST, shape SECOND; no reflection of the raw hostile bytes.
# ========================================================================================
def test_prompt_arg_oversize_rejected() -> None:
    """An entity_id of length _PROMPT_ARG_MAX_LEN+1 -> the thin fn raises a ValueError whose
    message json.loads-es to {"error": "argument too long", "hint": <non-empty str>}; the
    raw oversize bytes do NOT appear in the message."""
    from worldmonitor.mcp.server import prompt_entity_workup

    oversize = "Q" * (_PROMPT_ARG_MAX_LEN + 1)
    with pytest.raises(ValueError) as exc:
        prompt_entity_workup(oversize)

    envelope = json.loads(str(exc.value))
    assert set(envelope.keys()) == {"error", "hint"}
    assert envelope["error"] == "argument too long"
    assert isinstance(envelope["hint"], str) and envelope["hint"].strip() != ""
    assert oversize not in str(exc.value), "the raw oversize argument must not be reflected"


def test_prompt_arg_injection_rejected() -> None:
    """An injection-shaped entity_id -> {"error": "invalid argument", "hint": <non-empty>}."""
    from worldmonitor.mcp.server import prompt_entity_workup

    with pytest.raises(ValueError) as exc:
        prompt_entity_workup(_INJECTION_ID)

    envelope = json.loads(str(exc.value))
    assert set(envelope.keys()) == {"error", "hint"}
    assert envelope["error"] == "invalid argument"
    assert isinstance(envelope["hint"], str) and envelope["hint"].strip() != ""


def test_prompt_missing_required_arg() -> None:
    """server.get_prompt("entity-workup", {}) raises the SDK's un-prefixed
    "Missing required arguments: ..." ValueError (no "Error rendering prompt" wrapper —
    the fn body never runs when a required arg is absent, spec §1.1)."""
    server = build_server(neo4j_client=_NoTouchFake())
    with pytest.raises(ValueError, match="Missing required arguments"):
        _get_prompt(server, "entity-workup", {})


def test_freshness_audit_arg_oversize_rejected() -> None:
    """The arg/cap table (spec §5.1) applies identically to freshness-audit's connector_id."""
    from worldmonitor.mcp.server import prompt_freshness_audit

    oversize = "c" * (_PROMPT_ARG_MAX_LEN + 1)
    with pytest.raises(ValueError) as exc:
        prompt_freshness_audit(oversize)

    envelope = json.loads(str(exc.value))
    assert set(envelope.keys()) == {"error", "hint"}
    assert envelope["error"] == "argument too long"
    assert oversize not in str(exc.value)


def test_freshness_audit_arg_injection_rejected() -> None:
    from worldmonitor.mcp.server import prompt_freshness_audit

    with pytest.raises(ValueError) as exc:
        prompt_freshness_audit(_INJECTION_ID)

    envelope = json.loads(str(exc.value))
    assert set(envelope.keys()) == {"error", "hint"}
    assert envelope["error"] == "invalid argument"


def test_freshness_audit_empty_connector_id_is_the_all_instances_sentinel() -> None:
    """connector_id="" is the documented "all instances" sentinel (spec §5.1) — it must be
    ACCEPTED (no error), unlike entity_id's required-non-empty rule."""
    from worldmonitor.mcp.server import prompt_freshness_audit

    text = prompt_freshness_audit("")
    assert "Scope: all connector instances." in text


# ========================================================================================
# Both transports — no drift (INV-S1). Registers via the shared _register_prompts helper
# directly on a freshly built HTTP-flavoured FastMCP (mirrors the existing
# test_http_tools_carry_same_annotations_and_schema_as_stdio idiom in test_mcp_http_auth.py).
# ========================================================================================
def test_http_registers_same_two_prompts_no_drift() -> None:
    from mcp.server.fastmcp import FastMCP

    from worldmonitor.mcp.auth import ZitadelMCPTokenVerifier, build_auth_settings
    from worldmonitor.mcp.server import _register_prompts, _register_read_tools

    class _StubVerifier:
        """Never actually invoked (no request is sent) — only needed to satisfy
        FastMCP(auth=..., token_verifier=...)'s constructor requirement."""

        def verify(self, token: str) -> dict[str, Any]:
            raise AssertionError("verify() must not be called — no request is sent")

    verifier = ZitadelMCPTokenVerifier(_StubVerifier())
    auth_cfg = build_auth_settings(issuer_url="https://issuer.test.example")
    http_server = FastMCP(
        name="worldmonitor-graph-read",
        auth=auth_cfg,
        token_verifier=verifier,
        stateless_http=True,
    )
    _register_read_tools(http_server, _NoTouchFake())
    _register_prompts(http_server)

    stdio_server = build_server(neo4j_client=_NoTouchFake())

    http_names = _prompt_names(http_server)
    stdio_names = _prompt_names(stdio_server)
    assert http_names == stdio_names == _ALL_PROMPT_NAMES, (
        f"HTTP and stdio prompt sets must be identical: http={http_names!r} stdio={stdio_names!r}"
    )
