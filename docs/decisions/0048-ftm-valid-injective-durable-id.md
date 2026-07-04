# 0048 — FtM-valid, injective durable canonical id (Gate CID-fix)

- **Status:** ACCEPTED
- **Date:** 2026-06-26
- **Gate:** CID-fix (`docs/reviews/GATE_CID_FIX_SPEC.md`) — a focused BUG-FIX gate off `master`.
- **Touches:** `resolution/canonical.py` (`pick_anchor` id serialization + `record_durable_id` parse +
  a pure `_anchor_id` helper). Tests in `tests/`. No schema/migration.
- **Refines:** [0044](0044-anchor-preferred-stable-ids.md) — **only** its durable-id *serialization*
  clause (`§Decision.2`: the `qid:`/`lei:`/`regno:`/`taxno:` scheme) and the open Builder-record item
  "Final durable-id serialization". Does **NOT** relitigate 0044's precedence, ledger, adopt/merge/split,
  or the [0040](0040-er-anchor-conflict-negative-evidence.md) anchor-conflict guard.
- **Preserves (does NOT relitigate):** [0036](0036-deterministic-canonical-id.md) (`wmc-` idempotency
  fingerprint), [0039](0039-er-distinguishing-evidence.md) (regNo/taxNo `identifier` normalisation),
  [0040](0040-er-anchor-conflict-negative-evidence.md) (anchor-conflict skip),
  [0025](0025-referent-rewriting.md) (referent rewriting), [0042](0042-single-tenancy-teardown.md)
  (native `{id}` MERGE), `DEFAULT_MERGE_THRESHOLD=0.92` + Splink weights + `cluster_and_merge` membership.
