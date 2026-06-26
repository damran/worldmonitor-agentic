# Gate CID-fix — FtM-valid, injective durable canonical id

> **BUG-FIX gate** (confirmed BLOCKER). Branch off `master`. Spec only — no code here.
> **ADR:** `docs/decisions/0048-ftm-valid-injective-durable-id.md` (PROPOSED).
> **Refines:** ADR 0044 (anchor-preferred stable ids) — only its durable-id *serialization* clause
> (`§Decision.2` + the open Builder-record item "Final durable-id serialization"). Does NOT relitigate
> the precedence (QID>LEI>regNo>taxNo), the ledger, adopt/merge/split, or the ADR-0040 conflict guard.
> **Cross-line:** Workflow A independently hit this exact bug (its `229c899` edge-write fix +
> `34307c3` "CID-5" metamorphic catch, A's ADR 0048). B's fix re-derives A's proven design.

A **non-vacuous failing-test-first** suite is required (the integration edge-survival oracle goes RED
on `master`, GREEN post-fix). The colon-format unit/integration suite stayed green precisely because it
never exercised an anchor-derived id as an *edge endpoint* — that gap is what hid the blocker.

> **ADR-number note (task said "0043"):** in Workflow B, `0043` is the *ER measurement harness*; the
> *anchor-preferred stable ids* ADR is **`0044`**. This gate refines **0044**. The new ADR is **0048**
> (current highest in B is 0047).

---

## 1. The bug (CONFIRMED, reproduced — not hypothetical)

`src/worldmonitor/resolution/canonical.py:143`, in `pick_anchor`:

```python
if len(union) == 1:
    return f"{tier.kind}:{next(iter(union))}"   # qid:Q42 / lei:<20> / regno:<v> / taxno:<v>
```

The **colon** is not in FtM's entity-reference charset (`[A-Za-z0-9.-]`). This string becomes:
1. the cluster's **durable `canonical_id`** (`rekey_cluster`, `pipeline.py:356`),
2. the **graph node id** (native `{id}` MERGE, `graph/writer.py`),
3. the value **rewritten into edge ENDPOINTS** by referent rewriting
   (`build_referent_map` → `rewrite_referents`, `pipeline.py:474-476`; `referents.py:33,53`).

FtM cleans entity-typed property values through `registry.entity`. An edge endpoint that is `qid:Q42`
cleans to **`None`** → the **edge is silently dropped**. The re-keyed node exists, but its owning
Ownership/Directorship/Sanction edge vanishes. Because an anchor-preferred id is minted for **every**
entity carrying a `wikidataId` / `leiCode` / `registrationNumber` / `taxNumber` (a large fraction of
real entities), this corrupts **the product** (the resolved entity graph) for those entities.

### 1.1 Reproduced evidence (installed `followthemoney==4.9.2`, via `uv run python`)

```text
registry.entity.clean('qid:Q42')                      -> None
registry.entity.clean('lei:5493001KJTIIGC8Y1R12')     -> None
registry.entity.clean('regno:HRB12345')               -> None
registry.entity.clean('taxno:DE123')                  -> None
registry.entity.clean('wm-anchor-qid-Q42')            -> 'wm-anchor-qid-Q42'      # hyphen is fine
registry.entity.clean('wm-anchor-regno-HRB-12-345')   -> 'wm-anchor-regno-HRB-12-345'
```

Three additional **hostile-value** hazard classes, also reproduced on 4.9.2 (these are why a naive
sanitize is *not* sufficient — see §3):

```text
# (a) INJECTIVITY: two DISTINCT regNo values collide under a naive strip-to-hyphen sanitize
re.sub(r'[^A-Za-z0-9.-]','-','HRB/12')  == re.sub(r'[^A-Za-z0-9.-]','-','HRB-12')  -> True  # COLLISION
#     -> a silent cross-entity merge the catastrophic-merge guard never sees.

# (b) TRAILING-PUNCTUATION (A's "CID-5"): an already-[A-Za-z0-9.-] value ending in '.'/'-'
registry.entity.clean('wm-anchor-regno-ABC.')         -> None   # edge would drop
registry.entity.clean('wm-anchor-regno-ABC-')         -> None   # edge would drop

# (c) EMPTY after sanitize
registry.entity.clean('wm-anchor-regno-')             -> None
```

regNo/taxNo values are scraped/hostile: `registry.identifier.clean` (the ADR-0039 normaliser
`pick_anchor` already applies) **keeps** `/`, `:`, and spaces —
`identifier.clean('HRB/12-345') == 'HRB/12-345'`, `identifier.clean('HRB:12') == 'HRB:12'` — so a
normalised regNo/taxNo is routinely **not** FtM-entity-clean. QID (`is_qid`) and LEI (20-char alnum)
values are already clean; **regNo/taxNo are the ones that need the injective treatment.**

### 1.2 Why B's green suite hid it

`tests/unit/test_canonical.py`, `tests/test_stable_id.py` etc. assert the **string** durable id
(`f"qid:{Q_A}"`) and the ledger round-trip, but the integration graph oracles
(`test_stable_id_graph.py`, `test_value_provenance_graph.py`) wrote/queried the node, never asserting
that an **edge whose endpoint is the anchor-derived id** survives the write. There is **no** test that
builds an anchored merge, attaches an edge, and reads the edge back. That absent test is the one this
gate adds (§7.1).

---

## 2. Scope (exact files)

**Production code — the fix is ENTIRELY in one file:**
- `src/worldmonitor/resolution/canonical.py`
  - `pick_anchor` (line ~143): mint the FtM-clean injective id instead of `f"{tier.kind}:{value}"`.
  - `record_durable_id` (line ~290): replace the `partition(":")` anchor-kind/value parse with a
    `wm-anchor-<kind>-` prefix parse (the colon discriminator no longer exists).
  - a small **pure** id-construction helper extracted so the unit guard can hammer it directly (§3.2).
  - docstrings/module header updated to the new serialization.

`graph/writer.py`, `resolution/referents.py`, `resolution/merge.py`, `resolution/pipeline.py` are
**format-agnostic string passthroughs** and are **OUT OF SCOPE**. If the builder concludes any of them
needs editing, that is a **scope signal — STOP and flag the human** (the design says they must not).

**Tests — UPDATED by this gate** (they assert the OLD colon format; this gate changes the format, so
these assertions change — see §7.4):
- `tests/unit/test_canonical.py` (also gains the two new guards, §7.2)
- `tests/test_stable_id.py` (the Docker-free durable-id oracle; incl. the `anchor.partition(":")` parse)
- `tests/integration/test_stable_id_graph.py` (gains the new edge-survival oracle, §7.1)
- `tests/unit/test_anchors.py`
- `tests/test_abstract_edge.py`
- `tests/integration/test_value_provenance_graph.py`

**Tests — FROZEN** (no colon-format dependency; must stay green byte-for-byte — see §7.3).

**Docs:** this spec, ADR 0048, and the `.claude/gate.scope`. (A one-line "Refined by ADR 0048"
back-pointer in ADR 0044 is OPTIONAL.)

---

## 3. The fix (FtM-clean + INJECTIVE durable id)

### 3.1 Id format

```
wm-anchor-<kind>-<encoded-value>        kind ∈ {qid, lei, regno, taxno}
wm-mint-<uuid4>                         (unanchored cluster — UNCHANGED)
wmc-<sha256[:40]>                       (unanchored-merge idempotency fingerprint — UNCHANGED, ADR 0036)
<source-id>                             (singleton keeps its own id — UNCHANGED)
```

`<kind>` is the tier kind (`qid`/`lei`/`regno`/`taxno`) — all already FtM-clean tokens, so (unlike A,
whose field names had `_`) **no field→code map is needed**. The fixed `wm-anchor-` prefix + the fixed
4-token kind enum make the kind namespaces mutually disjoint (`wm-anchor-qid-…` can never equal
`wm-anchor-regno-…`), so injectivity reduces to **per-kind injectivity over the value**.

### 3.2 The encoder rule (the LOAD-BEARING part)

Extract a pure helper (name e.g. `_anchor_id(kind: str, value: str) -> str`) and apply, verbatim:

```python
_ANCHOR_ID_PREFIX = "wm-anchor-"
_HASH_TAIL = re.compile(r"-[0-9a-f]{12}$")   # the SHA-256 tail shape; [0-9a-f] keeps the id FtM-clean

def _anchor_id(kind: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]", "-", value)
    candidate = f"{_ANCHOR_ID_PREFIX}{kind}-{safe}"
    if (
        safe != value                                    # (a) sanitisation changed the value
        or registry.entity.clean(candidate) != candidate # (b) would not survive FtM cleaning (CID-5:
                                                          #     an already-safe value ending in '.'/'-')
        or _HASH_TAIL.search(candidate)                  # (c) verbatim id would land in the hashed
                                                          #     namespace -> force-hash (injectivity)
    ):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        candidate = f"{_ANCHOR_ID_PREFIX}{kind}-{safe}-{digest}"
    return candidate
```

`pick_anchor` returns `_anchor_id(tier.kind, next(iter(union)))` for the single-value tier (every other
behaviour — precedence, the ADR-0040 `len(union) > 1` conflict fall-through, the `None` exhaustion case
— is **unchanged**).

### 3.3 Why this is INJECTIVE (no two distinct anchors ever collapse to one id)

Two **provably disjoint** id-namespaces partitioned along the trailing `-<12 hex>` shape:

1. **Different kind** → different `wm-anchor-<kind>-` prefix → disjoint. ✓
2. **Clean (verbatim) branch**, same kind: id is `wm-anchor-<kind>-<value>` with `<value>` embedded
   verbatim. `value1 ≠ value2 ⇒ id1 ≠ id2`. ✓
3. **Hashed branch**, same kind: id is `wm-anchor-<kind>-<safe>-<digest>` where `digest =
   sha256(ORIGINAL value)[:12]`. Even when `safe` collides (e.g. `HRB/12` and `HRB-12` both →
   `HRB-12`), the digest is of the **original** value, so the ids differ. A collision needs both
   `safe(v1)==safe(v2)` **and** a 48-bit SHA-256 prefix collision — infeasible. ✓
4. **Clean vs hashed**, same kind: a hashed id **always** ends in `-<12 hex>`; a clean id **never**
   does (clause (c) forces any value whose verbatim id would into the hashed branch). Disjoint. ✓

QID/LEI always take the clean branch (no hyphen → cannot match `-<12 hex>`, always FtM-clean), so
`Q42`→`wm-anchor-qid-Q42` and a 20-char LEI stay verbatim and human-readable. The hash only ever lands
on a hostile regNo/taxNo. This is exactly A's final state (its `229c899` introduced the hash-on-sanitise
form; the `34307c3` "CID-5" + CRIT-1 review added clauses (b) and (c) — B adopts the corrected form
directly, skipping A's intermediate non-injective version).

### 3.4 Verified output (installed FtM 4.9.2 — `registry.entity.clean(id) == id` for all)

```text
qid   'Q42'                  -> wm-anchor-qid-Q42                            clean ✓
lei   '5493001KJTIIGC8Y1R12' -> wm-anchor-lei-5493001KJTIIGC8Y1R12          clean ✓
regno 'GOV-9'               -> wm-anchor-regno-GOV-9                        clean ✓ (verbatim, no hash)
regno 'HRB/12-345'          -> wm-anchor-regno-HRB-12-345-70bbb7dfdc72      clean ✓ (hashed)
regno 'HRB-12-345'          -> wm-anchor-regno-HRB-12-345                   clean ✓ (verbatim) — distinct
regno 'HRB/12'             -> wm-anchor-regno-HRB-12-46cbfd39a714          clean ✓
regno 'HRB-12'             -> wm-anchor-regno-HRB-12                       clean ✓ — distinct from above
taxno 'DE.'                -> wm-anchor-taxno-DE.-72bf674218c5             clean ✓ (CID-5 trailing dot)
taxno 'DE-'                -> wm-anchor-taxno-DE--6cb5b656c290             clean ✓ (CID-5 trailing dash)
regno ''                   -> wm-anchor-regno--e3b0c44298fc                clean ✓
regno '-'                  -> wm-anchor-regno---3973e022e932               clean ✓
regno 'a-0123456789ab'     -> wm-anchor-regno-a-0123456789ab-b5c315955f0a  clean ✓ (forced-hash: clause c)
```

All distinct values → distinct ids; all ids are FtM `entity` fixed points.

### 3.5 The ledger parse (`record_durable_id`)

The colon discriminator is gone; replace it:

```python
if durable_id.startswith(_ANCHOR_ID_PREFIX):
    kind, _, value = durable_id.removeprefix(_ANCHOR_ID_PREFIX).partition("-")
    record_canonical(session, durable_id, anchor_kind=kind, anchor_value=value)
elif durable_id.startswith(_MINT_PREFIX):
    record_canonical(session, durable_id, anchor_kind="mint", anchor_value="")
else:
    record_canonical(session, durable_id, anchor_kind="", anchor_value="")
```

`anchor_kind` is exactly the tier kind. `anchor_value` is the **encoded** value (the readable verbatim
value for QID/LEI/clean regNo; the `<safe>-<digest>` form for a hostile value) — an **audit/debug
column only**: no lookup keys on it (`lookup_durable_for_anchor`/`resolve_durable` key on
`canonical_id`/`canonical_alias`, which are the full durable id — format-agnostic). This keeps the fix
in-file. (If the builder wants raw-value audit fidelity for hashed ids, threading the raw value is an
allowed, optional refinement — but NOT required and must not change `pick_anchor`'s signature.)

---

## 4. Invariants preserved (every one is a DENY if broken)

- **Anchor precedence** QID > LEI > regNo > taxNo — unchanged (`_PRECEDENCE` untouched).
- **ADR-0040 anchor-conflict skip** — `len(union) > 1` still falls through; `pick_anchor` never picks
  `[0]`; all-conflict → `None`. The new encoder is reached ONLY on `len(union) == 1`.
- **Re-ingest STABILITY** — `_anchor_id` is a pure deterministic function of `(kind, raw value)`; a
  re-formed anchored cluster with the same anchor yields the **same** durable id (adopt via the ledger
  still hits the same self-row).
- **`canonical_alias` ledger** — append-only; self-row + member aliases + prior-`wmc-` alias all keyed
  on the (now FtM-clean) durable id; idempotent inserts unchanged.
- **`wmc-` / `wm-mint-` separation** — `wmc-` remains the unanchored-merge fallback only (DENY D1: a
  durable id is never derived FROM a hash); `wm-mint-<uuid>` unchanged.
- **G1 provenance on every node AND edge** — the whole point: edges stop dropping, so the
  provenance-bearing edge is actually written. No prov_* touched.
- **Append-only / no un-merge** and **canonical-canonical only via the guard** — untouched (no
  clustering, threshold, Splink, or guard change).

---

## 5. Migration conclusion — **NONE needed; do NOT stop the human**

- The change is **format-in-code only**. The `canonical_id_ledger` columns
  (`canonical_id`/`canonical_alias`/`anchor_kind`/`anchor_value`) are strings — **no Alembic/schema
  change**; `tests/integration/test_migrations.py` (ADR 0030 drift guard) stays green untouched.
- **No production data to migrate.** ADR 0044 is still **PROPOSED** (not operational); B is dev-phase,
  single-tenant, with **no production graph or ledger**. Integration tests create the ledger fresh.
- **Stale `qid:`-form rows in a dev Postgres are benign and need no migration.** Any such row was
  written by the buggy code, whose nodes had their edges *dropped at write time* — i.e. it never
  represented valid graph data. A re-ingest simply mints the new-format self-row; the orphaned
  `qid:Q42` self-row is never looked up again (lookups go via the new-format anchor id). A developer
  who wants a clean slate drops their local graph/ledger volume. This is a NOTE, not a blocker.

**Conclusion: no data migration of existing `qid:`-form ledger rows is required → no STOP.** If the
builder discovers a persisted, *production-relevant* ledger with `qid:`-form rows that must be
preserved, THAT would change this conclusion → STOP and flag the human.

---

## 6. Person-affecting assessment + human sign-off

- The id-format change is **person-NEUTRAL**: it does not change who-merges-with-whom (precedence +
  ADR-0040 conflict guard + 0.92 threshold + Splink all untouched). It only makes the durable id a
  valid FtM edge endpoint.
- **BUT injectivity is the person-safety property.** A *non-injective* id (two distinct real entities
  → one id) is a silent cross-entity merge the catastrophic-merge guard never sees — exactly A's
  CRIT-1. The fix *preserves* injectivity (§3.3) and the conflict guard, so the **net** change is
  person-neutral.
- **Human sign-off: NOT required**, *conditional on the injectivity + FtM-clean guard tests being
  green*. These tests are the safety net (mirrors A, which fixed this autonomously under its ADR 0048
  with the property test as the regression guard). The DENY criteria (§8) make a non-injective or
  non-FtM-clean id a hard fail.

---

## 7. Failing-first test plan

### 7.1 THE oracle — integration (Neo4j), the test whose absence hid the bug — **RED on `master`**
Add `test_anchored_merge_edge_survives_durable_rekey` to
`tests/integration/test_stable_id_graph.py` (or a new `tests/integration/test_anchor_edge_write.py`):
1. Build a 2-member cluster that earns a **QID anchor** (so the durable id is anchor-derived), resolve
   + promote it through the real pipeline so it is re-keyed under the durable id and written.
2. Write an **Ownership** (or Directorship) edge whose endpoint names a merged-away member id (so
   referent rewriting points it at the durable id).
3. Assert the edge is **NON-EMPTY**: `get_neighbors(durable_id)` returns the counterparty AND/OR a
   direct Cypher `MATCH (a {id:$durable})-[r]-(b) RETURN count(r)` is `>= 1`.
- **Pre-fix:** endpoint cleans to `None` → edge dropped → assertion fails (RED). **Post-fix:** GREEN.
- The expected durable id is `wm-anchor-qid-<QID>` (FtM-clean), so the node and its edge both persist.

### 7.2 THE unit GUARD — pure, Docker-free (in `tests/unit/test_canonical.py`)
Drive `_anchor_id` (and `pick_anchor` end-to-end where the tier accepts the value) over an adversarial
value class, asserting **both** properties:

- **FtM-clean-through:** for every kind ∈ {qid, lei, regno, taxno} and every value in
  `{'', '.', '-', 'ABC.', 'ABC-', 'HRB/12-345', 'HRB:12', 'HRB 12 345', 'a-0123456789ab', a real QID,
  a real 20-char LEI}`: `registry.entity.clean(id) == id`.
- **Injectivity:** over a set of **distinct** raw values mapped through the same kind, all ids are
  distinct. MUST include the **constructed traps** (random text misses them):
  - sanitisation-collision twins: `'HRB/12'` vs `'HRB-12'` → distinct ids;
  - hash-tail twin: a clean value whose verbatim id would end in `-<12 hex>` (e.g.
    `'a-0123456789ab'`) vs a hostile value sanitising to the same `<safe>` → distinct ids, and the
    clean value is forced into the hashed namespace (its id ends in `-<12 hex>`).
- A focused **CID-5 regression**: `_anchor_id('regno','ABC.')` and `_anchor_id('taxno','ABC-')` both
  clean unchanged (the trailing-punctuation class that A's first fix missed).

### 7.3 FROZEN regressions — must stay GREEN, byte-for-byte (no colon-format dependency)
- `tests/unit/test_resolution_canonical_id.py` (ADR 0036 `wmc-` determinism)
- `tests/unit/test_resolution_anchor_conflict.py` (ADR 0040 scoring/park)
- all other `tests/unit/test_resolution_*.py` and the resolution/sign-off integration suites
  (`test_resolution_pipeline.py`, `test_resolution_batching.py`, `test_signoff.py`,
  `test_b1_*`, `test_b6_*`, `test_migrations.py`).
- **The ADR-0040 conflict-skip regression in `test_canonical.py`/`test_stable_id.py` stays
  GREEN** — only its *expected id string* updates (a conflict still yields `None` or falls through to
  the next tier; that behaviour is asserted unchanged).

### 7.4 UPDATED assertions (old colon → new format) in the §2 "UPDATED" test files
Mechanical: `f"qid:{Q}"`→`f"wm-anchor-qid-{Q}"`, `f"lei:{L}"`→`f"wm-anchor-lei-{L}"`,
`"regno:R1"`→`"wm-anchor-regno-R1"`, `"taxno:T1"`→`"wm-anchor-taxno-T1"`,
`"regno:12345"`/`"regno:GOV-9"` likewise (both stay verbatim — clean, no hash). In
`tests/test_stable_id.py:164` the `anchor.partition(":")` parse becomes
`anchor.removeprefix("wm-anchor-").partition("-")`; `startswith("qid:")` checks become
`startswith("wm-anchor-qid-")`. `tests/test_abstract_edge.py:210`
`"lei:5493001KJTIIGC8Y1R12"`→`"wm-anchor-lei-5493001KJTIIGC8Y1R12"`. The re-ingest-stability assertion
(`first == again == <id>`) stays — only the literal updates.

---

## 8. APPROVE / DENY criteria

**APPROVE iff ALL hold:**
1. The §7.1 integration edge-survival oracle exists, was RED on `master`, and is GREEN post-fix.
2. The §7.2 unit guard exists and proves, for every kind + the full adversarial class:
   `registry.entity.clean(id) == id` **AND** injectivity over distinct values (incl. the hash-twin and
   sanitisation-collision constructed pairs).
3. Precedence, the ADR-0040 conflict skip, re-ingest stability, the ledger, and the `wmc-`/`wm-mint-`
   separation are all preserved (§4) and their frozen tests (§7.3) pass byte-for-byte.
4. The diff is confined to §2 scope (production change ONLY in `canonical.py`).
5. No Alembic/schema/migration change (§5).

**DENY (hard) if ANY:**
- **D-INJ** — any id construction that can map two distinct raw `(kind,value)` pairs to one id (a
  non-injective sanitize), or a guard test that does not actually construct the collision traps.
- **D-CLEAN** — any durable id for any adversarial value where `registry.entity.clean(id) != id`
  (incl. the trailing-punctuation/empty CID-5 class).
- **D-EDGE** — the §7.1 oracle is absent, or does not assert the edge non-empty, or was never RED.
- **D-SCOPE** — a production edit outside `canonical.py` (esp. `writer.py`/`referents.py`): a scope
  signal the design forbids → STOP.
- **D-INV** — precedence / ADR-0040 skip / re-ingest stability / ledger / `wmc-`-separation regressed,
  or any frozen test (§7.3) altered/skipped/loosened.
- **D-MIG** — an unflagged migration, or a `qid:`-row data migration done silently instead of flagged.

---

## 9. Slice plan — **ONE slice**

`slice-1` (person-NEUTRAL; `human_fork = false`): the FtM-clean injective encoder + the
`record_durable_id` parse change in `canonical.py`; the §7.1 integration oracle + the §7.2 unit guard
(failing-first); the §7.4 mechanical assertion updates. Individually mergeable; CI-green required. The
fix is small and cohesive — no benefit to splitting. (Test-author MAY land the failing tests in the
same PR as the fix, since the RED demonstration is captured against `master`/`HEAD` pre-fix.)
