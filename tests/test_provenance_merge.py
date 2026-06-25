"""Gate C — Value-Level Provenance: the PRIMARY failing-first invariant test.

> BUILD gate (`gate/c-value-provenance`, off `master@a28de24`). Spec:
> ``docs/reviews/GATE_C_VALUE_PROVENANCE_SPEC.md`` (§4 StatementEntity fusion, §5 tier-1 witness
> map, §7 APPROVE/DENY, §9 value-set-invariance fence, §11 tests, §13 adversarial).
> Verify-before-code artefact: ``VERIFIED_API.md`` "Gate C — followthemoney StatementEntity".

THE BUG THIS GATE EXISTS TO KILL (spec §0, DENY D-COLLAPSE)
----------------------------------------------------------
``resolution/merge.py:_merge_entities`` seeds a merged entity from ``member_ids[0]`` then folds the
rest in with ``ValueEntity.merge``. ``ValueEntity.merge`` unions VALUES but binds NO lineage.
``provenance/model.py`` compounds the loss: ``stamp`` writes ``wm_prov_*`` context **lists** (so the
raw context already holds every source's ``source_id``), but ``get_provenance`` /
``provenance_node_properties`` read only ``[0]`` — so a merged node carries exactly ONE projected
lineage, ``source[0]``'s. A 3-source merge keeps one lineage and silently drops two.

THE MULTI-SOURCE / TIER-1-WITNESS API THIS TEST DEFINES (the builder conforms verbatim)
---------------------------------------------------------------------------------------
This test is BOTH the oracle the builder must satisfy AND the contract that names the API the
builder implements. Per spec §3 ("a per-property witness-set view derived from the fused
``StatementEntity`` (Tier-1 ``prop_sources``)") and §5 (Tier-1 per-property witness map), the
builder MUST expose, in ``worldmonitor.provenance.model``::

    def witness_map(entity: FtmEntity) -> dict[str, set[str]]:
        '''Tier-1 per-property witness sets for a (possibly fused) entity.

        Returns, for each FtM property name that carries at least one value on ``entity``, the SET
        of datasets (= each contributing member's ``Provenance.source_id``) that witnessed ANY value
        of that property. Derived from the fused ``StatementEntity``'s per-(prop, value, dataset)
        statements (spec §4/§5). A singleton / single-source entity yields singleton sets. The "id"
        pseudo-property is NOT included.
        '''

and ``resolution/merge.py:_merge_entities`` MUST fuse the cluster with ``StatementEntity`` (each
member's statements stamped with that member's ``Provenance`` as ``dataset=source_id``) under the
canonical id, so that ``witness_map`` over the FUSED entity reflects ALL contributing sources —
while keeping the existing ``(merged, dropped)`` return contract byte-for-byte (H-2, ADR 0041) and
changing NO value (the §9 fence). The single-source ``Provenance`` / ``stamp`` / ``get_provenance``
/ ``provenance_node_properties`` surface is KEPT (G1's ``prov_*`` stays — additive, A3).

WHY THIS FAILS NOW (red for the RIGHT reason)
---------------------------------------------
``witness_map`` does not exist on the current tree (``ImportError``), and the fusion path keeps only
``source[0]``'s lineage. Both case 1 (3-source corroboration) and case 3 (adversarial single-source
value) fail pre-fix; the §9 value-set-invariance fence (case 2) PASSES pre- AND post-fix — it is the
guardrail that proves Gate C is lineage-only / person-NEUTRAL and must never go red.

PLACEMENT / RUNTIME
-------------------
Repo-root, NO ``@pytest.mark.integration`` — the fusion + multi-source provenance read are
pure-Python (FtM ``StatementEntity`` is in-process, no DB), so this runs in the ``quality`` job
under ``pytest -m "not integration"``. The graph Tier-1 projection (the witness map on the Neo4j
node) is a separate ``tests/integration/test_value_provenance_graph.py`` — NOT asserted here.
"""

from __future__ import annotations

import importlib

import pytest

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.merge import _merge_entities

# The three distinct source datasets asserting the SAME real-world person (spec §11 / §13).
SRC_A = "src-A"
SRC_B = "src-B"
SRC_C = "src-C"

# The shared (corroborated) property values every source agrees on. These props end up witnessed by
# ALL THREE datasets once the StatementEntity fusion lands.
_SHARED_PROPS: dict[str, list[str]] = {
    "name": ["Vladimir Example"],
    "nationality": ["ru"],
    "birthDate": ["1960-01-01"],
}
# The adversarial single-source value (spec §13): a passport number ONLY src-C asserts. Its witness
# set must be exactly {src-C} — NOT {src-A} (the source[0] collapse bug), NOT {src-A,src-B,src-C}.
_ADVERSARIAL_PROP = "passportNumber"
_ADVERSARIAL_VALUE = "P-XYZ-99204"

