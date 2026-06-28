"""Property/metamorphic harness for ADR 0073 — abjad (Arabic/Persian) name normalization.

The ER name key (`_name_fingerprint` in `resolution/splink_model.py`) projects a record's name
via `fingerprints.generate`. For abjad scripts that library transliterates the short-vowel marks
(harakat/tashkeel) into Latin vowels, so the SAME name written WITH vs WITHOUT tashkeel projects a
DIFFERENT key — two records of one (often sanctioned) individual never co-block / never hit the
exact level. ADR 0073 closes that hole by stripping the mark codepoints (a PURE DELETION) before
`fingerprints.generate`, via a new `_strip_arabic_marks` helper.

These pin the REAL invariant, not the implementation:

* INV-1 (recall, behavioral): `_name_fingerprint` is INVARIANT under tashkeel decoration. Pins the
  bug directly through the public projection — so it FAILS on pre-fix HEAD by ASSERTION (the recall
  hole), independent of how the fix is coded. `_strip_arabic_marks` is NOT imported here.
* INV-2 (precision/structure): `_strip_arabic_marks` deletes ONLY the ADR-0073 mark codepoints and
  nothing else (output is the marks-filtered subsequence of the input) + is idempotent. This is what
  guarantees NO new over-merge. The reference mark set is written out from the Unicode ranges here,
  independent of the builder's set.
* INV-3 (no-op): on text containing none of those codepoints the helper is the identity, and the
  ADR-0035 Legion Cyrillic/Latin keys still project byte-for-byte unchanged.

House style follows `tests/property/test_prop_er_merge.py` (`@given` + shared `settings(...)`).
"""

from __future__ import annotations

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.ftm import FtmEntity, make_entity

# IMPORTANT: only the PUBLIC projection is imported at module scope, so INV-1 (the red-first recall
# property) fails by ASSERTION on pre-fix HEAD — never by a collection-time ImportError of the
# not-yet-built helper. `_strip_arabic_marks` is imported LOCALLY inside the structural tests only.
from worldmonitor.resolution.splink_model import _name_fingerprint

# deadline=None: the first timed example pays the one-time followthemoney + fingerprints/ICU lazy
# init (heavier under coverage tracing), like the other heavy property suites (e.g.
# test_prop_node_provenance.py). Bounded by max_examples; the assertions, not wall-clock, are the
# invariant.
_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

# --- Abjad alphabets + the ADR-0073 mark codepoint set (written out independently) ---------------

# Arabic base letters U+0621-U+063A and U+0641-U+064A (the consonant skeleton carriers), plus the
# Persian-specific letters peh/tcheh/jeh/keheh/gaf/farsi-yeh. These carry meaning — they are NEVER
# stripped; a name that differs in any of them is a different name.
ARABIC_BASE_LETTERS = [chr(c) for c in range(0x0621, 0x063B)] + [
    chr(c) for c in range(0x0641, 0x064B)
]
PERSIAN_EXTRA_LETTERS = ["پ", "چ", "ژ", "ک", "گ", "ی"]
BASE_LETTERS = ARABIC_BASE_LETTERS + PERSIAN_EXTRA_LETTERS

# The EXACT ADR-0073 mark ranges (harakat/tashkeel + Qur'anic annotation marks + dagger alif +
# tatweel/kashida). Written out from the ADR here so this test is an INDEPENDENT oracle — it does
# NOT import the builder's codepoint set.
_MARK_RANGES: tuple[tuple[int, int], ...] = (
    (0x0610, 0x061A),  # Arabic signs
    (0x064B, 0x065F),  # harakat + extended marks
    (0x0670, 0x0670),  # superscript alef / dagger alif
    (0x06D6, 0x06DC),  # Qur'anic annotation marks
    (0x06DF, 0x06E4),
    (0x06E7, 0x06E8),
    (0x06EA, 0x06ED),
    (0x0640, 0x0640),  # tatweel / kashida
)
MARKS = [chr(c) for lo, hi in _MARK_RANGES for c in range(lo, hi + 1)]
MARK_SET = frozenset(MARKS)


def _reference_strip(s: str) -> str:
    """The ground-truth `_strip_arabic_marks`: delete EXACTLY the ADR-0073 mark codepoints, keep the
    rest in order (the marks-filtered subsequence of the input)."""
    return "".join(ch for ch in s if ch not in MARK_SET)


def _person(name: str) -> FtmEntity:
    """A minimal named FtM Person (the abjad-name case is overwhelmingly people)."""
    return make_entity(
        {"id": "x", "schema": "Person", "properties": {"name": [name]}, "datasets": ["t"]}
    )


@st.composite
def _bare_and_decorated(draw: st.DrawFn) -> tuple[str, str]:
    """A bare abjad name (base letters only) and a DECORATED variant of the SAME name produced by
    inserting random harakat/tashkeel marks after letters. By construction the decorated string is
    the bare string with only mark codepoints added, so a correct strip collapses them to the same
    key — the metamorphic relation. At least one mark is always inserted (else the pair is trivial).
    """
    letters = draw(st.lists(st.sampled_from(BASE_LETTERS), min_size=1, max_size=8))
    bare = "".join(letters)
    parts: list[str] = []
    inserted = False
    for ch in letters:
        parts.append(ch)
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            parts.append(draw(st.sampled_from(MARKS)))
            inserted = True
    decorated = "".join(parts)
    if not inserted:
        # Force the relation to be non-trivial: append one mark after the final letter.
        decorated = decorated + draw(st.sampled_from(MARKS))
    return bare, decorated


