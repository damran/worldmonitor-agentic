"""Fold-side completeness check — the alias<->co-commit invariant (Gate WPI-2 / ADR 0111).

INV-ALIAS-COCOMMIT: for every supersession alias ``prior -> survivor`` recorded in the
``canonical_id_ledger``, the final survivor ``survivor_of(prior)`` must have >= 1 **statement
row** folding into it at rebuild — equivalently, the fold must materialise a node for it. Today
the invariant holds by construction: both alias producers (``pipeline.py::_resolve_batch`` and
``signoff.py::approve``) co-commit a supersession alias and >= 1 statement row in the SAME
transaction. This module turns that (currently-holding) co-commit assumption into an enforced,
fail-loud check at ``full_rebuild`` time, catching a future regression (a new producer, or a
reorder across a transaction boundary) before it silently materialises an aliased survivor with
an empty/missing node.

**Statement rows only — context-claims are NOT counted as coverage (checker-confirmed, ADR 0111
§Decision).** ``reconstruct_entities`` (``projector.py``) materialises a node ONLY from statement
rows: it groups by statement rows, and anchors are applied *inside* that statement-group loop, so
a survivor with only context-claim rows yields **no node** (see ``projector.py`` docstring, "a
context-claim-only survivor therefore yields no entity and no anchors"). Therefore a survivor is
reconstructable-as-a-node **iff** it has >= 1 statement row; counting a context-claim row as
coverage would be a *false-pass* — certifying "safe" an aliased survivor that in fact materialises
no node. A zero-prop merge survivor (whether or not it carries anchors) has zero statement rows
(``fuse_statement_rows`` skips the only non-``id`` pseudo-prop) and so **correctly fails loud
here**; giving it a materialisation (an existence-claim statement, or fold-from-context) is WPI-1 /
ADR 0112. That is the intended interlock: WPI-2 makes the zero-prop-merge residual fail loud;
WPI-1 gives it a node.

PURE — no DB, no Neo4j, no ORM/session imports. Driven by a plain iterable whose elements expose
a ``.canonical_id`` attribute (real ``StatementRecord`` rows, or a lightweight test stand-in). Row
elements are typed ``Any`` rather than a structural ``Protocol`` — under ``pyright strict`` a
``Protocol`` attribute does not resolve SQLAlchemy's ``Mapped[str]`` instance-access descriptor for
structural conformance, so it would incorrectly reject real ORM rows; this module only ever reads
``row.canonical_id`` off each element.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


class IncompleteAliasedSurvivorError(RuntimeError):
    """Raised when a supersession alias's final survivor has no foldable statement row.

    Signals a violation of INV-ALIAS-COCOMMIT (ADR 0111): the log holds an alias
    ``prior -> survivor`` but no ``StatementRecord`` folds into ``survivor_of(prior)`` — the fold
    would otherwise materialise no node for the survivor (an empty/missing aliased node).
    """


def _build_survivor_of(alias_map: dict[str, str]) -> Callable[[str], str]:
    """Build a transitive resolver from a supersession-only alias map.

    Mirrors :func:`worldmonitor.resolution.projector.build_survivor_of`'s inner fixed-point
    walk exactly (same cycle-guarded loop) but is intentionally NOT imported from there — this
    module stays self-contained and pure (no projector/DB/session dependency).
    """

    def survivor_of(cid: str) -> str:
        seen: set[str] = set()
        current = cid
        while current in alias_map and current not in seen:
            seen.add(current)
            current = alias_map[current]
        return current

    return survivor_of


def find_incomplete_aliased_survivors(
    alias_map: dict[str, str],
    statement_rows: Iterable[Any],
    *,
    survivor_of: Callable[[str], str] | None = None,
) -> set[str]:
    """Return the set of aliased FINAL survivors with NO foldable statement row.

    A survivor is "covered" (reconstructable-as-a-node) iff it has >= 1 statement row folding into
    it — matching exactly what ``reconstruct_entities`` materialises. Context-claim rows are
    deliberately NOT accepted here: under the current fold a context-only survivor materialises no
    node, so counting a context row as coverage would be a false-pass (see the module docstring +
    ADR 0111 §Decision). WPI-1 / ADR 0112 will give zero-prop survivors a statement row (or a
    fold-from-context materialisation); until then such a survivor correctly fails loud here.

    :param alias_map:      Supersession-only ``canonical_alias -> canonical_id`` map, as returned
                            by ``projector._load_alias_map``.
    :param statement_rows: Statement-lane rows folding into the rebuild (each exposes
                            ``.canonical_id``).
    :param survivor_of:    The projector's transitive resolver. If ``None``, a resolver is built
                            from ``alias_map`` (same fixed-point walk, cycle-guarded).
    :returns: ``targets - covered`` where ``targets = {survivor_of(a) for a in alias_map}``
              (aliased final survivors, resolved TRANSITIVELY — NOT ``set(alias_map.values())``,
              which would wrongly require an intermediate hop in a chain ``a -> b -> c`` to carry
              its own row) and ``covered = {survivor_of(row.canonical_id) for row in
              statement_rows}``.
    """
    resolve = survivor_of if survivor_of is not None else _build_survivor_of(alias_map)

    targets = {resolve(alias) for alias in alias_map}
    covered = {resolve(str(row.canonical_id)) for row in statement_rows}
    return targets - covered