_CANONICAL_ID = "wmc-merged-person"
_MEMBER_IDS: tuple[str, ...] = ("e-a", "e-b", "e-c")


def _source_entity(
    entity_id: str, source_id: str, *, extra: dict[str, list[str]] | None = None
) -> FtmEntity:
    """A single-source Person entity stamped with its own (single-source) Provenance.

    A SOURCE entity legitimately has exactly one source, so ``get_provenance``
    (single-source) is the correct per-MEMBER read; the multi-source faithfulness is a
    property of the FUSED entity only.
    """
    props: dict[str, list[str]] = {key: list(values) for key, values in _SHARED_PROPS.items()}
    if extra:
        props.update(extra)
    entity = make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": [source_id]}
    )
    stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at=f"2026-06-2{entity_id[-1]}T00:00:00Z",
            reliability="A",
            source_record=f"s3://landing/{source_id}/{entity_id}.json",
        ),
    )
    return entity


def _build_cluster() -> dict[str, FtmEntity]:
    """The 3-source cluster: A and B corroborate the shared props, C adds a unique passport."""
    return {
        "e-a": _source_entity("e-a", SRC_A),
        "e-b": _source_entity("e-b", SRC_B),
        "e-c": _source_entity("e-c", SRC_C, extra={_ADVERSARIAL_PROP: [_ADVERSARIAL_VALUE]}),
    }


def _value_set(entity: FtmEntity) -> dict[str, list[str]]:
    """A canonical, comparable value set: per-property the SORTED list of its values.

    This is the byte-for-byte comparison unit for the §9 value-set-invariance fence. The "id"
    pseudo-property is excluded (it differs by construction — the canonical id vs the member id).
    """
    return {prop: sorted(entity.get(prop)) for prop in entity.properties}


def _value_entity_oracle(
    canonical_id: str, member_ids: tuple[str, ...], by_id: dict[str, FtmEntity]
) -> FtmEntity:
    """Reconstruct the CURRENT ``ValueEntity`` fusion value set INDEPENDENTLY of ``merge.py``.

    This mirrors the legacy ``merge.py:281-286`` path (seed from ``member_ids[0]`` via
    ``make_entity``, then ``ValueEntity.merge`` each member) directly in the test, so it remains a
    faithful oracle for the §9 fence even AFTER the builder rewrites ``_merge_entities`` to use
    ``StatementEntity``. The fence asserts the new fusion's value set equals THIS oracle
    byte-for-byte (spec A10 / D-VALUESET).
    """
    base = by_id[member_ids[0]]
    oracle = make_entity({**base.to_dict(), "id": canonical_id})
    for member_id in member_ids:
        oracle.merge(by_id[member_id])
    return oracle


def _witness_map(entity: FtmEntity) -> dict[str, set[str]]:
    """Call the multi-source Tier-1 witness API the builder must expose.

    Imported lazily so the PRE-FIX failure is the precise, intended one ("``witness_map`` does not
    exist yet"), not a module-load error in an unrelated import. Once the builder lands
    ``provenance.model.witness_map``, this resolves and the lineage assertions can run.
    """
    model = importlib.import_module("worldmonitor.provenance.model")
    witness_map = getattr(model, "witness_map", None)
    if witness_map is None:
        pytest.fail(
            "Gate C multi-source API missing: worldmonitor.provenance.model.witness_map(entity) "
            "-> dict[str, set[str]] is not implemented. The builder must expose the Tier-1 "
            "per-property witness-set view derived from the fused StatementEntity (spec §3/§5)."
        )
    return witness_map(entity)


# --------------------------------------------------------------------------------------------------
# Case 1 — 3-source merge ⇒ 3 retained lineages (THE CRUX, spec A1 / DENY D-COLLAPSE)
# --------------------------------------------------------------------------------------------------
def test_three_source_merge_retains_all_three_lineages() -> None:
    """The SAME entity asserted by 3 distinct sources fuses to a node witnessed by ALL THREE.

    FAILS pre-fix: the ``ValueEntity`` fusion + ``provenance_node_properties``/``get_provenance``
    ``[0]`` read keep only ``src-A``'s lineage, and ``witness_map`` does not exist. PASSES once the
    ``StatementEntity`` fusion + Tier-1 ``witness_map`` land.
    """
    by_id = _build_cluster()
    merged, dropped = _merge_entities(_CANONICAL_ID, _MEMBER_IDS, by_id)

    # The (merged, dropped) contract holds: a same-schema 3-source cluster drops nobody (H-2).
    assert dropped == (), (
        "no member is schema-incompatible; the (merged, dropped) contract must hold"
    )
    assert merged.id == _CANONICAL_ID

    witnesses = _witness_map(merged)

    # The corroborated props are witnessed by ALL THREE datasets — not just source[0] (src-A).
    for prop in _SHARED_PROPS:
        assert witnesses.get(prop) == {SRC_A, SRC_B, SRC_C}, (
            f"prop {prop!r} must be witnessed by all 3 sources after fusion; "
            f"got {witnesses.get(prop)!r} (a {{src-A}} result is the D-COLLAPSE bug)"
        )

    # The union of every property's witnesses must cover all three datasets (no source dropped).
    all_witnesses: set[str] = set().union(*witnesses.values()) if witnesses else set()
    assert all_witnesses == {SRC_A, SRC_B, SRC_C}, (
        f"the fused entity must retain all 3 source lineages; got {sorted(all_witnesses)!r}"
    )


