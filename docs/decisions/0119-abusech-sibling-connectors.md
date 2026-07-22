# 0119 — abuse.ch sibling IOC connectors (ThreatFox / URLhaus / SSLBL) + the Splink `matchable` gate

- **Status:** ACCEPTED (2026-07-22)
- **Date:** 2026-07-22
- **human_cosign:** PENDING — offered to the operator at this gate PR (#217-to-be), the slice-D
  merge that flips this ADR to ACCEPTED; the classification substance (`human_fork: false`,
  `person_affecting: false`) is covered by ADR 0118's cosign precedent (the `matchable` finding
  this gate executes), to be countersigned by the operator.
- **human_fork:** false — reversible. The three connectors are additive plugins (disable/remove =
  reversal); Indicator nodes are prunable by `datasets` tag (`threatfox`/`urlhaus`/`sslbl`). The
  slice-A `score_pairs` change only SHRINKS the fuzzy-match surface. Reversal cost: drop the three
  packages + their three seed rows; revert the one-line `score_pairs` filter; already-written
  Indicator nodes remain inert provenance-stamped data. No FtM-core schema, threshold, or store
  shape changes. Revisit triggers below.
- **person_affecting:** false — argued precisely: (1) the connectors emit only `wm:Indicator`
  nodes (infrastructure IOCs — C2 IPs, malicious URLs, cert fingerprints), never people, never
  edges to people. (2) Slice A can only REMOVE candidate pairs that involve a non-matchable
  entity; it can never CREATE a pair, nor change the `probability` of any pair between two matchable
  (Person/Organization) entities — `prior`/m/u are fixed constants, not corpus-size-derived, so
  non-matchable entities never perturb the matchable results (pinned by the non-interference
  property test). Therefore no person's merge outcome, ER threshold, guard mode, or
  individual-affecting score changes. The canonical-id/merge invariant surface is touched
  (defense-in-depth on the merge path), so the mandatory `@given` property suite applies (CLAUDE.md).

## Context

