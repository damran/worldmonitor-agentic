"""Gate L1-a PRIMARY invariant — P-CHOKE: the LLM egress choke-point is a machine-enforced
contract, not a docstring (ADR 0104 item 1, spec `docs/reviews/GATE_L1_LLM_EGRESS_HARDENING_SPEC.md`
§2.2, §2.6).

**NAME:** P-CHOKE (exhaustive guard).

**STATEMENT (INV-CHOKE):** every ``.py`` under ``src/worldmonitor`` **outside**
``src/worldmonitor/llm/`` is free of ``litellm`` / ``openai`` / ``anthropic`` imports, in any
form (``import X``, ``import X.y``, ``import X as z``, ``from X import ...``, ``from X.y import
...``), and free of a best-effort-detectable dynamic import
(``importlib.import_module("litellm")`` / ``__import__("openai")``) of the same modules. The
``llm/`` package is the sole allowlisted home for these SDKs.

**GENERATOR:** deterministic, **exhaustive** enumeration of every ``.py`` file under
``src/worldmonitor`` (NOT sampled) via ``Path.rglob``.

**ORACLE:** an AST-based detector (``find_import_violations`` / ``scan_tree_for_violations``)
returns an empty violation list over the whole tree; on failure the assertion message names the
offending file(s) + module(s).

**NON-VACUITY:** this file's own sanity anchor (``test_sanity_anchor_detector_flags_llm_internals_
without_the_exclusion``) proves the detector is not an always-empty tautology: run WITHOUT the
``llm/`` exclusion it MUST flag ``llm/gateway.py`` and ``llm/claude_shim.py`` (both of which
really do ``import litellm``); run WITH the exclusion it must flag neither. The companion
metamorphic property (``tests/property/test_prop_llm_chokepoint.py``) additionally proves the
detector reacts to an *injected* forbidden import and ignores a same-named docstring/comment/
string-literal decoy — i.e. it is AST-based, not text grep (grep would false-positive on
``api/llm.py``/``settings.py``/``modes.py``, which mention "litellm"/"openai" in prose and string
literals — see ``settings.py``'s ``llm_openrouter_model: str = "openai/gpt-4o"`` default, a real
decoy already present in this tree).

Lives at the **repo root** (like ``tests/test_contract_consistency.py``,
``tests/test_ftm_schema_vendored.py``) so it runs inside the existing ``quality`` CI job under
``pytest -m "not integration"`` — no new CI step, no new dependency (SF-1, ADR 0104).

The detector functions in this module are also imported (via ``importlib.util``, since this
directory has no ``__init__.py`` and is not a regular package) by
``tests/property/test_prop_llm_chokepoint.py`` so the exhaustive guard and the metamorphic
non-vacuity property share exactly one detector implementation.

Expected status when first written: **GREEN** — the current tree already has zero violations
(§1 of the spec: the only real ``litellm`` imports are ``llm/gateway.py`` and
``llm/claude_shim.py``, both inside ``llm/``). That is the correct, non-vacuous state for a
*guard* test: it exists to turn RED the moment a future plugin adds a stray
``import litellm``/``openai``/``anthropic`` outside ``llm/``. Its non-vacuity is proven by the
sanity anchor below, not by starting RED.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Resolve the repo root robustly from THIS file's location, not the cwd:
# tests/test_llm_egress_chokepoint.py -> parents[1] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "worldmonitor"

# The sole allowlisted home for external LLM-provider SDKs (ADR 0104 item 1).
LLM_PACKAGE_PREFIX = "llm/"

# Forbidden top-level modules (ADR 0104 item 1 / spec §2.2).
FORBIDDEN_MODULES: frozenset[str] = frozenset({"litellm", "openai", "anthropic"})

# A prefix that can never match any real relative path — used to disable the llm/
# allowlist for the sanity-anchor / non-vacuity scan.
_NEVER_MATCHES_PREFIX = "\0__l1a_sanity_anchor_never_matches__\0"


def _top_level(dotted_name: str) -> str:
    """``alias.name`` / ``node.module`` -> its top-level package (spec §2.2)."""
    return dotted_name.split(".")[0]


def _dynamic_import_forbidden_module(call: ast.Call) -> str | None:
    """Best-effort (spec §2.2) detection of a dynamic import of a forbidden module.

    Flags ``importlib.import_module("X")`` (an ``ast.Attribute`` call whose attr is
    ``import_module``) and ``__import__("X")`` (an ``ast.Name`` call named ``__import__``)
    when the first positional argument is a **constant string** whose top-level module is
    forbidden. This is explicitly best-effort/defence-in-depth — it does not attempt to
    resolve dynamic module names built from variables/f-strings.
    """
    func = call.func
    is_import_module_call = isinstance(func, ast.Attribute) and func.attr == "import_module"
    is_dunder_import_call = isinstance(func, ast.Name) and func.id == "__import__"
    if not (is_import_module_call or is_dunder_import_call):
        return None
    if not call.args:
        return None
    first_arg = call.args[0]
    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
        top = _top_level(first_arg.value)
        if top in FORBIDDEN_MODULES:
            return top
    return None


def find_import_violations(
    source: str,
    virtual_path: str,
    *,
    exclude_prefix: str = LLM_PACKAGE_PREFIX,
) -> list[tuple[str, str]]:
    """AST-scan ``source`` (the contents of a file logically at ``virtual_path``, a
    ``/``-separated path relative to ``src/worldmonitor``) for forbidden external-SDK
    imports.

    Returns a list of ``(virtual_path, forbidden_module)`` violation tuples. A violation
    requires **both**:

    1. ``virtual_path`` does **not** start with ``exclude_prefix`` (the ``llm/`` allowlist,
       or a sentinel that never matches when the caller wants the allowlist disabled); AND
    2. ``source`` contains a real static import (``ast.Import`` / ``ast.ImportFrom``, in
       ANY placement — module body or inside a function/class — and ignoring genuinely
       relative imports where ``node.level > 0``, which can never resolve to a top-level
       external package) OR a best-effort dynamic import (see
       ``_dynamic_import_forbidden_module``) of a module in ``FORBIDDEN_MODULES``.

    Deliberately AST-based, NOT text/grep-based (ADR 0104 SF-1): a grep would false-positive
    on docstring/comment/string-literal mentions of "litellm"/"openai"/"anthropic" that
    already exist in this tree (e.g. ``api/llm.py``'s prose, ``settings.py``'s
    ``llm_openrouter_model: str = "openai/gpt-4o"`` default).
    """
    if virtual_path.startswith(exclude_prefix):
        return []

    tree = ast.parse(source, filename=virtual_path)
    violations: list[tuple[str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _top_level(alias.name)
                if top in FORBIDDEN_MODULES:
                    violations.append((virtual_path, top))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # a genuinely relative `from . import x` cannot be a forbidden pkg
            if node.module is None:
                continue
            top = _top_level(node.module)
            if top in FORBIDDEN_MODULES:
                violations.append((virtual_path, top))
        elif isinstance(node, ast.Call):
            dynamic_top = _dynamic_import_forbidden_module(node)
            if dynamic_top is not None:
                violations.append((virtual_path, dynamic_top))

    return violations


def _iter_src_py_files() -> list[Path]:
    """Deterministic, exhaustive (NOT sampled) enumeration of every .py under src/worldmonitor."""
    return sorted(SRC_ROOT.rglob("*.py"))


def _virtual_path_for(path: Path) -> str:
    """A '/'-separated path relative to src/worldmonitor, e.g. 'llm/gateway.py'."""
    return path.relative_to(SRC_ROOT).as_posix()


def scan_tree_for_violations(*, respect_llm_exclusion: bool) -> list[tuple[str, str]]:
    """Exhaustively scan every ``.py`` under ``src/worldmonitor`` for forbidden imports.

    When ``respect_llm_exclusion`` is ``False`` the ``llm/`` allowlist is disabled (a
    sentinel prefix that never matches any real path is used instead) — this is the
    sanity-anchor / non-vacuity mode that proves the detector actually detects real
    ``litellm`` imports (``llm/gateway.py``, ``llm/claude_shim.py``) when nothing is
    excluding them.
    """
    exclude_prefix = LLM_PACKAGE_PREFIX if respect_llm_exclusion else _NEVER_MATCHES_PREFIX
    violations: list[tuple[str, str]] = []
    for path in _iter_src_py_files():
        virtual_path = _virtual_path_for(path)
        source = path.read_text(encoding="utf-8")
        violations.extend(
            find_import_violations(source, virtual_path, exclude_prefix=exclude_prefix)
        )
    return violations


# ── P-CHOKE: the exhaustive guard (INV-CHOKE) ──────────────────────────────────────────


def test_no_module_outside_llm_package_imports_forbidden_external_sdk() -> None:
    """P-CHOKE / INV-CHOKE: exhaustive AST scan over EVERY .py under src/worldmonitor.

    A file outside src/worldmonitor/llm/ that imports litellm/openai/anthropic (in any
    form, any placement) is a violation. The assertion names the offending file(s) and
    module(s) on failure -- it is not a bare "no exception" check.
    """
    violations = scan_tree_for_violations(respect_llm_exclusion=True)
    assert violations == [], (
        "forbidden external-LLM-SDK import(s) found OUTSIDE src/worldmonitor/llm/: "
        f"{violations!r}.  INV-CHOKE (ADR 0104 item 1): only src/worldmonitor/llm/** may "
        f"import {sorted(FORBIDDEN_MODULES)!r} -- every other module MUST call through "
        "worldmonitor.llm.gateway.LLMGateway."
    )


# ── Non-vacuity sanity anchor (spec §2.6, P-CHOKE non-vacuity clause) ──────────────────


def test_sanity_anchor_detector_flags_llm_internals_without_the_exclusion() -> None:
    """Non-vacuity anchor: prove the detector actually detects, not that it is always empty.

    WITHOUT the llm/ exclusion, the detector MUST flag llm/gateway.py and
    llm/claude_shim.py (both of which really `import litellm`, per §1 of the spec).
    WITH the exclusion (the real guard's configuration), it must flag neither. A detector
    that is grep-based, always-empty, or that mis-scopes the llm/ allowlist would fail one
    half of this pair.
    """
    unexcluded_violations = scan_tree_for_violations(respect_llm_exclusion=False)
    flagged_paths = {virtual_path for virtual_path, _ in unexcluded_violations}

    assert "llm/gateway.py" in flagged_paths, (
        "sanity anchor failed: WITHOUT the llm/ exclusion the detector should flag "
        f"llm/gateway.py (it does `import litellm`, spec §1); flagged={sorted(flagged_paths)!r}. "
        "A detector that never flags anything is vacuous."
    )
    assert "llm/claude_shim.py" in flagged_paths, (
        "sanity anchor failed: WITHOUT the llm/ exclusion the detector should flag "
        f"llm/claude_shim.py (it does `import litellm`, spec §1); "
        f"flagged={sorted(flagged_paths)!r}."
    )

    excluded_violations = scan_tree_for_violations(respect_llm_exclusion=True)
    excluded_paths = {virtual_path for virtual_path, _ in excluded_violations}
    assert "llm/gateway.py" not in excluded_paths, (
        "the llm/ allowlist must suppress llm/gateway.py's real litellm import -- "
        f"it did not; flagged={sorted(excluded_paths)!r}."
    )
    assert "llm/claude_shim.py" not in excluded_paths, (
        "the llm/ allowlist must suppress llm/claude_shim.py's real litellm import -- "
        f"it did not; flagged={sorted(excluded_paths)!r}."
    )
