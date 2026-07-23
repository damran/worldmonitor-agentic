# 0120 — Ransomware.live victim/group connector (first edge-emitting `map()`; UnknownLink over Event)

- **Status:** PROPOSED
- **Date:** 2026-07-23
- **human_fork:** false — reversible additive connector (full reversal cost + revisit triggers below).
- **person_affecting:** false — the connector emits only candidates-with-provenance and changes NO
  ER threshold, guard mode, or individual-affecting score; the emitted entities are Organizations /
  Companies (never `Person`); group Orgs carry `crime.cyber` (sensitive ⇒ merges park), and the
  allegation rides a non-matchable `UnknownLink` edge (reliability `"E"`), never a topic/score on the
  victim Company node. Full argument + the allegation-grade nuance in the Classification note.
- **human_cosign:** PENDING — offered at the gate-completing PR (operator: register a free PRO key at
  `https://my.ransomware.live` + confirm interim personal-tier posture; countersign this
  classification). ER-adjacent, so cosign is offered per the 0117/0118/0119 precedent.

## Context

The OG-harvest backlog's **S-4** lane (`docs/fable-review/91_OG_HARVEST_BACKLOG.md`) brings
Ransomware.live: a free inventory of ransomware leak-site claims (groups → claimed victims). It fits
the CTI persona (ADR 0094 D4) and the resolved-graph product — victim Companies resolve against
OpenCorporates/OpenSanctions, groups against the ATT&CK actor substrate (ADR 0117). Unlike the S-2
IOC lane (infrastructure indicators), this data is **relational** (a group *claims* a victim) and
**allegation-grade** (unverified criminal self-declarations, per the operator's own disclaimer).

Two modeling questions and one licensing question had to be settled. Feasibility was verified
first-hand against the installed FollowTheMoney model and the live v2 endpoints (2026-07-23; buildable
spec: `docs/reviews/GATE_S4_RANSOMWARE_LIVE_SPEC.md`).

## Decision

1. **Model the victimization as an FtM `UnknownLink` edge, not a `wm:Event` node.** The backlog's
   `wm:Event` sketch is superseded: the locked ontology rule is *"wm: extensions only where FtM
   can't reach"*, and FtM reaches this domain natively. `UnknownLink` (verified: `edge=True`,
   `matchable=false`, `subject`/`object` range `Thing`, carries `role`/`date`/`startDate`/`summary`/
   `sourceUrl`) is FtM's designated catch-all directed edge — a clean fit for "group —[claimed
   victim]→ company". `subject` = the group Organization, `object` = the victim Company,
   `role="ransomware victim (claimed by group)"`, `date`=`attackdate`, `sourceUrl`=`claim_url`,
   `summary`=`description` (placeholders dropped). **This is the codebase's first edge-emitting
   `map()`** — provenance is stamped on the edge as well as both nodes (G1 on every node AND edge).
2. **`crime.cyber` topic on every group Organization** (verified valid + sensitive). Victim Companies
   **never** carry a risk topic. Group merges therefore park for human review; the victim's clean
   node resolves normally and the disputed claim stays on the disclaimed edge (leads-not-verdicts).
3. **Fixed low reliability `"E"` (unreliable — criminal self-declaration)** stamped on every entity
   AND the edge, threaded seed-side (`SeedSpec.reliability="E"` → `ConnectorInstance.reliability` →
   the driver → `run_ingest`). Never `"B"`.
