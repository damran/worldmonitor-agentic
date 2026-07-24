"""Property: MCP prompt arguments — Gate F-4 (ADR 0125 D6, recorded decision §3.1).

F-4 touches no CLAUDE.md invariant (declarative, read-only text — spec §3.1), so the
mandatory-``@given`` build-discipline rule does not apply. This is the ONE cheap property
test the gate nonetheless adds (mirroring ADR 0121/0124's decision): the load-bearing
guarantee — *a hostile argument never crashes, never corrupts stdout, and is accepted iff
it is under the length cap and shape-valid* — is exactly the class an example test
under-samples.

Reuses the ``configure_stderr_logging`` + ``capfd`` stdout-purity idiom from
``test_prop_mcp_stdout_purity.py``. The two thin prompt functions
(``prompt_entity_workup`` / ``prompt_freshness_audit``) are imported LOCALLY inside each
``@given`` body (fail-soft: a missing symbol fails only that test, never module collection —
``worldmonitor.mcp.server`` itself already exists).

RED today: ``prompt_entity_workup`` / ``prompt_freshness_audit`` do not exist on
``worldmonitor.mcp.server`` -> every example raises ``ImportError`` on first execution.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.graph.read_guards import validate_entity_id
from worldmonitor.mcp.server import configure_stderr_logging

_SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

# Contract-pinned cap (spec §5.1) — this test's own oracle, NOT imported from prod.
_PROMPT_ARG_MAX_LEN = 256

# A distinctive marker prefixed onto every hostile/arbitrary-text example. It contains `~`,
# which is OUTSIDE the ID_PATTERN alphabet ([A-Za-z0-9:._-]), so any string carrying it is
# GUARANTEED shape-invalid regardless of what follows — and it is astronomically unlikely to
# appear inside any static {"error", "hint"} envelope the builder writes. This makes the
# "hostile bytes are not reflected in the error message" assertion MEANINGFUL (it can only
# pass by luck if the implementation genuinely never echoes the argument), mirroring the
# `~`-prefix idiom already used in tests/property/test_mcp_auth_boundary.py.
_HOSTILE_MARKER = "~WM-HOSTILE~"

_CANONICAL = st.sampled_from(
    ["Q42", "opensanctions:abc-1", "geonames:123", "iso-3166:US", "lei:5493001KJTIIGC8Y1R12"]
)
_HOSTILE_SHAPES = st.one_of(
    st.sampled_from(
        ['") DETACH DELETE n //', "a{b}", "a$b", "a\nMATCH", "p1' OR '1'='1", " ", "\t\n"]
    ),
    st.text(max_size=40),
).map(lambda t: f"{_HOSTILE_MARKER}{t}")
_OVERCAP = st.integers(min_value=257, max_value=2000).map(lambda n: "Q" * n)

# entity_id: never empty by construction of the above three strategies (validate_entity_id
# would reject "" anyway, since ID_PATTERN's `+` requires >=1 char).
_ENTITY_IDS = st.one_of(_CANONICAL, _HOSTILE_SHAPES, _OVERCAP)
# connector_id: same space, PLUS the empty-string "all instances" sentinel.
_CONNECTOR_IDS = st.one_of(_ENTITY_IDS, st.just(""))


def _should_accept(s: str, *, allow_empty: bool) -> bool:
    """The oracle predicate: accept iff (allow_empty and empty) or (under cap and shape-valid).

    Length is checked FIRST (spec §5.1 "validation order is load-bearing"): an over-cap AND
    shape-invalid string must be reported as "argument too long", never "invalid argument".
    """
    if allow_empty and s == "":
        return True
    return len(s) <= _PROMPT_ARG_MAX_LEN and validate_entity_id(s)


def _expected_reject_token(s: str) -> str:
    if len(s) > _PROMPT_ARG_MAX_LEN:
        return "argument too long"
    return "invalid argument"


@given(entity_id=_ENTITY_IDS)
@_SETTINGS
def test_entity_workup_arg_accept_iff_valid_never_crashes_stdout_pure(
    capfd: pytest.CaptureFixture[str], entity_id: str
) -> None:
    from worldmonitor.mcp.server import prompt_entity_workup

    configure_stderr_logging()
    should_accept = _should_accept(entity_id, allow_empty=False)

    text: str | None = None
    raised: ValueError | None = None
    try:
        text = prompt_entity_workup(entity_id)
    except ValueError as exc:
        raised = exc

    captured = capfd.readouterr()
    assert captured.out == "", f"stdout must stay empty; leaked: {captured.out!r}"

    if should_accept:
        assert raised is None, (
            f"a valid, under-cap entity_id must not raise; id={entity_id!r} raised {raised!r}"
        )
        assert isinstance(text, str) and text != "", f"expected non-empty text; got {text!r}"
        assert entity_id in text, (
            f"accepted entity_id {entity_id!r} must appear verbatim in the rendered text"
        )
    else:
        assert raised is not None, (
            f"entity_id {entity_id!r} fails the cap/shape check but the fn did not raise"
        )
        envelope = json.loads(str(raised))
        assert set(envelope.keys()) == {"error", "hint"}
        assert envelope["error"] == _expected_reject_token(entity_id)
        assert isinstance(envelope["hint"], str) and envelope["hint"].strip() != ""
        assert entity_id not in str(raised), (
            f"the raw hostile/oversize entity_id must not be reflected: {raised!r}"
        )


@given(connector_id=_CONNECTOR_IDS)
@_SETTINGS
def test_freshness_audit_arg_accept_iff_valid_or_empty_never_crashes_stdout_pure(
    capfd: pytest.CaptureFixture[str], connector_id: str
) -> None:
    from worldmonitor.mcp.server import prompt_freshness_audit

    configure_stderr_logging()
    should_accept = _should_accept(connector_id, allow_empty=True)

    text: str | None = None
    raised: ValueError | None = None
    try:
        text = prompt_freshness_audit(connector_id)
    except ValueError as exc:
        raised = exc

    captured = capfd.readouterr()
    assert captured.out == "", f"stdout must stay empty; leaked: {captured.out!r}"

    if should_accept:
        assert raised is None, (
            f"a valid/empty connector_id must not raise; id={connector_id!r} raised {raised!r}"
        )
        assert isinstance(text, str) and text != ""
        if connector_id == "":
            assert "Scope: all connector instances." in text
        else:
            assert connector_id in text, (
                f"accepted connector_id {connector_id!r} must appear verbatim in the text"
            )
    else:
        assert raised is not None, (
            f"connector_id {connector_id!r} fails the cap/shape check but the fn did not raise"
        )
        envelope = json.loads(str(raised))
        assert set(envelope.keys()) == {"error", "hint"}
        assert envelope["error"] == _expected_reject_token(connector_id)
        assert isinstance(envelope["hint"], str) and envelope["hint"].strip() != ""
        assert connector_id not in str(raised), (
            f"the raw hostile/oversize connector_id must not be reflected: {raised!r}"
        )
