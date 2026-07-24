"""Unit tests — Gate F-8: MCP live-smoke set-pin lockstep + ``compare()`` behaviour.

Spec: ``docs/reviews/GATE_F8_MCP_LIVE_SMOKE_SPEC.md`` (§3/§7/§8). ADR: ``docs/decisions/
0126-mcp-live-smoke.md``. This gate adds ``src/worldmonitor/mcp/smoke.py``, a package module
(``python -m worldmonitor.mcp.smoke``) that spawns the real ``python -m worldmonitor.mcp``
entrypoint over stdio and asserts the served tool/prompt surface is EXACTLY the registered
set — a drift pin appended to the ``compose-boot`` CI job. This file is the LOCAL, fast,
non-flaky half of that pin (the live compose-boot run against the built image is the other
half; it is not reproduced here).

CONTRACT ASSUMED (the builder MUST match these names/signatures exactly — spec §3/§7.2/§11):
    worldmonitor.mcp.smoke
        EXPECTED_TOOLS: frozenset[str]      # {"get_entity", "get_neighbors", "get_provenance",
                                             #  "find_paths", "get_entity_dossier"} (5 — ADR
                                             #  0063 + 0122)
        EXPECTED_PROMPTS: frozenset[str]    # {"entity-workup", "freshness-audit"} (2 — ADR 0125)
        compare(served_tools, served_prompts) -> tuple[int, str]
            # Pure, no I/O. Exit code 0 iff served_tools == EXPECTED_TOOLS AND
            # served_prompts == EXPECTED_PROMPTS; otherwise non-zero. The returned message is
            # always a non-empty string. On MATCH it is a one-line OK summary carrying NEITHER
            # the "missing=" nor "unexpected=" drift markers (§2 point 4, "On exact match it
            # prints a one-line OK summary"). On ANY mismatch it names, per surface, BOTH
            # directions of the symmetric difference using the exact literal markers the spec
            # gives verbatim (§4): a "tools: missing=<...> unexpected=<...>" segment and a
            # "prompts: missing=<...> unexpected=<...>" segment — "missing" = expected-but-
            # absent, "unexpected" = present-but-unexpected. The exact bracket/quote style
            # around each set's members is NOT pinned here (a Python set repr, a sorted list
            # repr, etc. are all acceptable) — only the literal "tools:"/"prompts:"/"missing="/
            # "unexpected=" tokens and correct membership/bucketing are.
        run_smoke() -> int
            # Spawn `[sys.executable, "-m", "worldmonitor.mcp"]` with
            # env=os.environ | {"MCP_TRANSPORT": "stdio"}, handshake initialize ->
            # notifications/initialized -> tools/list -> prompts/list, collect served name
            # sets, call compare(), bound every read with a deadline, terminate the child in a
            # finally. Spawn-only by design (no injectable transport/process parameter is
            # specified anywhere in the spec) — see the NOTE before the optional integration
            # test below for why this file does not fake-inject the subprocess to unit-test
            # the deadline/terminate behaviour in isolation.

Each test that needs a not-yet-existing symbol imports it LOCALLY (inside the test body),
mirroring the local-import idiom already used in ``tests/unit/test_mcp_server.py`` /
``tests/unit/test_mcp_prompts.py`` for a not-yet-shipped symbol: a missing symbol fails ONLY
that test, never the whole module's collection.

RED today: ``worldmonitor.mcp.smoke`` does not exist at all (no such file under
``src/worldmonitor/mcp/``) — every test below fails at its local
``from worldmonitor.mcp import smoke`` (or ``from worldmonitor.mcp.smoke import run_smoke``)
line with ``ModuleNotFoundError: No module named 'worldmonitor.mcp.smoke'``. That is RED for
the right reason: the module is simply absent, nothing to critique yet about its behaviour.

Named tests (the proof this gate holds — spec §8):
    test_expected_tool_set_matches_live_registration      — the load-bearing lockstep guard.
    test_expected_prompt_set_matches_live_registration    — its prompt-surface twin.
    test_smoke_compare_exact_match_zero                   — match -> 0, no drift markers.
    test_smoke_compare_flags_drift_nonzero                — combined drift -> non-zero, both
                                                             surfaces' sections attributed.
    test_smoke_end_to_end_stdio_exit_zero (optional, @pytest.mark.integration) — real subprocess.
Plus additional granular compare()-direction tests (extra/missing, tools/prompts, and the
simultaneous-both-directions case) that are not separately named in the spec's §8 list but
pin the same contract at finer grain (never asserting merely "no exception").
"""

from __future__ import annotations

import asyncio
import re

import pytest