ADR 0118 landed `wm:Indicator` + the Feodo Tracker connector and carried, as an explicit HARD
phase-2 precondition, the checker finding that `matchable: false` is honored by FtM/nomenklatura's
native matcher but is INERT in our Splink `score_pairs` path. S-2b (ADR 0118 revisit-trigger (a),
executed early, PR #211) discharged half of it — the connector-independent `ontology.ioc.indicator_id`
scheme means same-value ⟹ same-id, so Indicator↔Indicator convergence is id-only TODAY. The OTHER
half — the systemic `score_pairs`-consults-`schema.matchable` defense-in-depth — stayed OPEN. It
becomes load-bearing the moment a SECOND Indicator connector lands: this gate lands three.

The OG-harvest backlog's S-2 lane (`91`) brings the abuse.ch sibling feeds. Verified live research
(2026-07-21, 63 CONFIRMED / 6 corrected; `scratchpad/research_summary.md`) establishes: ThreatFox,
URLhaus, and SSLBL all still serve **anonymous** bulk exports despite abuse.ch's 2025-06-30
mandatory-auth transition (which IS enforced on the query APIs — 401 without a key); the legacy anon
export URLs are de-documented and therefore at deprecation risk. ADR 0117's revisit trigger ("the
S-2 IOC lane landing") is the reason the ATT&CK actor substrate exists — but the deterministic chain
from an IOC ends at the malware family, so this gate deliberately stops there.

## Decision

Build three connectors (buildable-spec: `GATE_S2P2_ABUSECH_SIBLINGS_SPEC.md`) + one ER fix, one
independently-mergeable PR per slice (A→B→C→D):

1. **Slice A — `score_pairs` consults `schema.matchable` FIRST.** Entities whose schema is
   non-matchable are filtered BEFORE frame construction — they never enter Splink's blocking,
   linker, or `predict`; the `< 2 → []` short-circuit applies to the matchable subset. The existing
   post-`predict` `_schema_compatible` guard stays (defense in depth for transitive/sibling clashes
   among matchable schemas). This executes ADR 0118 revisit-trigger (a); ADR 0118's text is amended
   in place to record execution (precedent: the S-2b "EXECUTED EARLY" note) — **not a new ADR**.
2. **Slice B — `threatfox`** (EXTERNAL_IMPORT/PASSIVE): default = the legacy anon
   `export/json/recent/` (48 h, ~4.1k IOCs); optional `auth_key` (`secret: true`) sent as the
   `Auth-Key` header; a 401/403 fails loud with an actionable message. Because this slice
   introduces the secret header, it also adds `auth-key` to `guarded_stream`'s
   `_SENSITIVE_HEADERS` (G-NET-1, ADR 0087) so the key is stripped on any cross-host redirect. Map: one `wm:Indicator` per
   record via `indicator_id(ioc_value)`, traversing the `{id:[record,...]}` bulk shape;
   `ioc_type→indicatorType` per the shared vocabulary (`ip:port`→`ipv4` **exactly as feodo**, so the
   same C2 IP converges); `malwareFamily` from `malware_printable` (skip `"unknown"`); first/last
   seen from `first_seen_utc`/`last_seen_utc`. **No `indicates` edges.**
3. **Slice C — `urlhaus`** (EXTERNAL_IMPORT/PASSIVE): default = anon `downloads/json_recent/`
   (30-day, ~20.5k, ~10.8 MB, under the 16 MiB cap — asserted); value = `url`, type `url`;
   firstSeenAt = `dateadded` (strip ` UTC`), lastSeenAt = `last_online`; **no `malwareFamily`**
   (`tags`/`threat` are not a reliable family signal — recorded).
4. **Slice D — `sslbl`** (EXTERNAL_IMPORT/PASSIVE): default = anon **CC0** `sslblacklist.csv`
   (`Listingdate,SHA1,Listingreason`, `#` comments, unquoted); value = the SHA1 cert fingerprint,
   type `sha1_cert` (distinct from a file `sha1`); `malwareFamily` parsed from `Listingreason` per
   the verified nuances (strip trailing ` C&C` only when the remainder is a real family, excluding
   generic `Malware`; handle `<Family> malware distribution`; when in doubt, no family); firstSeenAt
   = `Listingdate`; no lastSeenAt.

All three seeded **enabled**, `category="cti"`, `url` spelled out explicitly in the SeedSpec, no
`auth_key` in seed config; they inherit the global 1 h ingest cadence (≥ the 5-min etiquette floor).

## Consequences

- The graph's CTI-native layer grows from Feodo-only to four converging IOC feeds; the same
  real-world indicator from multiple sources resolves to ONE node by deterministic id, provenance
  and `datasets` unioning on the fold — never by fuzzy matching. Slice A makes that non-fuzzy
  guarantee systemic (the third, Splink-level gate, complementing `matchable:false` and
  `extends:[Thing]`), and as a side effect keeps Indicators out of the gold/measurement harness's
  `score_pairs` calls once the live ledger holds them.
- **Slice A also covers FtM-NATIVE non-matchable schemas** (discovered at build time): FtM marks
  e.g. `Sanction` `matchable: false`, yet pre-0119 those entities entered Splink scoring — two
  Sanctions could fuzzy-pair (the same bug class as Indicator↔Indicator), and an all-no-name
  Sanction window crashed the scorer (SplinkException) and was collaterally quarantined by the
  B-2 containment path. Post-0119 they are filtered like Indicators and resolve as id-convergent
  singletons. Behavior change pinned in `tests/integration/test_b2_poison_batch_isolation.py`
  (the crash-containment pin now uses a matchable no-name vehicle, verified to still raise; the
  old Sanction vehicle is re-pinned to its new clean-drain semantics).
- **S-2 phase 3 (designated deferral) — the attribution enricher.** `indicates → Organization` edges
  are OUT of this gate. Research verdict is final for phase 2: ThreatFox records carry NO actor
  field, and family→actor is fuzzy exactly where volume lives (`win.cobalt_strike` → 21 actors;
  Cobalt Strike is used by 30 ATT&CK intrusion sets, Mimikatz 51). Phase 3 is a separate L3-side
  INTERNAL_ENRICHMENT plugin that fuses **Malpedia attribution** and **ATT&CK software-`uses`** as
  independent, per-source-confidence evidence streams → ranked hypotheses (leads-not-verdicts), with
  commodity-tool edges (Cobalt Strike / Mimikatz class) suppressed or heavily discounted by default.
  **Licensing note for phase 3:** Malpedia's public metadata API is unauth but its content is
  **CC BY-NC-SA 3.0** (non-commercial, share-alike) — record an explicit non-commercial confirmation
  (OS-Pairs CC BY-NC precedent, ADR 0080) before that gate builds.
- **Licensing posture (this gate).** Feodo + **SSLBL are explicitly CC0** (commercial + non-commercial,
  no limitations). **ThreatFox + URLhaus are NOT CC0** — governed by the abuse.ch AG Terms of Use
  (Effective 2025-11-04): a fair-use / not-for-profit grant, "Query Volume Limits" discretionary,
  commercial use may require a Spamhaus subscription; §7.3 forbids redistribution/derivatives of the
  Platforms' IP (not the downloaded IOC data). This deployment is **self-hosted, non-commercial,
  pull-only, no redistribution** — squarely inside the fair-use grant (OS-Pairs CC BY-NC precedent).
  Continued free access for non-commercial self-hosted use is **probable-but-not-explicitly-
  guaranteed** by the 2025-07-29 sustainability post (corrected research verdict) — monitor.
- **Explicit rejections** (do not build): the SSLBL **JA3 feed** (frozen since 2021-08-03) and the
  SSLBL **botnet-C2 IP blacklist** (deprecated 2025-01-03, empty); the **v2 keyed zip exports**; any
  **`indicates` edge**.
- **Registration/scheduling residuals:** connectors auto-register via `walk_packages` (no registry
  file edit); the global 1 h cadence already respects abuse.ch etiquette (no per-connector interval).

## Revisit triggers + reversal costs

- **Legacy anon endpoints get gated (401/403).** Flip the defaults to the keyed v2 exports
  (`.../v2/files/exports/<AUTH-KEY>/recent.csv[.zip]`). **Reversal cost:** URL-embedded-secret
  redaction in logs/errors (the Auth-Key moves from an HTTP header into the URL path) + zip /
  decompression-bomb handling for the `.zip` full exports. Recorded, NOT built now. The connectors
  already carry the optional `auth_key` header from day one, so a build-time 401 probe (spec §11) is
  the early-warning signal — STOP and flag for the human rather than auto-switching.
- **Deployment commercializes.** ThreatFox/URLhaus then require a Spamhaus subscription or the
  contribute-for-six-months path (Feodo/SSLBL CC0 are unaffected). Operator determination, not
  technical. **Reversal cost:** low — an operator toggle / documented posture change.
- **A false-positive cross-connector convergence observed** (two genuinely-distinct IOCs colliding
  on `indicator_id`). **Reversal cost:** low — revisit the normalization contract in `ontology/ioc.py`
  (e.g. stop casefolding URL paths); does not affect this gate's connectors structurally.
- **Slice-A regression suspected** (a matchable merge outcome changes). The non-interference property
  test is the tripwire; **reversal cost:** trivial — the filter is one comprehension in `score_pairs`.
- **`indicatorType` unknown-passthrough proves noisy** (an unknown `ioc_type` needs value-specific
  normalization). Handle it in the mapper then; today's pass-through never drops evidence.
- **`train_candidate_model` asymmetry (slice-A checker LOW, deferred by spec scope):** the offline
  EM/measurement path (`splink_model.py::train_candidate_model`) still frames UNFILTERED entities —
  never the live merge path, not person-affecting, but a gold-harness run over a mixed real-seed
  corpus containing a non-matchable no-name window would re-encounter the all-null-fingerprint
  `SplinkException` there. Mirror the matchable filter when the measurement lane next opens.

## Classification note

`human_fork: false` + `person_affecting: false`. Reversible defaults pre-decided by the main loop
per CLAUDE.md's "classify every ADR decision by reversibility" rule; recorded here with reversal
cost + revisit trigger rather than manufacturing a human fork. Cosign offered at the gate PR.
