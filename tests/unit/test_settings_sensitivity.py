"""Gate E (slice-2) — new ``Settings`` knobs for the k-hop depth + Chow abstain band.

Spec: ``docs/reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md`` §6 (config table) + DENY E-CONFIG-OPEN.
ADR: ``docs/decisions/0047-fail-closed-sensitivity-guard.md`` Decision 6.

PRIMARY invariant tests for the slice-2 config surface, pinning DEFAULTS and the field VALIDATOR:

| Field                       | Type           | Default | Meaning                                 |
|-----------------------------|----------------|---------|-----------------------------------------|
| ``sensitivity_khop_depth``  | ``int (ge=0)`` | ``1``   | Stage-2 depth; ``0`` disables Stage 2   |
| ``sensitivity_abstain_low`` | ``float[0,1]`` | ``0.92``| band low bound (incl); ==high⇒OFF |
| ``sensitivity_abstain_high``| ``float[0,1]`` | ``0.92``| band high bound (excl); ==low⇒OFF |

Defaults ship the abstain band OFF (``low == high == 0.92``) so slice-2 is a no-op until tuned. A
field-validator rejects ``abstain_low > abstain_high``.

Why RED on the current tree: ``Settings`` has none of these fields, so reading
``Settings(_env_file=None).sensitivity_khop_depth`` raises ``AttributeError`` (the default tests),
and the validator test's ``pytest.raises(ValidationError)`` does NOT raise because the unknown
kwargs are silently dropped by ``extra="ignore"`` (DID NOT RAISE). Both fail for the RIGHT reason
and pass once the builder adds the fields + the validator.

Defaults are read with ``Settings(_env_file=None)`` (the ``.env``-robust pattern) so a developer's
local ``.env`` cannot perturb the asserted defaults. This MUST NOT assert anything about
``DEFAULT_MERGE_THRESHOLD`` (the band is a distinct axis — spec §3.4 / DENY E-THRESHOLD).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from worldmonitor.settings import Settings


def test_sensitivity_khop_depth_defaults_to_one() -> None:
    """Stage-2 default traversal depth is 1 (ADR 0047 Decision 6): a direct one-hop neighbour."""
    assert Settings(_env_file=None).sensitivity_khop_depth == 1


def test_sensitivity_khop_depth_zero_is_allowed_kill_switch() -> None:
    """``sensitivity_khop_depth = 0`` is valid — the kill-switch disabling Stage 2 (``ge=0``)."""
    assert Settings(_env_file=None, sensitivity_khop_depth=0).sensitivity_khop_depth == 0


def test_sensitivity_khop_depth_rejects_negative() -> None:
    """Depth is ``int ge=0`` — a negative depth is invalid (it is inlined into ``[*1..k]``)."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sensitivity_khop_depth=-1)


def test_sensitivity_khop_depth_accepts_override() -> None:
    assert Settings(_env_file=None, sensitivity_khop_depth=2).sensitivity_khop_depth == 2


def test_abstain_band_defaults_to_off() -> None:
    """The abstain band ships OFF: ``low == high == 0.92`` ⇒ an empty half-open interval (no park).

    Pins the spec §6 "defaults ship the abstain band OFF" invariant. ``0.92`` is the documented
    default; the band is a no-op until a human tunes it.
    """
    settings = Settings(_env_file=None)
    assert settings.sensitivity_abstain_low == 0.92
    assert settings.sensitivity_abstain_high == 0.92
    assert settings.sensitivity_abstain_low == settings.sensitivity_abstain_high, (
        "low == high ⇒ empty band ⇒ Stage 3 is a no-op by default"
    )


def test_abstain_band_accepts_valid_override() -> None:
    """A valid tuned band (low <= high, both in [0, 1]) is accepted."""
    settings = Settings(_env_file=None, sensitivity_abstain_low=0.90, sensitivity_abstain_high=0.95)
    assert settings.sensitivity_abstain_low == 0.90
    assert settings.sensitivity_abstain_high == 0.95


def test_abstain_band_equal_bounds_allowed() -> None:
    """``low == high`` is valid (the OFF/empty-band config) — the validator allows equality."""
    settings = Settings(_env_file=None, sensitivity_abstain_low=0.95, sensitivity_abstain_high=0.95)
    assert settings.sensitivity_abstain_low == settings.sensitivity_abstain_high == 0.95


def test_abstain_low_above_high_is_rejected() -> None:
    """The field-validator rejects ``abstain_low > abstain_high`` (an inverted, nonsensical band).

    Spec §6 / ADR 0047 Decision 6: a validator enforces ``abstain_low <= abstain_high``. PRE-FIX the
    fields do not exist, so the unknown kwargs are dropped by ``extra="ignore"`` and NO
    ``ValidationError`` is raised → ``pytest.raises`` fails (DID NOT RAISE). POST-FIX the validator
    rejects the inverted band.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sensitivity_abstain_low=0.95, sensitivity_abstain_high=0.90)


def test_abstain_bounds_reject_out_of_unit_range() -> None:
    """Each bound is a probability in ``[0.0, 1.0]`` (``ge=0.0, le=1.0``)."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sensitivity_abstain_low=-0.1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sensitivity_abstain_high=1.5)