# ------------------------------------------------------------------------------------------
# Diff-message parsing helpers. Deliberately tolerant of exact bracket/quote/order style
# (spec §4 pins the literal tokens "tools:"/"prompts:"/"missing="/"unexpected=", not a
# specific Python repr) while still proving EXACT membership + correct bucketing.
# ------------------------------------------------------------------------------------------
def _section(message: str, label: str) -> str:
    """Extract the ``<label>: ... missing=... unexpected=...`` segment of a ``compare()``
    diff message (up to the next ``tools:``/``prompts:`` label or end of string), whether
    the two sections are newline-separated or concatenated on one line."""
    pattern = re.compile(rf"{label}:\s*missing=.*?unexpected=.*?(?=tools:|prompts:|\Z)", re.DOTALL)
    match = pattern.search(message)
    assert match is not None, (
        f"diff message has no {label!r} section (expected a 'missing=' ... 'unexpected=' "
        f"pair following a {label!r}: label); full message: {message!r}"
    )
    return match.group(0)


def _halves(section: str) -> tuple[str, str]:
    """Split a ``missing=... unexpected=...`` section into its (missing, unexpected) halves."""
    before_unexpected, sep, unexpected_half = section.partition("unexpected=")
    assert sep, f"section carries no 'unexpected=' marker: {section!r}"
    _prefix, sep2, missing_half = before_unexpected.partition("missing=")
    assert sep2, f"section carries no 'missing=' marker: {section!r}"
    return missing_half, unexpected_half


# ========================================================================================
# THE LOCKSTEP GUARD (the load-bearing test, spec §7.1 / §10) — smoke.EXPECTED_TOOLS /
# smoke.EXPECTED_PROMPTS must equal the REAL live registration from build_server(), driven
# exactly the way test_mcp_server.py / test_mcp_prompts.py already do (async list_tools() /
# list_prompts() on a built server). This is what makes the constants impossible to rot: a
# legitimate future 6th tool / 3rd prompt registered in server.py fails THIS test (in the
# fast unit suite) until smoke.py's constants are updated too.
# ========================================================================================
def test_expected_tool_set_matches_live_registration() -> None:
    from worldmonitor.mcp import smoke
    from worldmonitor.mcp.server import build_server

    server = build_server()
    live_names = {t.name for t in asyncio.run(server.list_tools())}

    # Sanity-anchor: today's ACTUAL live registration is exactly the documented 5-tool set
    # (ADR 0063 + 0122). If this fails, the lockstep comparison below would be meaningless
    # (comparing smoke's pin against an already-wrong live surface).
    assert live_names == {
        "get_entity",
        "get_neighbors",
        "get_provenance",
        "find_paths",
        "get_entity_dossier",
    }, f"today's live tool registration is not the expected 5-tool set: {live_names!r}"

    assert live_names == smoke.EXPECTED_TOOLS, (
        f"smoke.EXPECTED_TOOLS has drifted from the live build_server().list_tools() "
        f"registration: pinned={set(smoke.EXPECTED_TOOLS)!r} live={live_names!r}"
    )


def test_expected_prompt_set_matches_live_registration() -> None:
    from worldmonitor.mcp import smoke
    from worldmonitor.mcp.server import build_server

    server = build_server()
    live_names = {p.name for p in asyncio.run(server.list_prompts())}

    assert live_names == {"entity-workup", "freshness-audit"}, (
        f"today's live prompt registration is not the expected 2-prompt set: {live_names!r}"
    )

    assert live_names == smoke.EXPECTED_PROMPTS, (
        f"smoke.EXPECTED_PROMPTS has drifted from the live build_server().list_prompts() "
        f"registration: pinned={set(smoke.EXPECTED_PROMPTS)!r} live={live_names!r}"
    )


def test_expected_constants_are_frozensets_of_the_right_cardinality() -> None:
    """Cheap type/shape sanity on the two pinned constants (spec §3): immutable frozensets
    with today's documented cardinality — 5 tools, 2 prompts."""
    from worldmonitor.mcp import smoke

    assert isinstance(smoke.EXPECTED_TOOLS, frozenset), (
        f"EXPECTED_TOOLS must be a frozenset (immutable pin); got {type(smoke.EXPECTED_TOOLS)}"
    )
    assert len(smoke.EXPECTED_TOOLS) == 5, smoke.EXPECTED_TOOLS
    assert isinstance(smoke.EXPECTED_PROMPTS, frozenset), (
        f"EXPECTED_PROMPTS must be a frozenset (immutable pin); got {type(smoke.EXPECTED_PROMPTS)}"
    )
    assert len(smoke.EXPECTED_PROMPTS) == 2, smoke.EXPECTED_PROMPTS


