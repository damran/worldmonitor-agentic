"""Gate L1-a PRIMARY invariant — P-CHOKE-DETECTOR: the choke-point AST detector is
metamorphically non-vacuous (ADR 0104 item 1, spec §2.6).

**NAME:** P-CHOKE-DETECTOR (metamorphic non-vacuity).

**STATEMENT:** the detector (``find_import_violations``, shared with
``tests/test_llm_egress_chokepoint.py``) flags a forbidden import placed OUTSIDE ``llm/``,
ignores the SAME module name embedded only in a docstring/comment/string-literal, and ignores a
forbidden import placed INSIDE ``llm/``.

**GENERATOR:** ``@given`` over (forbidden module ∈ {litellm, openai, anthropic}) × (import form ∈
{``import X``, ``import X.y``, ``import X as z``, ``from X import a``}) × (placement ∈
{module-body, inside-a-function}) × (decoy kind ∈ {docstring, comment, string-literal}). For each
drawn combination a synthetic source string + a synthetic virtual path are built and fed straight
to the detector helper (no real files touched).

**ORACLE:** for every drawn combination, three synthetic sources are checked against the SAME
detector call:

1. the REAL import at a non-``llm/`` virtual path -> MUST be flagged (violation naming that
   module);
2. the SAME real import (identical source) at an ``llm/`` virtual path -> MUST NOT be flagged;
3. a DECOY source (the module name appears only in a docstring/comment/string-literal, no
   import statement at all) at a non-``llm/`` virtual path -> MUST NOT be flagged.

**NON-VACUITY:** a grep-based detector (``"litellm" in source``) would flag case (3) — the
decoy — which is exactly the false-positive ADR 0104 SF-1 rejects (and which the real tree already
exhibits at ``settings.py``'s ``llm_openrouter_model: str = "openai/gpt-4o"`` default). An
always-empty / always-vacuous detector would fail case (1). A detector that ignores the ``llm/``
allowlist entirely would fail case (2). No implementation can pass all three simultaneously by
being either "always flag" or "never flag" or "flag on substring" — proving the detector is a real
AST-based import check, not a tautology.

Imports the detector via ``importlib.util`` (spec-from-file-location) rather than a package
import, because neither ``tests/`` nor ``tests/property/`` is a regular package (no
``__init__.py``) — this keeps ``tests/test_llm_egress_chokepoint.py`` as the single source of
truth for the detector without adding a new shared-util file outside this gate's
``.claude/gate.scope``.

Expected status when first written: **GREEN** immediately (this is a property ABOUT the
detector, not about production code) — it must stay green forever as a non-vacuity witness.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

# ── Load the shared detector from tests/test_llm_egress_chokepoint.py ─────────────────
# tests/property/test_prop_llm_chokepoint.py -> parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHOKEPOINT_MODULE_PATH = _REPO_ROOT / "tests" / "test_llm_egress_chokepoint.py"


def _load_chokepoint_detector_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "wm_l1a_llm_egress_chokepoint_detector", _CHOKEPOINT_MODULE_PATH
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load detector module from {_CHOKEPOINT_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_chokepoint = _load_chokepoint_detector_module()
find_import_violations = _chokepoint.find_import_violations
FORBIDDEN_MODULES = _chokepoint.FORBIDDEN_MODULES

_HYP_SETTINGS = hyp_settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# ── Generators ──────────────────────────────────────────────────────────────────────────

_MODULES = st.sampled_from(sorted(FORBIDDEN_MODULES))
_FORMS = st.sampled_from(["import_x", "import_x_dot_y", "import_x_as_z", "from_x_import_a"])
_PLACEMENTS = st.sampled_from(["module_body", "inside_function"])
_DECOY_KINDS = st.sampled_from(["docstring", "comment", "string_literal"])

_NON_LLM_VIRTUAL_PATH = "connectors/some_future_plugin.py"
_LLM_VIRTUAL_PATH = "llm/some_internal_helper.py"


def _import_statement(module: str, form: str) -> str:
    if form == "import_x":
        return f"import {module}"
    if form == "import_x_dot_y":
        return f"import {module}.submodule"
    if form == "import_x_as_z":
        return f"import {module} as _aliased_forbidden_sdk"
    if form == "from_x_import_a":
        return f"from {module} import something"
    raise ValueError(f"unknown import form: {form!r}")  # pragma: no cover - exhaustive strategy


def _build_real_import_source(module: str, form: str, placement: str) -> str:
    """A syntactically valid module whose ONLY notable feature is one real import
    of `module` in the requested `form`, at the requested `placement`."""
    stmt = _import_statement(module, form)
    if placement == "module_body":
        return f'"""Synthetic test module."""\n\n{stmt}\n\n\ndef handler() -> int:\n    return 1\n'
    # inside_function: the import is nested inside a function body (still detected by
    # ast.walk regardless of nesting depth).
    return f'"""Synthetic test module."""\n\n\ndef handler() -> int:\n    {stmt}\n    return 1\n'


def _build_decoy_source(module: str, decoy_kind: str) -> str:
    """A syntactically valid module that mentions `module` ONLY as inert text —
    never as a real import statement."""
    if decoy_kind == "docstring":
        return (
            f'"""This module talks about {module} in prose but never imports it."""\n\n\n'
            "def handler() -> int:\n    return 1\n"
        )
    if decoy_kind == "comment":
        return (
            f"# mentions {module} here purely as a comment, never as an import\n\n\n"
            "def handler() -> int:\n    return 1\n"
        )
    if decoy_kind == "string_literal":
        return (
            f'MENTION = "a reference to {module} embedded in a plain string literal"\n\n\n'
            "def handler() -> int:\n    return 1\n"
        )
    raise ValueError(f"unknown decoy kind: {decoy_kind!r}")  # pragma: no cover - exhaustive


# ── P-CHOKE-DETECTOR ────────────────────────────────────────────────────────────────────


@given(
    module=_MODULES,
    form=_FORMS,
    placement=_PLACEMENTS,
    decoy_kind=_DECOY_KINDS,
)
@_HYP_SETTINGS
def test_detector_flags_real_import_outside_llm_ignores_decoy_and_llm_placement(
    module: str,
    form: str,
    placement: str,
    decoy_kind: str,
) -> None:
    """P-CHOKE-DETECTOR: real import outside llm/ -> flagged; same import inside llm/ ->
    not flagged; decoy-only mention outside llm/ -> not flagged. A tautology (always-flag,
    never-flag, or grep-on-substring) fails at least one of the three branches below.
    """
    real_import_source = _build_real_import_source(module, form, placement)

    # (1) REAL import, non-llm/ path -> MUST be flagged, naming this module.
    violations_outside_llm = find_import_violations(
        real_import_source, _NON_LLM_VIRTUAL_PATH, exclude_prefix="llm/"
    )
    flagged_modules = {flagged_module for _, flagged_module in violations_outside_llm}
    assert module in flagged_modules, (
        f"module={module!r} form={form!r} placement={placement!r}: a REAL import outside "
        f"llm/ was NOT flagged.  source=\n{real_import_source}\n"
        f"violations={violations_outside_llm!r}"
    )

    # (2) the SAME real import, llm/ path -> MUST NOT be flagged.
    violations_inside_llm = find_import_violations(
        real_import_source, _LLM_VIRTUAL_PATH, exclude_prefix="llm/"
    )
    assert violations_inside_llm == [], (
        f"module={module!r} form={form!r} placement={placement!r}: a real import INSIDE "
        f"llm/ was incorrectly flagged: {violations_inside_llm!r}.  The llm/ package is the "
        "allowlisted home for these SDKs (ADR 0104 item 1)."
    )

    # (3) DECOY-only source (module name in prose/comment/string, no import), non-llm/ path
    #     -> MUST NOT be flagged. A grep-based ("litellm" in source) detector fails here.
    decoy_source = _build_decoy_source(module, decoy_kind)
    violations_decoy = find_import_violations(
        decoy_source, _NON_LLM_VIRTUAL_PATH, exclude_prefix="llm/"
    )
    assert violations_decoy == [], (
        f"module={module!r} decoy_kind={decoy_kind!r}: a DECOY mention (no real import) "
        f"was incorrectly flagged: {violations_decoy!r}.  source=\n{decoy_source}\n"
        "SF-1 (ADR 0104): the detector MUST be AST-based, not text/grep-based, precisely to "
        "avoid this false positive."
    )
