"""Property: the ransomware_live group-id minting never degenerates to a bare, ambiguous prefix
(Gate S-4 slice 3, ``docs/reviews/GATE_S4_RANSOMWARE_LIVE_SPEC.md`` §3.1 **degenerate-slug
amendment**, added at the end of the section from the slice-2 checker's F1 MEDIUM finding).

``_group_id(x) = f"ransomware-live-group-{_slug(x)}"`` where ``_slug`` strips ALL
non-``[a-z0-9]`` characters after lower-casing. For a raw form with NO ASCII alphanumerics at all
(a name/url-slug in a non-Latin script, or pure punctuation/emoji/whitespace — e.g. "Кибер Група",
"!!!", "🔥🔥🔥"), ``_slug`` folds to the EMPTY string, and the naive formula collapses to the BARE
prefix ``"ransomware-live-group-"`` — silently conflating EVERY such group at the identity layer,
*below* the ER sensitivity guard's sight (a group Organization is always ``crime.cyber``-sensitive,
so a genuine fuzzy merge of two distinct groups would park for review — but an identity-layer
COLLISION never even reaches the guard: it is the same node from the first write). The amendment's
rule: an empty slug falls back to ``f"ransomware-live-group--{_h(raw)}"`` (note the DOUBLE dash —
unreachable from any real slug-derived id, since ``_slug`` output never itself contains a ``-``).

This is a WHITE-BOX property pin (deliberately, per the deliverable spec): it imports the
connector's private ``_group_id``/``_slug``/``_h`` helpers directly — the exact functions ``map()``
calls internally — because the degenerate-slug guard is a MINTING-LAYER invariant with no
observable effect through ``map()``'s public entity output alone (a bare-prefix id is still a
syntactically valid Organization ``id``; only comparing what TWO distinct degenerate raws mint
reveals the collision).

RED TODAY (both properties independently probed via direct calls, see the plan/build log): the
installed connector's ``_group_id`` has no empty-slug fallback yet — ``_group_id("Кибер Група")``
and ``_group_id("!!!")`` and ``_group_id("")`` all currently return exactly the bare prefix
``"ransomware-live-group-"``, so ``test_p_s4_group_id_is_never_the_bare_prefix`` FAILS for any
degenerate-slug example the generator draws (reliably, every run — Cyrillic/CJK/emoji/punctuation
alphabets are heavily biased in below). GREEN once the builder adds the double-dash hash fallback
per the amendment. ``test_p_s4_distinct_slugs_never_collide`` pins the OTHER half of the amendment
(distinctness is never sacrificed for non-degenerate, non-colliding slugs) and may already be
GREEN today (the naive formula already keeps non-empty distinct slugs apart) — reported precisely,
not assumed, in the verification run.
"""

from __future__ import annotations

import re
import string

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from worldmonitor.plugins.connectors.ransomware_live.connector import _group_id, _h, _slug

_SETTINGS = settings(
    max_examples=200, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much]
)

_HASH_FALLBACK_RE = re.compile(r"^ransomware-live-group--[0-9a-f]{16}$")

# --------------------------------------------------------------------------------------------------
# Strategies — biased heavily toward the degenerate case (no ASCII alphanumerics at all): Cyrillic,
# CJK, an emoji block, pure punctuation/whitespace, and the empty string itself — plus a small
# admixture of ordinary ASCII text so the "never bare prefix" property is exercised broadly, not
# only on degenerate inputs.
# --------------------------------------------------------------------------------------------------

_CYRILLIC = st.text(
    alphabet=st.characters(min_codepoint=0x0400, max_codepoint=0x04FF), min_size=0, max_size=15
)
_CJK = st.text(
    alphabet=st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF), min_size=0, max_size=10
)
_EMOJI = st.text(
    alphabet=st.characters(min_codepoint=0x1F300, max_codepoint=0x1F5FF), min_size=0, max_size=6
)
_PUNCT_ONLY = st.text(alphabet=string.punctuation + " \t", min_size=0, max_size=15)
_ORDINARY = st.text(alphabet=string.ascii_letters + string.digits + " -_", min_size=0, max_size=20)

_MIXED_RAW = st.one_of(_CYRILLIC, _CJK, _EMOJI, _PUNCT_ONLY, _ORDINARY, st.just(""))


# ---------------------------------------------------------------------------------------------
# test_p_s4_group_id_is_never_the_bare_prefix
# ---------------------------------------------------------------------------------------------


@given(raw=_MIXED_RAW)
@_SETTINGS
def test_p_s4_group_id_is_never_the_bare_prefix(raw: str) -> None:
    group_id = _group_id(raw)

    assert group_id != "ransomware-live-group-", (
        f"group id minted from raw={raw!r} degenerated to the UNQUALIFIED bare prefix "
        "'ransomware-live-group-' -- an empty _slug MUST fall back to the double-dash hash form "
        "per the amended spec §3.1 (silently conflates EVERY degenerate-slug group at the "
        "identity layer, below the ER sensitivity guard's sight)"
    )

    if _slug(raw) == "":
        assert _HASH_FALLBACK_RE.fullmatch(group_id), (
            f"empty-slug raw={raw!r} must mint the double-dash hash-fallback id "
            f"'ransomware-live-group--<sha1(raw)[:16]>', got {group_id!r}"
        )
        expected = f"ransomware-live-group--{_h(raw)}"
        assert group_id == expected, (
            "the hash fallback must be _h(raw) itself (independent oracle) -- got "
            f"{group_id!r}, expected {expected!r}"
        )


# ---------------------------------------------------------------------------------------------
# test_p_s4_distinct_slugs_never_collide
# ---------------------------------------------------------------------------------------------


@given(raw_a=_MIXED_RAW, raw_b=_MIXED_RAW)
@_SETTINGS
def test_p_s4_distinct_slugs_never_collide(raw_a: str, raw_b: str) -> None:
    slug_a, slug_b = _slug(raw_a), _slug(raw_b)
    assume(slug_a != "" and slug_b != "" and slug_a != slug_b)

    assert _group_id(raw_a) != _group_id(raw_b), (
        f"two raws with distinct NON-EMPTY slugs minted the SAME group id: "
        f"raw_a={raw_a!r} (slug={slug_a!r}), raw_b={raw_b!r} (slug={slug_b!r}) -> "
        f"{_group_id(raw_a)!r}"
    )