# ========================================================================================
# compare() — exact match -> 0, no drift markers (spec §7.2 / §8 test_smoke_compare_exact_
# match_zero).
# ========================================================================================
def test_smoke_compare_exact_match_zero() -> None:
    from worldmonitor.mcp import smoke

    code, message = smoke.compare(set(smoke.EXPECTED_TOOLS), set(smoke.EXPECTED_PROMPTS))
    assert code == 0, f"an exact-match surface must exit 0; got {code} (message={message!r})"
    assert isinstance(message, str) and message.strip() != "", (
        "compare() must still return a non-empty one-line summary on an exact match"
    )
    assert "missing=" not in message and "unexpected=" not in message, (
        f"an exact-match message must carry NO drift markers (it is a one-line OK summary, "
        f"not a diff); got {message!r}"
    )


# ========================================================================================
# compare() — single-direction drift, one surface at a time (finer grain than the spec's
# named test list, per the task's explicit ask: extra tool / missing tool / extra prompt /
# missing prompt, each asserting the diff names the SPECIFIC drifted member on the correct
# side).
# ========================================================================================
def test_smoke_compare_extra_tool_flagged_nonzero_with_diff() -> None:
    from worldmonitor.mcp import smoke

    served_tools = set(smoke.EXPECTED_TOOLS) | {"raw_cypher_escape_hatch"}
    code, message = smoke.compare(served_tools, set(smoke.EXPECTED_PROMPTS))
    assert code != 0, "an extra (unregistered) tool must exit non-zero"

    tools_section = _section(message, "tools")
    missing, unexpected = _halves(tools_section)
    assert "raw_cypher_escape_hatch" in unexpected, (
        f"the extra tool must be named on the 'unexpected' (present-but-unexpected) side: "
        f"{tools_section!r}"
    )
    assert "raw_cypher_escape_hatch" not in missing, (
        f"the extra tool must NOT also appear on the 'missing' side: {tools_section!r}"
    )


def test_smoke_compare_missing_tool_flagged_nonzero_with_diff() -> None:
    from worldmonitor.mcp import smoke

    served_tools = set(smoke.EXPECTED_TOOLS) - {"get_provenance"}
    code, message = smoke.compare(served_tools, set(smoke.EXPECTED_PROMPTS))
    assert code != 0, "a missing (dropped) tool must exit non-zero"

    tools_section = _section(message, "tools")
    missing, unexpected = _halves(tools_section)
    assert "get_provenance" in missing, (
        f"the missing tool must be named on the 'missing' (expected-but-absent) side: "
        f"{tools_section!r}"
    )
    assert "get_provenance" not in unexpected, (
        f"the missing tool must NOT also appear on the 'unexpected' side: {tools_section!r}"
    )


def test_smoke_compare_extra_prompt_flagged_nonzero_with_diff() -> None:
    from worldmonitor.mcp import smoke

    served_prompts = set(smoke.EXPECTED_PROMPTS) | {"raw-shell-prompt"}
    code, message = smoke.compare(set(smoke.EXPECTED_TOOLS), served_prompts)
    assert code != 0, "an extra (unregistered) prompt must exit non-zero"

    prompts_section = _section(message, "prompts")
    missing, unexpected = _halves(prompts_section)
    assert "raw-shell-prompt" in unexpected, (
        f"the extra prompt must be named on the 'unexpected' side: {prompts_section!r}"
    )
    assert "raw-shell-prompt" not in missing, (
        f"the extra prompt must NOT also appear on the 'missing' side: {prompts_section!r}"
    )


def test_smoke_compare_missing_prompt_flagged_nonzero_with_diff() -> None:
    from worldmonitor.mcp import smoke

    served_prompts = set(smoke.EXPECTED_PROMPTS) - {"freshness-audit"}
    code, message = smoke.compare(set(smoke.EXPECTED_TOOLS), served_prompts)
    assert code != 0, "a missing (dropped) prompt must exit non-zero"

    prompts_section = _section(message, "prompts")
    missing, unexpected = _halves(prompts_section)
    assert "freshness-audit" in missing, (
        f"the missing prompt must be named on the 'missing' side: {prompts_section!r}"
    )
    assert "freshness-audit" not in unexpected, (
        f"the missing prompt must NOT also appear on the 'unexpected' side: {prompts_section!r}"
    )