4. **Deterministic connector-minted ids** from source natural keys (spec §3.1): readable
   `ransomware-live-group-<slug>` (mitre/opencorporates style; `_slug`=strip-non-alnum+lower —
   the only normalization that converges the group's three observed name forms), hashed
   `ransomware-live-victim-<sha1(permalink)>` and `ransomware-live-claim-<sha1(subject+object+key)>`
   (indicator_id rationale for opaque keys). L3 owns dedupe.
5. **Free v2, no key, PRO-ready config.** Build against `https://api.ransomware.live/v2`
   (unauthenticated, 200 anonymous) with single-request-per-`collect()` under the global 1 h cadence
   (satisfies the 1 req/min/endpoint ceiling 60×). `config.schema.json` is PRO-ready (`url` override
   + optional `api_key` `"secret": true`) so an operator can move to PRO with no code change.

### Licensing / T&C determination (COMPATIBLE-WITH-CONDITIONS)

The GitHub Unlicense covers the **site code only**, not the **dataset** (no CC0/CC-BY/ODbL exists for
the data). Obligations we comply with, for a **self-hosted, single-tenant, non-commercial,
pull-only** deployment: **commercial use is prohibited** without the Publisher's express permission
(French legal notice, disclaimer page — informal translation; a native/legal read is a revisit
trigger before any public-facing exposure); **the free tier is "personal use only"** (⇒ the operator
TODO to register a PRO key and confirm interim posture); **no redistribution of the feed**, **no
re-hosting screenshots** (our design emits derived FtM entities + a provenance pointer back, never
mirrors raw or screenshots — keep it that way); attribution is not contractually required but we
stamp `source_id`/URL provenance per our own invariant; **the data is explicitly unverified**
("only an inventory of claims … the sole responsibility of their authors") ⇒ the hard requirement
for the fixed `"E"` grade + leads-not-verdicts. The full PRO T&C document was not retrievable (SPA)
— its re-review is a revisit trigger.

## Consequences

- The graph gains its first **relational CTI layer** and its first **edge-emitting connector**.
  Group Organizations converge across the two datasets (thin victim-side + rich groups-side) by
  deterministic id; the victim Company resolves against real company registries via L3; the
  `UnknownLink` edge records the disputed claim with full provenance.
- **First edge through resolution** (designated build-time verification): the `UnknownLink` edge is
  `matchable:false` ⇒ ADR 0119's `score_pairs` pre-filter excludes it from Splink (verified), so it
  becomes a schema-safe singleton and is projected by the writer/ftmg. The projector path is
  unproven for edges in this codebase — the gate STOPS and escalates if the writer cannot yet emit
  edges rather than forcing it (spec §9/§10).
- **Reliability-column gap closed.** `ConnectorInstance.reliability` existed in neither the ORM nor
  any migration (migration 0009's `reliability` is on the `statement` table; the dossier's "declared
  in the ORM" claim was wrong — checker-verified); slice 1 adds the ORM field, the migration, the
  seed field, and threads the value through the driver. Every other connector's `"B"` default is
  byte-identical (`None → "B"`).
- **Sensitivity posture is deliberate.** `crime.cyber` makes every group Org sensitive so group
  merges (incl. `_slug` collisions) park for review; victims never get a risk topic, so a victim
  resolving into a real company does not tag that company with an allegation.
- **Explicit rejections (do NOT build):** the `wm:Event` node; PRO endpoints; monthly/bulk/RSS
  backfill; screenshot fetch/re-host; `ttps`→ATT&CK linking; geo enrichment; connector-side dedupe;
  any `indicates`/actor-attribution edge.

## Revisit triggers + reversal costs

- **Reversal (human_fork: false).** Remove the `ransomware_live` package + its two seed rows; drop
  the `connector_instance.reliability` column (slice-1 migration `downgrade()`) and revert the
  `SeedSpec`/`driver.py` threading; already-written entities remain inert provenance-stamped data
  (prunable by the `ransomware_live` dataset tag). No FtM-core schema, ER threshold, or store-shape
  change. **Reversal cost:** low.
- **Free v2 returns 401/403 / personal-tier posture changes.** STOP and flag the human; adopt the
  PRO surface (`api-pro.ransomware.live` + confirmed `X-API-KEY` + real PRO T&C) as its own gate.
  **Reversal cost:** low — the config is already PRO-ready; no minted-id or schema change.
- **PRO T&C re-review** (document was unretrievable) and **French legal-notice native/legal
  confirmation** — required before any public-facing exposure of derivatives; use stays
  non-commercial regardless. **Reversal cost:** none technical.
- **UnknownLink-vs-Event modeling** (reversible — additive remap). If analysts need a first-class
  event node (multiple participants, timeline), remap to the native FtM `Event` node in a later
  slice. **Reversal cost:** moderate — a third entity-id scheme + re-attribution of the existing
  edges; already-written edges remain valid. Recommended default stays `UnknownLink` (lighter; the
  Event node adds ambiguous attacker/victim roles via `involved`/`organizer`).
- **`groups[].locations[].slug` → `website`** proves noisy (onion DLS ≠ a website). Move to
  raw-only or a different prop. **Reversal cost:** trivial (one mapper line; property re-projection).
- **`_slug` false-collision observed** (two distinct groups fold to one id). Tighten the
  normalization. **Reversal cost:** low; the collision parks for human review today (both
  `crime.cyber`), never a silent merge.
- **`operator_run.py` reliability residual** — manual REST-triggered runs stamp `"B"` (the live
  driver stamps `"E"`). Thread it too if manual runs of this source become routine. **Reversal
  cost:** trivial (mirror the driver's read-side).

## Classification note

`human_fork: false` + `person_affecting: false`. Reversible defaults pre-decided by the main loop per
CLAUDE.md's "classify every ADR decision by reversibility" rule; recorded with reversal cost + revisit
triggers rather than manufacturing a human fork.

**`person_affecting: false` — full argument (allegation-grade nuance foregrounded):** (1) the
connector emits only **candidates-with-provenance**; it changes **no** ER threshold, guard mode, or
individual-affecting score (the platform's `DEFAULT_MERGE_THRESHOLD` and catastrophic-merge guard are
untouched). (2) The emitted entities are FtM **Organizations / Companies** (org-level), never
`Person`; the group Org carries `topics=crime.cyber` ⇒ `guard.sensitivity.is_sensitive` is `True`
(verified — clause (b), `crime` ∈ `registry.topic.RISKS`), so any cluster containing a group parks for
human review before any auto-promote. (3) The allegation is carried on a **non-matchable
`UnknownLink` edge** (`role="ransomware victim (claimed by group)"`, reliability `"E"`), **never as a
topic or score on the victim Company node** — the company node stays clean and resolves normally; the
disputed claim lives on a disclaimed edge. (4) The edge is `matchable: false` (verified) ⇒ ADR 0119's
`score_pairs` matchable pre-filter excludes it from Splink, so it can never fuzzy-fuse. The
canonical-id/merge invariant surface is touched (a new matchable Company/Org lane + the first edge),
so the mandatory `@given` property suite applies (CLAUDE.md). Cosign is offered at the gate PR because
the data is person/org-adjacent even though the mechanism is not person-affecting (0118/0119 precedent).
