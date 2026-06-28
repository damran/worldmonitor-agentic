"""Abjad (Arabic/Persian) name resolution (ADR 0073) — example fixtures on REAL names.

Mirrors `tests/unit/test_resolution_multiscript.py` (ADR 0035 — the same bug class for Cyrillic /
Latin). `fingerprints.generate` transliterates Arabic short-vowel marks (harakat/tashkeel) into
Latin vowels, so the SAME name written WITH vs WITHOUT tashkeel projects a DIFFERENT
`_name_fingerprint` and two records of one (often sanctioned) individual never co-block / never hit
the exact level. ADR 0073 strips the mark codepoints before `fingerprints.generate`.

These pin: (1) tashkeel/tatweel variants of one name CONVERGE; (2) genuinely-distinct abjad names
STAY distinct (no over-merge — the precision side); (3) the catastrophic-merge guard is UNCHANGED —
a SENSITIVE abjad duplicate still merges then PARKS for human sign-off; (4) a non-abjad key is
unchanged.
"""

from __future__ import annotations

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import cluster_and_merge
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import _name_fingerprint, score_pairs

# Real abjad personal names: a BARE spelling, a tashkeel-DECORATED spelling, and (Muhammad) a
# tatweel/kashida-stretched spelling — all ONE name.
MUHAMMAD_BARE = "محمد"
MUHAMMAD_TASHKEEL = "مُحَمَّد"
MUHAMMAD_TATWEEL = "محـمد"
ALI_BARE = "علي"
ALI_TASHKEEL = "عَلِيّ"
HUSSEIN_BARE = "حسين"
HUSSEIN_TASHKEEL = "حُسَيْن"

# Real OpenSanctions us_ofac Cyrillic Legion caption (ADR 0035) — the non-abjad regression anchor.
LEGION_CYR = "Общество С Ограниченной Ответственностью Легион Комплект"


def _person(name: str) -> FtmEntity:
    return make_entity(
        {"id": "x", "schema": "Person", "properties": {"name": [name]}, "datasets": ["t"]}
    )


def _sensitive_org(entity_id: str, name: str) -> FtmEntity:
    """A SANCTIONED Organization (topics=['sanction']) — exercises the catastrophic-merge guard."""
    return make_entity(
        {
            "id": entity_id,
            "schema": "Organization",
            "properties": {"name": [name], "country": ["sy"], "topics": ["sanction"]},
            "datasets": ["t"],
        }
    )


def test_muhammad_tashkeel_and_tatweel_variants_converge() -> None:
    """`مُحَمَّد` (tashkeel) ≡ `محمد` (bare) ≡ `محـمد` (tatweel) — one name, one ER key."""
    bare = _name_fingerprint(_person(MUHAMMAD_BARE))
    assert bare  # the bare consonant skeleton is a real key
    assert _name_fingerprint(_person(MUHAMMAD_TASHKEEL)) == bare
    assert _name_fingerprint(_person(MUHAMMAD_TATWEEL)) == bare


def test_ali_tashkeel_variant_converges() -> None:
    """`عَلِيّ` (tashkeel) ≡ `علي` (bare)."""
    bare = _name_fingerprint(_person(ALI_BARE))
    assert bare
    assert _name_fingerprint(_person(ALI_TASHKEEL)) == bare


def test_hussein_tashkeel_variant_converges() -> None:
    """`حُسَيْن` (tashkeel) ≡ `حسين` (bare)."""
    bare = _name_fingerprint(_person(HUSSEIN_BARE))
    assert bare
    assert _name_fingerprint(_person(HUSSEIN_TASHKEEL)) == bare


def test_distinct_abjad_names_stay_distinct() -> None:
    """Precision / no over-merge: `حسن` / `حسين` / `سعد` / `سعيد` differ in BASE letters, so their
    keys must be pairwise DISTINCT — stripping marks must not collapse different skeletons."""
    names = ["حسن", "حسين", "سعد", "سعيد"]
    fps = [_name_fingerprint(_person(n)) for n in names]
    assert all(fp for fp in fps), f"every distinct name must still project a key: {fps}"
    assert len(set(fps)) == len(fps), f"distinct abjad names collided after normalization: {fps}"


def test_sensitive_abjad_tashkeel_duplicate_merges_then_parks() -> None:
    """INV-4 guard intact: two SANCTIONED Organizations whose ONLY difference is tashkeel on the
    same Arabic name now share the ER key, so they MERGE (the recall fix) — and because both are
    sensitive, the catastrophic-merge guard PARKS the merge for human sign-off (it is never
    auto-fused). The 0.92 threshold and the guard path are unchanged by ADR 0073."""
    a = _sensitive_org("a", MUHAMMAD_TASHKEEL)
    b = _sensitive_org("b", MUHAMMAD_BARE)
    # The fix makes the keys identical (the precondition for the pair to score the exact level).
    assert _name_fingerprint(a) == _name_fingerprint(b)
    merges = [c for c in cluster_and_merge([a, b], score_pairs([a, b])) if c.is_merge]
    assert len(merges) == 1, "tashkeel-only abjad duplicate must merge (was two different keys)"
    flagged, reason = needs_review(merges[0], {"a": a, "b": b})
    assert flagged is True and "sensitive" in reason.lower()


def test_non_abjad_key_unchanged_regression() -> None:
    """Regression: a non-abjad (Cyrillic) name's key is byte-for-byte what ADR 0035 locked —
    the abjad prenorm is a strict no-op off the Arabic block."""
    org = make_entity(
        {
            "id": "c",
            "schema": "Organization",
            "properties": {"name": [LEGION_CYR]},
            "datasets": ["t"],
        }
    )
    assert _name_fingerprint(org) == "komplekt legion"
