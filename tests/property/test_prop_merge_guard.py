"""Property: ``guard.sensitivity.is_sensitive`` FAILS CLOSED (deny-by-default, ADR 0047).

The whole point of the inversion (ADR 0047) is that a sensitive entity is NEVER waved through. A
fail-OPEN here lets an individual-affecting merge auto-promote without the human sign-off the
catastrophic-merge guard exists to force. These properties assert the three sensitive classes are
ALWAYS flagged with INDEPENDENT oracles (the known RISKS set, a synthesised sub-code, a synthesised
off-ontology code) so the test can't be passed by weakening it to mirror the implementation.
"""

from __future__ import annotations

import strategies as wm
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.guard.sensitivity import is_sensitive
from worldmonitor.ontology.ftm import make_entity

_SETTINGS = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])


def _entity_with_topics(topics: list[str]):  # noqa: ANN202 - FtmEntity
    return make_entity(
        {"id": "e", "schema": "Company", "properties": {"name": ["X"], "topics": topics}}
    )


@given(
    code=st.sampled_from(wm.RISK_TOPICS),
    extra=st.lists(st.sampled_from(wm.BENIGN_TOPICS), max_size=3),
)
@_SETTINGS
def test_any_risk_topic_is_sensitive(code: str, extra: list[str]) -> None:
    """Every FtM ``registry.topic.RISKS`` code flags sensitive, even mixed with benign topics."""
    assert is_sensitive(_entity_with_topics([*extra, code])) is True, (
        f"FAIL OPEN: risk topic {code!r} was NOT flagged sensitive"
    )


@given(
    risk=st.sampled_from(wm.RISK_TOPICS),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=6),
)
@_SETTINGS
def test_dot_subcode_of_risk_is_sensitive(risk: str, suffix: str) -> None:
    """A dot-SUBCODE of a RISKS ancestor inherits its risk (e.g. ``role.pep.natl`` -> ``role.pep``).
    Synthesised independently of the implementation's ancestor walk."""
    subcode = f"{risk}.{suffix}"
    assert is_sensitive(_entity_with_topics([subcode])) is True, (
        f"FAIL OPEN: sub-code {subcode!r} of risk ancestor {risk!r} was NOT flagged sensitive"
    )


@given(token=st.text(alphabet="abcdefghijklmnopqrstuvwxyz._", min_size=3, max_size=12))
@_SETTINGS
def test_off_ontology_topic_is_sensitive(token: str) -> None:
    """Unknown ⇒ sensitive (the inversion hinge). The ``wm-unknown-`` sentinel prefix guarantees the
    code is not in ``registry.topic.names`` and is not a RISKS sub-code, so the ONLY correct verdict
    is sensitive."""
    code = f"wm-unknown-{token}"
    assert is_sensitive(_entity_with_topics([code])) is True, (
        f"FAIL OPEN: off-ontology topic {code!r} was NOT flagged sensitive (deny-by-default)"
    )


@given(codes=st.lists(st.sampled_from(wm.BENIGN_TOPICS), min_size=1, max_size=4, unique=True))
@_SETTINGS
def test_benign_known_topics_are_not_sensitive(codes: list[str]) -> None:
    """Correctness in the OTHER direction: a known, non-risk, no-risk-ancestor topic is benign.
    (Over-flagging benign topics would defeat the gate; this pins the boundary.)"""
    assert is_sensitive(_entity_with_topics(codes)) is False, (
        f"benign known topics {codes!r} were wrongly flagged sensitive"
    )