# --------------------------------------------------------------------------------------------------
# Case 2 — VALUE-SET INVARIANCE FENCE (#1 person-safety, spec §9 / A10 / DENY D-VALUESET)
# --------------------------------------------------------------------------------------------------
def test_fused_value_set_is_byte_for_byte_identical_to_value_entity_path() -> None:
    """Switching to ``StatementEntity`` adds LINEAGE but changes NO value.

    The fused value set MUST equal the independent ``ValueEntity`` oracle byte-for-byte — same
    ``name``/``nationality``/``birthDate``/``passportNumber``, same multiset of values per prop.
    This is the load-bearing fence that proves Gate C is lineage-only and person-NEUTRAL.
    It PASSES pre- AND post-fix; if it EVER goes red the gate is a DENY (the merge
    silently changed an ER value).
    """
    by_id = _build_cluster()

    oracle = _value_entity_oracle(_CANONICAL_ID, _MEMBER_IDS, by_id)
    expected_value_set = _value_set(oracle)

    # Hard-pin the oracle too, so a (hypothetical) FtM behaviour change can't silently move BOTH
    # sides of the comparison in lockstep and hide a regression.
    assert expected_value_set == {
        "name": ["Vladimir Example"],
        "nationality": ["ru"],
        "birthDate": ["1960-01-01"],
        "passportNumber": [_ADVERSARIAL_VALUE],
    }

    merged, _dropped = _merge_entities(_CANONICAL_ID, _MEMBER_IDS, by_id)
    assert _value_set(merged) == expected_value_set, (
        "the StatementEntity-fused value set must be byte-for-byte identical to the ValueEntity "
        "path (spec A10 / D-VALUESET — lineage may be added, but no value may change)"
    )


# --------------------------------------------------------------------------------------------------
# Case 3 — adversarial: a value ONLY ONE source has (spec §13 / A2 / DENY D-COLLAPSE)
# --------------------------------------------------------------------------------------------------
def test_single_source_value_retained_and_witnessed_by_exactly_that_source() -> None:
    """src-C's passport (no other source asserts it) is retained AND witnessed by exactly {src-C}.

    This is the judge's heaviest-weighted adversarial target. Pre-fix the value's lineage collapses
    to ``source[0]`` (src-A), so the witness is wrong (or absent). Post-fix the passport's witness
    set is EXACTLY ``{src-C}`` — pinned so a future ``delete_source('src-C')`` could remove only it,
    leaving src-A/src-B untouched.
    """
    by_id = _build_cluster()
    merged, _dropped = _merge_entities(_CANONICAL_ID, _MEMBER_IDS, by_id)

    # (1) the value survives the fusion (it is in the fused value set — also part of the §9 fence).
    assert merged.get(_ADVERSARIAL_PROP) == [_ADVERSARIAL_VALUE], (
        f"the single-source {_ADVERSARIAL_PROP} must be retained in the fused entity"
    )

    # (2) its Tier-1 witness set is EXACTLY {src-C} — not {src-A} (source[0] collapse), not all 3.
    witnesses = _witness_map(merged)
    assert witnesses.get(_ADVERSARIAL_PROP) == {SRC_C}, (
        f"{_ADVERSARIAL_PROP} is asserted only by {SRC_C}; its witness set must be exactly "
        f"{{{SRC_C!r}}}, got {witnesses.get(_ADVERSARIAL_PROP)!r}"
    )

    # And it must NOT have leaked any other source's lineage onto the single-source prop.
    assert SRC_A not in witnesses.get(_ADVERSARIAL_PROP, set()), (
        "the source[0] collapse bug would mis-witness the passport as src-A — it must not"
    )
    assert SRC_B not in witnesses.get(_ADVERSARIAL_PROP, set())