# --- INV-1: RECALL (the primary, behavioral, red-first property) --------------------------------


@given(pair=_bare_and_decorated())
@_SETTINGS
def test_inv1_name_fingerprint_invariant_under_tashkeel(pair: tuple[str, str]) -> None:
    """RECALL: decorating an abjad name with arbitrary tashkeel must NOT change its
    `_name_fingerprint`. On pre-fix HEAD the marks transliterate into Latin vowels (e.g.
    `مُحَمَّد` -> 'muhamad' vs `محمد` -> 'mhmd'), so this FAILS by assertion — pinning the bug, not
    the fix. Names whose bare fingerprint is empty/None are skipped (they never block on a name)."""
    bare, decorated = pair
    bare_fp = _name_fingerprint(_person(bare))
    assume(bare_fp)  # skip no-key names (the null comparison level, not a recall case)
    decorated_fp = _name_fingerprint(_person(decorated))
    assert decorated_fp == bare_fp, (
        "tashkeel decoration changed the ER name key (recall hole): "
        f"bare={bare!r}->{bare_fp!r}  decorated={decorated!r}->{decorated_fp!r}"
    )


# --- INV-2: PRECISION / STRUCTURE (deletes ONLY the listed codepoints) --------------------------

_MIXED_ALPHABET = "".join(BASE_LETTERS + MARKS + list("abcXYZ 0123-.,áéñ"))


@given(s=st.text(alphabet=_MIXED_ALPHABET, min_size=0, max_size=40))
@_SETTINGS
def test_inv2_strip_is_exact_marks_filtered_subsequence(s: str) -> None:
    """PRECISION: `_strip_arabic_marks` removes EXACTLY the ADR-0073 mark codepoints and nothing
    else — its output equals the independently-computed marks-filtered subsequence. A subsequence of
    the input can never collide two DISTINCT consonant skeletons, so no over-merge is introduced."""
    from worldmonitor.resolution.splink_model import _strip_arabic_marks

    expected = _reference_strip(s)
    got = _strip_arabic_marks(s)
    assert got == expected, f"strip is not the exact marks-filtered subsequence: {s!r}->{got!r}"
    # Every surviving codepoint was in the input (pure deletion, never substitution/insertion).
    assert all(ch in s for ch in got)
    # No mark codepoint survives.
    assert not (set(got) & MARK_SET)


@given(s=st.text(alphabet=_MIXED_ALPHABET, min_size=0, max_size=40))
@_SETTINGS
def test_inv2_strip_is_idempotent(s: str) -> None:
    """A second pass is a no-op (the stripped text carries no mark codepoints)."""
    from worldmonitor.resolution.splink_model import _strip_arabic_marks

    once = _strip_arabic_marks(s)
    assert _strip_arabic_marks(once) == once


# --- INV-3: NO-OP on non-abjad text -------------------------------------------------------------

# Latin / Cyrillic / CJK / digits + Latin diacritics — none of these codepoints are in the ADR-0073
# mark ranges, so the helper must be the identity on them.
_NON_ABJAD_ALPHABET = "".join(
    list("abcdefXYZ0123 -.,") + ["ä", "é", "ñ", "ç", "Ω", "Я", "д", "ё", "中", "文", "東", "京"]
)

# ADR-0035 Legion captions (real OpenSanctions us_ofac strings): one Cyrillic, one Latin.
LEGION_CYR = "Общество С Ограниченной Ответственностью Легион Комплект"
LEGION_LAT = "LIMITED LIABILITY COMPANY LEGION KOMPLEKT"


@given(s=st.text(alphabet=_NON_ABJAD_ALPHABET, min_size=0, max_size=40))
@_SETTINGS
def test_inv3_noop_on_non_abjad_text(s: str) -> None:
    """NO-OP: a string containing none of the ADR-0073 mark codepoints is returned IDENTICAL."""
    from worldmonitor.resolution.splink_model import _strip_arabic_marks

    assert _strip_arabic_marks(s) == s


def test_inv3_legion_keys_unchanged_byte_for_byte() -> None:
    """The ADR-0035 Cyrillic/Latin Legion keys are byte-for-byte intact: the strip is a no-op on
    them, so `fingerprints.generate(_strip_arabic_marks(caption))` pins the exact pre-fix keys."""
    import fingerprints

    from worldmonitor.resolution.splink_model import _strip_arabic_marks

    assert _strip_arabic_marks(LEGION_CYR) == LEGION_CYR
    assert _strip_arabic_marks(LEGION_LAT) == LEGION_LAT
    assert fingerprints.generate(_strip_arabic_marks(LEGION_CYR)) == "komplekt legion ooo"
    assert fingerprints.generate(_strip_arabic_marks(LEGION_LAT)) == "komplekt legion llc"
