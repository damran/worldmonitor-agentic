"""Property: durable canonical-id minting is INJECTIVE + the anchor-conflict guard fails closed.

``resolution.canonical._anchor_id`` is the catastrophic-merge-PREVENTION primitive: distinct raw
``(kind, value)`` pairs MUST mint distinct durable ids. A non-injective id is a SILENT cross-entity
merge — two real-world entities collapse onto one durable id (one node) and the catastrophic-merge
guard never sees it, because no cluster ever formed. So injectivity here is a person-safety
not a nicety.

``pick_anchor`` must (a) never pick an arbitrary ``[0]`` winner from a CONFLICTING anchor tier
(ADR 0040 — deriving a durable id is never a back-door fusion of two identities), (b) FALL THROUGH
to the next non-conflicting tier when a higher tier conflicts, and (c) be deterministic under member
permutation.
"""

from __future__ import annotations

import strategies as wm
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.resolution.canonical import _anchor_id, pick_anchor

_SETTINGS = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])

# A fixed, valid 20-char alphanumeric LEI shape (ISO 17442) for the fall-through tier tests.
_LEI = "5493001KJTIIGC8Y1R12"


@given(values=st.lists(wm.anchor_value(), min_size=2, max_size=8, unique=True))
@_SETTINGS
def test_anchor_id_injective_within_kind(values: list[str]) -> None:
    """Within ONE kind, distinct raw values never collide onto the same durable id (ADR 0048).

    ``values`` is ``unique=True`` over the RAW strings, so any two ids that coincide are a genuine
    non-injectivity — the silent cross-entity merge ADR 0048 §3.2 forbids.
    """
    for kind in wm.ANCHOR_KINDS:
        ids = {value: _anchor_id(kind, value) for value in values}
        # Invert: every distinct raw value must map to a distinct id.
        seen: dict[str, str] = {}
        for value, anchor_id in ids.items():
            clash = seen.get(anchor_id)
            assert clash is None, (
                f"_anchor_id COLLISION ({kind}): {value!r} and {clash!r} both -> {anchor_id!r}"
            )
            seen[anchor_id] = value
        assert len(set(ids.values())) == len(values)


@given(value=wm.anchor_value())
@_SETTINGS
def test_anchor_id_is_ftm_clean_fixed_point(value: str) -> None:
    """Every minted id is an FtM entity-reference fixed point, so it survives as a rewritten edge
    ENDPOINT (the colon form cleaned to ``None`` and silently dropped the edge — ADR 0048)."""
    from followthemoney import registry

    for kind in wm.ANCHOR_KINDS:
        anchor_id = _anchor_id(kind, value)
        assert registry.entity.clean(anchor_id) == anchor_id, (
            f"_anchor_id({kind!r}, {value!r}) -> {anchor_id!r} is NOT an FtM clean fixed point"
        )


@given(value=wm.anchor_value())
@_SETTINGS
def test_anchor_id_deterministic(value: str) -> None:
    """Pure in ``(kind, value)`` — re-ingest stability falls straight out (ADR 0036/0048)."""
    for kind in wm.ANCHOR_KINDS:
        assert _anchor_id(kind, value) == _anchor_id(kind, value)


@given(
    qid_a=st.text(alphabet="0123456789", min_size=2, max_size=6),
    qid_b=st.text(alphabet="0123456789", min_size=2, max_size=6),
)
@_SETTINGS
def test_pick_anchor_falls_through_on_conflicting_tier(qid_a: str, qid_b: str) -> None:
    """Two members carrying DISTINCT QIDs => the QID tier is in conflict and pick_anchor must NOT
    derive a durable id from it (ADR 0040). With no lower tier present it returns None — never an
    arbitrary ``[0]`` winner (which would be a silent fusion of two real identities)."""
    if qid_a == qid_b:
        return  # equal QIDs are not a conflict; covered by the agreement path below
    left = make_qid_entity("a", f"Q{qid_a}")
    right = make_qid_entity("b", f"Q{qid_b}")
    result = pick_anchor([left, right])
    assert result is None, (
        f"pick_anchor must fall through on a conflicting QID tier (Q{qid_a} vs Q{qid_b}), "
        f"got {result!r} — a back-door fusion of two identities (ADR 0040)"
    )


@given(
    qid_a=st.text(alphabet="0123456789", min_size=2, max_size=6),
    qid_b=st.text(alphabet="0123456789", min_size=2, max_size=6),
)
@_SETTINGS
def test_pick_anchor_falls_through_to_lower_agreeing_tier(qid_a: str, qid_b: str) -> None:
    """When the QID tier CONFLICTS but the LEI tier AGREES, pick_anchor falls THROUGH to the LEI
    anchor (it must not return None and abandon a perfectly good lower-tier agreement)."""
    if qid_a == qid_b:
        return
    left = make_qid_lei_entity("a", f"Q{qid_a}", _LEI)
    right = make_qid_lei_entity("b", f"Q{qid_b}", _LEI)
    result = pick_anchor([left, right])
    assert result == _anchor_id("lei", _LEI), (
        f"pick_anchor should fall through the conflicting QID tier to the agreeing LEI tier, "
        f"got {result!r}"
    )
    assert pick_anchor([right, left]) == result  # permutation-invariant


@given(qid=st.text(alphabet="0123456789", min_size=2, max_size=6))
@_SETTINGS
def test_pick_anchor_agreement_is_permutation_invariant(qid: str) -> None:
    """When members AGREE on the QID, pick_anchor returns that id and is order-independent."""
    members = [make_qid_entity(eid, f"Q{qid}") for eid in ("a", "b", "c")]
    forward = pick_anchor(members)
    backward = pick_anchor(list(reversed(members)))
    assert forward == backward
    assert forward == _anchor_id("qid", f"Q{qid}")


def make_qid_entity(entity_id: str, qid: str):  # noqa: ANN201 - returns FtmEntity
    """A Company carrying a wikidataId (the top durable-precedence tier)."""
    from worldmonitor.ontology.ftm import make_entity

    return make_entity(
        {"id": entity_id, "schema": "Company", "properties": {"name": ["X"], "wikidataId": [qid]}}
    )


def make_qid_lei_entity(entity_id: str, qid: str, lei: str):  # noqa: ANN201 - returns FtmEntity
    """A Company carrying both a wikidataId (QID tier) and a leiCode (LEI tier)."""
    from worldmonitor.ontology.ftm import make_entity

    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": ["X"], "wikidataId": [qid], "leiCode": [lei]},
        }
    )
