# 0117 — MITRE ATT&CK intrusion-set catalog + `gid` canonical-anchor namespace

- **Status:** ACCEPTED (2026-07-18)
- **Date:** 2026-07-18
- **human_cosign:** Mithat Akyol 2026-07-18 — confirms the `person_affecting: false` classification (in-session ruling; checker-held gate condition resolved)
- **human_fork:** false — reversible. The connector is an additive plugin (disable/remove =
  reversal); the `gid` anchor tier is additive to `CANONICAL_ID_FIELDS`/`_PRECEDENCE` and can
  carry no effect on any existing entity (none holds a `mitre_gid` anchor), so removal
  reverts durable-id derivation for future ingests only. Reversal cost: drop the tier + field,
  disable the seed instance; already-derived `wm-anchor-gid-*` ids persist as ordinary durable
  ids (harmless data). Revisit triggers below.
- **person_affecting:** false — intrusion sets are organization-shaped catalog entries; no ER
  threshold, merge-guard mode, individual-affecting score, or erasure change. The change adds
  NEGATIVE evidence (distinct G-ids = anchor conflict ⇒ park), which strengthens, never
  weakens, the catastrophic-merge posture. The canonical-id invariant is touched, so the
  mandatory `@given` property suite applies (CLAUDE.md).

## Context

The OG-harvest backlog (91, S-3, P1) calls the ATT&CK intrusion-set catalog "the substrate
that lets articles + IOCs resolve onto named actors" — the CTI persona's (ADR 0094 D4) actor
layer. The platform now extracts actor candidates from news (ADR 0115/0116) and ingests
sanctions lists, but has no named-threat-actor substrate to resolve them against. MITRE's
`attack-stix-data` bundle is pull-only, free, license-clean (attribution terms), versioned,
and FtM-mappable (intrusion-set → Organization with aliases). The G-id (`G0032`) is a
globally-administered unique identifier — exactly what the anchor architecture exists for.

## Decision

1. **`mitre_gid` joins `CANONICAL_ID_FIELDS`** (ontology/anchors.py). Because the constraints
   module, the divergence guard's anchor exclusion, and the conflict evidence all derive from
   that tuple, one addition propagates the whole invariant surface: node uniqueness on
   `mitre_gid`, guard-visible negative evidence for distinct G-ids, fold/divergence coverage.
2. **A `gid` tier in `resolution/canonical.py::_PRECEDENCE`** (after `lei`, before `regno`;
   validity `^G\d{4}$`; context-only — G-id has no FtM property home, and the anchor context
   channel is the established extension point, spine-captured since Gate P1/ADR 0106).
   Durable ids serialize as `wm-anchor-gid-G0032` via the existing injective scheme.
3. **A `mitre_attack` connector** (EXTERNAL_IMPORT/passive, FtmBulkConnector shape): the
   PINNED versioned enterprise bundle → one FtM `Organization` per non-revoked, non-deprecated
   intrusion set (name + aliases + `mitre_gid` anchor + provenance; NO `topics` — catalog
   membership is not a risk verdict). Seeded enabled (~180 records, tiny).

## Consequences

- Same-group records converge on one node across sources; two distinct groups can never fuse
  silently (anchor conflict ⇒ guard park). News-extracted actors and future IOC feeds (S-2)
  gain a resolution target.
- **Deliberate residuals:** (a) silver labels read FtM properties only, so `mitre_gid` does
  not yet feed the measurement substrate — add a context-channel reader to `silver.py` when
  the G-id corpus is large enough to matter (revisit trigger); (b) ATT&CK `software`/
  `campaign` objects and actor↔technique edges are OUT of this slice (a later enricher);
  (c) bundle-version bumps are manual (pinned default; config-overridable).
- **Revisit triggers:** a false-positive `gid` match observed (drop/adjust the tier); the
  S-2 IOC lane landing (wire Indicators→intrusion-set edges); silver-coverage need above.
