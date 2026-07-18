# 0118 — `wm:Indicator`: the first L2 extension + the Feodo Tracker connector

- **Status:** ACCEPTED (2026-07-18)
- **Date:** 2026-07-18
- **human_fork:** false — reversible, operator-directed in-session (the "Build wm:Indicator
  lane" ruling, 2026-07-18). Reversal: remove the wm schema + the connector + the seed row;
  already-written Indicator nodes remain inert provenance-stamped data (or are prunable by
  dataset). No FtM-core schema, threshold, or store shape changes.
- **person_affecting:** false — infrastructure indicators (C2 IPs), not people. The schema is
  **non-matchable by construction** (an explicit `matchable: false` in the schema — FtM's default is True; the gate's VERIFIED_API.md entry records the corrected probe), so
  Indicators can never enter fuzzy resolution or influence any person/org merge decision;
  identity is deterministic-id-only. Cosign offered to the operator at the gate PR
  (S-3/ADR 0117 precedent for L2/ER-adjacent classifications).

## Context

CTI is a first-class plugin domain (CLAUDE.md), the persona is CTI/L3 SOC (ADR 0094 D4), and
the sanctioned harvest backlog's S-2 lane (91) brings abuse.ch C2 indicators. FtM 4.x has no
IOC-shaped type, and the locked ontology decision anticipates exactly this: *"wm: extensions
only where FtM can't reach — each additive, each with an ADR."* This is the first such
extension. Feasibility was probed first-hand on the pinned followthemoney==4.9.2: schema
injection into the global model works, entities round-trip; the schema declares `matchable: false` explicitly (FtM defaults the flag
to True — corrected during the build and recorded in VERIFIED_API.md).

## Decision

1. **`wm:Indicator`** — a vendored, reviewable YAML under `ontology/schema/wm/`, injected into
   the GLOBAL FtM model at ontology import (`register_wm_schemata()`, idempotent), so the
   writer/resolver/validators all see it with zero env plumbing. Properties: value, type,
   malware family, first/last seen, and a dormant `indicates → Organization` edge for the
   future actor-attribution lane (S-2 phase 2 / ThreatFox).
2. **Non-matchable is load-bearing:** Indicators converge by deterministic id only
   (`feodo-<sha1(ip:port)>`); the property suite pins that an Indicator can never merge with
   an Organization/Person and that the schema stays non-matchable.
3. **`feodo` connector** (EXTERNAL_IMPORT/passive, mitre_attack package shape): the
   unauthenticated Feodo ipblocklist JSON → one provenance-stamped Indicator per C2 entry
   (reliability "B"); no topics, no country property (ASN geo is not an event location — off
   the globe by design). Seeded enabled. abuse.ch community data is used within its fair-use
   terms (pull-only, self-hosted analysis, no redistribution).

## Consequences

- The graph gains its first CTI-native layer; entity search/panel surface Indicators
  immediately; the ATT&CK actor substrate (ADR 0117) is the designated target of the dormant
  `indicates` edge.
- **Checker finding (2026-07-19, carried forward as a HARD phase-2 precondition):**
  `matchable: false` is honored by FtM/nomenklatura's native matcher but is INERT in our Splink
  scoring path — the load-bearing never-fuse-with-person/org mechanism is `extends: [Thing]` /
  `common_schema` refusal (independently verified through the real `cluster_and_merge`:
  cross-schema members dead-letter as `merge_incompatible`). Indicator↔Indicator identity is
  id-only TODAY solely because feodo's deterministic `feodo-<sha1(ip:port)>` makes
  same-value ⟹ same-id; a sibling Indicator connector with a divergent id scheme would let
  Splink fuzzy-fuse same-IOC records. Not reachable in this feodo-only slice; not
  person-affecting.
- **Revisit triggers:** (a) S-2 phase 2 (ThreatFox/URLhaus/SSLBL siblings + actor edges) MUST
  FIRST make Indicator identity connector-independent — a shared IOC-value id scheme across all
  Indicator connectors, or `score_pairs` consulting `schema.matchable` — per the finding above;
  (b) any need for `matchable: true` on Indicators (would require its own ADR + guard
  analysis — do NOT flip it casually); (c) volume growth → per-dataset retention policy;
  (d) the ftm-schema vendoring gate learning to cover wm schemas too (today it guards the
  FtM core only; wm YAMLs are ordinary reviewed repo files).
- **Residual:** the schema-injection relies on `Model.schemata`/`generate()` internals of the
  EXACTLY-pinned FtM (ADR 0098); any FtM version bump must re-verify the injection probe
  (added to VERIFIED_API.md).