# ========================================================================================
# compare() — BOTH directions named simultaneously on the SAME surface (a "renamed" tool:
# one dropped, a different one added) — the explicit ask: "the diff output must name BOTH
# directions (expected-but-absent / present-but-unexpected)".
# ========================================================================================
def test_smoke_compare_diff_names_both_directions_simultaneously() -> None:
    from worldmonitor.mcp import smoke

    served_tools = (set(smoke.EXPECTED_TOOLS) - {"find_paths"}) | {"query_graph_raw"}
    code, message = smoke.compare(served_tools, set(smoke.EXPECTED_PROMPTS))
    assert code != 0, "a renamed tool (drop + add) must exit non-zero"

    tools_section = _section(message, "tools")
    missing, unexpected = _halves(tools_section)
    assert "find_paths" in missing, f"dropped tool must be on the missing side: {tools_section!r}"
    assert "find_paths" not in unexpected, (
        f"dropped tool must NOT also appear on the unexpected side: {tools_section!r}"
    )
    assert "query_graph_raw" in unexpected, (
        f"added tool must be on the unexpected side: {tools_section!r}"
    )
    assert "query_graph_raw" not in missing, (
        f"added tool must NOT also appear on the missing side: {tools_section!r}"
    )


# ========================================================================================
# compare() — named per spec §8: test_smoke_compare_flags_drift_nonzero. A COMBINED drift
# across BOTH surfaces at once (an extra tool AND a missing prompt) still yields a single
# non-zero exit code, with EACH surface's section independently present and correctly
# attributed (proves compare() truly evaluates tools and prompts separately, not just
# "any name differs anywhere").
# ========================================================================================
def test_smoke_compare_flags_drift_nonzero() -> None:
    from worldmonitor.mcp import smoke

    served_tools = set(smoke.EXPECTED_TOOLS) | {"raw_cypher_escape_hatch"}
    served_prompts = set(smoke.EXPECTED_PROMPTS) - {"entity-workup"}
    code, message = smoke.compare(served_tools, served_prompts)
    assert code != 0, "combined tool+prompt drift must exit non-zero"

    tools_section = _section(message, "tools")
    t_missing, t_unexpected = _halves(tools_section)
    assert "raw_cypher_escape_hatch" in t_unexpected, tools_section
    assert "raw_cypher_escape_hatch" not in t_missing, tools_section

    prompts_section = _section(message, "prompts")
    p_missing, p_unexpected = _halves(prompts_section)
    assert "entity-workup" in p_missing, prompts_section
    assert "entity-workup" not in p_unexpected, prompts_section


# ========================================================================================
# run_smoke() — spawn-only by design; NO unit-level fake-injection test here.
#
# The spec's §11 slice breakdown gives run_smoke() a fixed shape: "spawn `python -m
# worldmonitor.mcp` with MCP_TRANSPORT=stdio, handshake ... collect sets, call compare(),
# bounded read deadline + finally terminate" — a zero-argument function with no documented
# injectable transport/process/clock parameter anywhere in the spec (§2, §11, §12). Faking
# subprocess.Popen via monkeypatch to unit-test the deadline/finally-terminate behaviour in
# isolation would mean asserting against an INVENTED signature the spec never commits to,
# which is exactly the kind of test a builder could "pass" by restructuring run_smoke() to
# dodge — the opposite of a load-bearing oracle. So per the task's own guidance ("if the
# design is spawn-only, limit unit coverage to the compare function + constants and note
# it — do NOT write a flaky subprocess unit test"), run_smoke()'s deadline/terminate
# behaviour is NOT unit-tested here; it is covered by:
#   (a) the compare()-function tests above (the decision logic run_smoke() delegates to),
#   (b) the optional real end-to-end test below (proves the happy path actually completes
#       well inside the deadline), and
#   (c) the compose-boot job itself (the spec's own designated "reality check", §7).
# A hung-child / timeout scenario is exercised only by (c) in CI, never faked here.
# ========================================================================================


# ========================================================================================
# OPTIONAL end-to-end (spec §7.3 / §8): a REAL run_smoke() invocation, no Docker/
# testcontainer. Verified empirically (independent of this gate's own unimplemented module)
# that `python -m worldmonitor.mcp` under a forced MCP_TRANSPORT=stdio env, with NO
# NEO4J_*/ENVIRONMENT vars set at all, answers a full initialize -> tools/list -> prompts/list
# handshake and exits cleanly in well under 1 second, repeatably across 5 runs (Settings()
# defaults to environment="development" and a LAZY Neo4j driver — spec §1.4 — so no store
# needs to be reachable). run_smoke()'s own spawn+handshake dance mirrors the ALREADY-green
# subprocess idiom `tests/integration/test_mcp_stdio.py` uses today. Included as non-flaky.
# ========================================================================================
@pytest.mark.integration
def test_smoke_end_to_end_stdio_exit_zero() -> None:
    from worldmonitor.mcp.smoke import run_smoke

    exit_code = run_smoke()
    assert exit_code == 0, (
        "run_smoke() must exit 0 against this tree's real (5-tool, 2-prompt) MCP surface "
        "served over a freshly spawned `python -m worldmonitor.mcp` stdio child"
    )
