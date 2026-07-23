"""Property: the dossier never launders provenance away (Gate F-3 slice 1, ADR 0122 D5).

CLAUDE.md's G1 ("provenance on every node AND edge") has a surface analogue for this new
aggregation view (spec §3.5 / §6.2): for ANY present entity, the assembled dossier must carry
a non-empty ``provenance`` section, the ``entity`` section must retain EVERY ``prov_*`` key
that section carries, and the ``merge_history`` sentinel must be present. A dossier that ever
presented an entity stripped of its provenance would be a laundering surface — exactly what
G1 forbids.

Unlike F-2 (pure metadata polish, no invariant touched -> no property test recorded), F-3 is a
NEW data-exposure surface that DOES touch provenance exposure, so this ``@given`` is mandatory
per CLAUDE.md's build-discipline rule (a gate touching ER/merge/canonical-id/merge-guard/
provenance MUST add a property/metamorphic test, not just an example test).

RED today: ``worldmonitor.graph.queries`` has no ``get_entity_dossier``.

Pure in-process fake, no DB — no container-leak risk (the "wrap in try/finally" memory note
applies to container-backed bodies; not needed here).
"""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.graph.queries import get_entity_dossier

_ENTITY_FRAGMENT = "RETURN properties(n) AS props"
_NEIGHBORS_FRAGMENT = "properties(m) AS props"
_PROVENANCE_FRAGMENT = "STARTS WITH 'prov_'"

_SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# The real prov_* vocabulary (provenance/model.py) — every combination of >=1 of these is a
# valid "present, provenance-stamped node" shape.
_PROV_KEYS = ("prov_source_id", "prov_retrieved_at", "prov_reliability", "prov_source_record")

_ID = st.text(alphabet="ABCDEFGHIJabcdefghij0123456789:._-", min_size=1, max_size=16)
_VALUE = st.text(alphabet="ABCDEFGHIJabcdefghij0123456789:/-_.T", min_size=1, max_size=24)
_NAME = st.text(alphabet="ABCDEFGHIJabcdefghij ", min_size=1, max_size=12)


@st.composite
def _present_entity(draw: st.DrawFn) -> tuple[str, dict[str, Any], dict[str, str]]:
    """A synthetic entity whose props include >=1 ``prov_*`` key (§6.2's fixture shape)."""
    entity_id = draw(_ID)
    prov_key_subset = draw(
        st.lists(st.sampled_from(_PROV_KEYS), min_size=1, max_size=len(_PROV_KEYS), unique=True)
    )
    prov = {key: draw(_VALUE) for key in prov_key_subset}
    entity_props: dict[str, Any] = {"id": entity_id, "name": [draw(_NAME)], **prov}
    return entity_id, entity_props, prov


class _RecordingFake:
    """Duck-types ``Neo4jClient.execute_read``; dispatches per-helper canned rows.

    ``get_entity`` returns the generated entity props; ``get_neighbors`` returns none
    (irrelevant to this invariant); ``get_provenance`` returns exactly the generated
    ``prov_*`` subset — mirroring what a real prov-stamped node's query would produce.
    Any write path RAISES, proving the helper never writes.
    """

    def __init__(self, *, entity: dict[str, Any], provenance: dict[str, str]) -> None:
        self.entity = entity
        self.provenance = provenance
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.read_calls.append((query, params))
        if _ENTITY_FRAGMENT in query and _NEIGHBORS_FRAGMENT not in query:
            return [{"props": self.entity}]
        if _NEIGHBORS_FRAGMENT in query:
            return []
        if _PROVENANCE_FRAGMENT in query:
            return [{"prov": [[k, v] for k, v in self.provenance.items()]}]
        return []

    def execute_write(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("get_entity_dossier must NEVER call execute_write")

    def session(self) -> Any:
        raise AssertionError("get_entity_dossier must NEVER open a write session")


@given(fixture=_present_entity())
@_SETTINGS
def test_prop_dossier_always_carries_provenance(
    fixture: tuple[str, dict[str, Any], dict[str, str]],
) -> None:
    entity_id, entity_props, prov = fixture
    fake = _RecordingFake(entity=entity_props, provenance=prov)

    dossier = get_entity_dossier(fake, entity_id=entity_id)

    # A present entity must NEVER assemble to None.
    assert dossier is not None, f"a present entity ({entity_id!r}) must never assemble to None"

    # (a) the provenance section is present AND non-empty — never a laundering surface.
    assert dossier.get("provenance"), (
        f"dossier for present entity {entity_id!r} carries an empty/absent provenance section: "
        f"{dossier.get('provenance')!r}"
    )
    assert dossier["provenance"] == prov

    # (b) every prov_* key the provenance section carries is ALSO present (with the same
    # value) on the entity section — the entity is never presented stripped of its own
    # provenance, even though `entity` and `provenance` come from two separate reads.
    entity_section = dossier.get("entity")
    assert isinstance(entity_section, dict)
    for key, value in prov.items():
        assert key in entity_section, (
            f"entity section dropped provenance key {key!r} present in the provenance "
            f"section (laundering): entity={entity_section!r}"
        )
        assert entity_section[key] == value, (
            f"entity section's {key!r} ({entity_section[key]!r}) diverged from the "
            f"provenance section's ({value!r})"
        )

    # (c) the merge_history sentinel is present (recorded absence, never silently omitted).
    assert dossier.get("merge_history") == {"status": "not_assembled", "available": False}, (
        f"merge_history must be the exact recorded-absence sentinel; got "
        f"{dossier.get('merge_history')!r}"
    )
