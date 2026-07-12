# Gate WPI-2 — alias⇔co-commit invariant (ADR 0111)

> Write-path-integrity slice 2 of 3 (F1 pre-cutover). Non-person-affecting, reversible, additive.
> Consult item §7-6 (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md`). Owner-mapped in
> `docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md:128-132`. Fable-sharpened plan:
> `.claude/plans/merry-doodling-dolphin.md` §"Slice 2 — WPI-2".

## Why

The fold materialises **one node per survivor group** (`projector.py:151-159`) and a promoted merge writes
a supersession **alias** (`record_durable_id`) so `survivor_of` rewrites collapsed-member ids and inbound
edge endpoints onto the surviving id. If the ledger holds an alias `prior → survivor` but **no** foldable
content row folds into `survivor`, a rebuild yields an **aliased survivor with an empty node** (inbound
edges rewrite onto a property-less, provenance-less node). Post-3b, rebuild-from-log is the routine path,
so that is a silent structural corruption. This gate turns the (currently-holding) co-commit assumption
into an **enforced, fail-loud invariant**.

## Invariant

**`INV-ALIAS-COCOMMIT`** — for every supersession alias `prior → survivor` in `canonical_id_ledger`, the
final survivor `survivor_of(prior)` has **≥1 foldable content row** (a `StatementRecord` **or** a
`ContextClaimRecord`) folding into it at rebuild. Equivalently: a `full_rebuild` fold never produces an
aliased survivor with an empty/incomplete node — it **fails loud** (`IncompleteAliasedSurvivorError`) before
writing Neo4j if the log violates this.

## Mechanism (ADR 0111 option (a))

### New pure module `src/worldmonitor/resolution/spine_integrity.py`

```
class IncompleteAliasedSurvivorError(RuntimeError): ...

def find_incomplete_aliased_survivors(
    alias_map: dict[str, str],                     # supersession-only map (alias -> canonical), as _load_alias_map returns
    statement_rows: Iterable[<has .canonical_id>],
    context_claim_rows: Iterable[<has .canonical_id>],
    *,
    survivor_of: Callable[[str], str] | None = None,   # projector's transitive resolver; built from alias_map if None
) -> set[str]:
    """Return the set of aliased FINAL survivors with NO foldable statement- or context-claim row.

    Pure — no DB, no Neo4j. If ``survivor_of`` is None, build a transitive resolver from ``alias_map``
    (same fixed-point walk as projector.build_survivor_of, with a visited-guard against a cycle).

      targets = {survivor_of(a) for a in alias_map}          # aliased final survivors (NOT set(values()))
      covered = {survivor_of(str(r.canonical_id)) for r in statement_rows}
              | {survivor_of(str(r.canonical_id)) for r in context_claim_rows}
      return targets - covered
    """
```

**Correctness points the builder MUST hold:**
- `targets` resolves alias keys **transitively** to the final survivor. `set(alias_map.values())` is WRONG:
  for a chain `a → b → c`, rows for `b` fold into `c`, so requiring intermediate `b` to be covered
  false-fires. (Metamorphic test pins this.)
- `covered` unions **both lanes** — context-claims count as content (a zero-prop-with-anchor survivor is
  reconstructable and must not trip).
- Pure and self-contained: the `@given` test drives it with plain dicts/lists (no session).

### `projector.project()` — one import + one guarded call

Insert, **only when `full_rebuild is True`**, after the statement/context row loads and
`survivor_of = build_survivor_of(session)` (≈ after `:365`) and **before** `write_entities` (`:423`):

```python
if full_rebuild:
    incomplete = find_incomplete_aliased_survivors(
        _load_alias_map(session),
        statement_rows,
        context_claim_delta_rows,          # == the complete context log under full_rebuild
        survivor_of=survivor_of,
    )
    if incomplete:
        raise IncompleteAliasedSurvivorError(
            f"{len(incomplete)} aliased survivor(s) have no foldable content row at rebuild: "
            f"{sorted(incomplete)[:20]}"
        )
```

**Why `full_rebuild`-gated:** under `full_rebuild` the projector reads the ENTIRE statement + context log
(`last_*_seq = 0`, no `seq >` filter — `:330-355`), so `statement_rows` + `context_claim_delta_rows` are the
complete log and the check is exact. In incremental mode those are only the delta, so a completeness check
would false-fire on any aliased survivor not touched this delta. Incremental integrity is upheld in real
time by the producer co-commit; the raise deliberately lives at the rebuild path (the Gate-3b-routine path).

`reconstruct_entities` and everything else in `projector.py` stay **byte-unchanged**.

## Acceptance criteria

1. **Mandatory `@given` property test** (`tests/property/test_prop_alias_cocommit.py`) — non-negotiable
   (CLAUDE.md build discipline). Over synthetic `(alias rows, statement rows, context_claim rows)`:
   - **positive**: every alias target has ≥1 foldable row ⇒ `find_incomplete_aliased_survivors` returns ∅
     **and** a real `project(full_rebuild=True)` over that log does **not** raise;
   - **metamorphic negative**: inject an alias target with zero foldable rows ⇒ it is in the returned set
     **and** `project(full_rebuild=True)` raises `IncompleteAliasedSurvivorError`;
   - **transitive-chain**: `a → b → c` with content only under `c` ⇒ ∅ (intermediate `b` needs no row);
   - **context-only**: an alias target with only context-claim rows (no statements) ⇒ NOT incomplete.
   Test the pure function (unit-fast) AND drive real `project()` (integration marker for the DB-backed arm).
   RED before the builder (the module/exception does not exist yet).
2. **Producer co-commit example test** (`tests/integration/test_alias_cocommit.py`) — behavioural, pins
   both alias producers. After a real 2-member merge promotes through `resolve_pending`, the survivor has
   **both** ledger alias rows **and** ≥1 statement row (co-committed); a `project(full_rebuild=True)` over
   the resulting log raises nothing. Same for `signoff.approve()` on a parked merge. (Behavioural, not
   source-grep — robust to refactors that keep the co-commit.)
3. Full `pytest -m "not integration"` + local `-m integration` green; `ruff format --check .` repo-wide
   clean; `ruff check` clean; `pyright` clean.
4. The **checker independently reproduces** `INV-ALIAS-COCOMMIT` against the diff.
5. `reconstruct_entities` diff is **empty**; the only `projector.py` change is one import + one
   `full_rebuild`-gated call. No schema change, no migration. No change to any producer write logic or any
   merge/park/ER/erasure/sign-off outcome.
6. **Builder gate before landing:** run the full projector/rebuild **integration** suite under strict
   (`Settings(enforcement_profile="strict")`, or `.env` moved aside) and confirm **no existing corpus /
   fixture trips the raise**. If one does, that IS the WPI-1 zero-prop-zero-anchor-merge hazard — surface
   it in the PR, do not suppress the check.

## FROZEN (byte-unchanged this gate)

- `src/worldmonitor/resolution/projector.py` — `reconstruct_entities`, `_load_alias_map`,
  `build_survivor_of`, the read/fold/write/checkpoint flow: all byte-unchanged except the one import + one
  `full_rebuild`-gated guard call in `project()`.
- `src/worldmonitor/resolution/statements.py` (append-only writers + fusion — unchanged),
  `src/worldmonitor/resolution/pipeline.py`, `src/worldmonitor/resolution/signoff.py`,
  `src/worldmonitor/resolution/canonical.py`, `src/worldmonitor/resolution/merge.py`,
  `src/worldmonitor/resolution/erasure_scrub.py`, all merge/park/erasure paths.
- `src/worldmonitor/db/models.py` (**no schema change**), `db/migrations/**` (**no new migration**),
  `graph/**`, `ontology/**`, `settings.py`, `runner/**`, every existing `tests/property/*` and
  `tests/integration/*` not listed in the gate.scope allowlist.

## Governance

`person_affecting:false`, `human_fork:false` — reversible (drop the module + call), additive, changes no
individual-affecting outcome. **No cosign, no human fork.** ADR 0111 is written PROPOSED; it flips
PROPOSED→ACCEPTED + `docs/decisions/README.md` index regen (`scripts/gen_adr_index.py`) in the **same PR**
at merge. proceed-and-report. ONE PR (docs + tests + src ship together). Branch: `gate/wpi2-alias-cocommit`.