- **Cross-line:** Workflow A (the human build line) hit this **same** bug independently: its edge-write
  fix `229c899` and the metamorphic-harness catch `34307c3` ("CID-5" + the CRIT-1 injectivity review,
  A's ADR 0048). B's decision re-derives A's *corrected* design — not its intermediate non-injective form.

## Context

ADR 0044 made a cluster's durable canonical id **anchor-preferred** and left the exact serialization to
its Builder record. The shipped serialization (`canonical.py:143`) was
`f"{tier.kind}:{value}"` → `qid:Q42` / `lei:<20>` / `regno:<v>` / `taxno:<v>`.

A cross-line audit reproduced a **confirmed blocker** on installed `followthemoney==4.9.2`: the **colon**
is not in FtM's entity-reference charset (`[A-Za-z0-9.-]`), so `registry.entity.clean('qid:Q42') is None`.
This durable id is not only the graph node id — it is **rewritten into edge ENDPOINTS** by referent
rewriting (`referents.py` → `pipeline.py`). FtM cleans entity-typed property values through
`registry.entity`, so an endpoint of `qid:Q42` cleans to `None` and the **edge silently drops**. Net
effect: every entity that earns an anchor-preferred id (anything with a
`wikidataId`/`leiCode`/`registrationNumber`/`taxNumber` — a large fraction of real entities) loses its
graph edges. This corrupts **the product** (the resolved entity graph). B's unit/integration suite stayed
green because it asserted the durable id *string* and the node, but never exercised an anchor-derived id
as an **edge endpoint**.

Two further hazards make a naive fix unsafe, both reproduced on 4.9.2:
- **Injectivity (CRIT-1, the person-safety hazard).** regNo/taxNo values are scraped/hostile;
  `registry.identifier.clean` keeps `/`, `:`, spaces. A naive strip-to-hyphen sanitize maps the
  *distinct* values `HRB/12` and `HRB-12` to the *same* `HRB-12` — a silent cross-entity merge the
  catastrophic-merge guard never sees (it inspects clustering, not id minting).
- **Trailing punctuation (CID-5).** An already-`[A-Za-z0-9.-]` value ending in `.`/`-` (or empty after
  sanitize) yields an id that `registry.entity.clean` rejects — the edge still drops.

## Decision

The durable id is an **FtM-clean, injective** entity reference:

```
wm-anchor-<kind>-<encoded-value>   kind ∈ {qid, lei, regno, taxno}
wm-mint-<uuid4>                    (unanchored cluster — unchanged)
wmc-<sha256[:40]>                  (unanchored-merge idempotency fingerprint — unchanged, ADR 0036)
```

The kind tokens are already FtM-clean (no field→code map needed, unlike A). The value is encoded by a
pure helper that partitions the id-space into **two provably disjoint namespaces** along the trailing
`-<12 hex>` SHA-256-tail shape:

1. `safe = re.sub(r"[^A-Za-z0-9.-]", "-", value)`; `candidate = f"wm-anchor-{kind}-{safe}"`.
2. Append `-<sha256(ORIGINAL value)[:12]>` **iff** `safe != value` **or**
   `registry.entity.clean(candidate) != candidate` **or** `candidate` already ends in `-<12 hex>`.

**Injectivity holds** because: different kinds have disjoint prefixes; a clean id embeds the value
verbatim; a hashed id disambiguates on the digest of the *original* value (so sanitisation-collisions
still differ); and clauses (b)+(c) keep the clean and hashed namespaces provably disjoint (a hashed id
always ends in `-<12 hex>`, a clean id never does). **FtM-validity holds** because every output is built
from `[A-Za-z0-9.-]` and is verified a `registry.entity.clean` fixed point across the adversarial value
class (`''`, `'.'`, `'-'`, trailing punctuation, embedded `/`/`:`/space, hash-twins). QID/LEI values are
already clean, so they stay verbatim and human-readable (`wm-anchor-qid-Q42`); only hostile regNo/taxNo
ever hash.

`record_durable_id` parses the anchor kind from the `wm-anchor-<kind>-` prefix (the colon discriminator
is gone); `anchor_value` is the encoded value (an **audit column only** — no lookup keys on it).

This is the explicit answer to ADR 0044's open Builder-record item and supersedes its illustrative
`qid:`/`lei:` serialization.

## Alternatives considered

- **Naive sanitize (strip non-`[A-Za-z0-9.-]` to `-`), no hash.** Rejected — **non-injective**: `HRB/12`
  and `HRB-12` collide → a silent cross-entity merge. This is the CRIT-1 hazard.
- **Hash only when sanitisation changed the value (A's first form, `229c899`).** Rejected — misses the
  CID-5 trailing-punctuation/empty class (an already-safe value ending in `.`/`-` still fails
  `registry.entity.clean`) AND leaves the clean id `<code>-A-B` reachable by a degenerate value whose
  hashed id is `<code>-A-B` (the non-injectivity A's metamorphic harness caught). The chosen rule adds
  the FtM-clean and hash-tail clauses.
- **Hash the whole id always (opaque `wm-anchor-<kind>-<digest>`).** Rejected — needlessly throws away the
  readability/debuggability of clean QID/LEI ids and the existing 0044 expectation that an anchored id is
  legible; provides no extra safety over the disjoint-namespace rule.
- **Keep the colon and strip it only in the writer/referents.** Rejected — spreads the format contract
  across `canonical.py` + `writer.py` + `referents.py`, invites drift, and the durable id is also the
  ledger key / audit `canonical_id`; the single seam is `canonical.py`.
- **Percent-/base32-encode the value reversibly.** Rejected — longer, less legible for the common clean
  case, and the disjoint-namespace + digest rule already gives injectivity with a verbatim clean branch.

## Consequences

- ✅ Edges whose endpoints are anchor-derived ids **survive the write** — G1 (provenance on every node
  AND edge) is actually upheld for anchored entities; the product stops silently losing edges.
- ✅ Injectivity is preserved and now **property-tested** with constructed hash-twin /
  sanitisation-collision pairs — no anchor minting can back-door a cross-entity merge.
- ✅ Re-ingest stability preserved: `_anchor_id` is a pure deterministic function of `(kind, raw value)`.
- ✅ Person-neutral: precedence, the ADR-0040 conflict skip, the 0.92 threshold, and Splink are
  untouched. No human sign-off required (conditional on the injectivity + FtM-clean guard tests green —
  these are the safety net; D-INJ/D-CLEAN are hard DENYs).
- ✅ No schema/migration: the change is format-in-code; ledger columns are strings; B is dev-phase with
  no production graph/ledger. Stale `qid:`-form dev rows are benign (their edges were already dropped).
- ⚠️ Hostile regNo/taxNo ids carry a `-<12 hex>` digest tail and are less legible than a verbatim value;
  the audit `anchor_value` for a hashed id is the encoded (not raw) value unless the builder optionally
  threads the raw value through. Accepted — these are hostile scraped ids, and injectivity outranks
  legibility for them.

## Relationship to other ADRs

- **Refines ADR 0044** (durable-id serialization clause only). Closes its Builder-record serialization
  item. ADR 0044's precedence / ledger / adopt-merge-split / person-affecting fencing are unchanged.
- **Preserves ADR 0040** (anchor-conflict skip), **0036** (`wmc-` fingerprint), **0039** (regNo/taxNo
  normalisation), **0025** (referent rewriting), **0042** (native `{id}` MERGE).
- **Independent confirmation:** Workflow A reached the identical conclusion (A's ADR 0048 / commits
  `229c899` + `34307c3`); the two build lines converged on the FtM-valid + disjoint-namespace-injective
  design from opposite directions (A via a metamorphic harness, B via a cross-line audit).
