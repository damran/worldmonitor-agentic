# Gate S-3 — MITRE ATT&CK intrusion-set catalog + `gid` canonical-anchor namespace (spec)

> 2026-07-18 · OG-harvest backlog S-3 (`docs/fable-review/91_OG_HARVEST_BACKLOG.md` — P1,
> "strategic: the substrate that lets articles + IOCs resolve onto named actors"; constraint-
> checked there: pull-only static bundle, free, MITRE terms = attribution). ADR 0117.
> **Canonical-id invariant touched ⇒ mandatory `@given` property test** (CLAUDE.md build
> discipline). Lean fleet: test-author → builder → ONE checker. NOT person-affecting
> (organizations/intrusion sets; no threshold, guard-mode, or erasure change).

## Why

The dashboard extracts actor candidates from news (`derivation:geoevent` Person/Organization)
and OFAC/DoD lists supply sanctioned entities — but there is no substrate of NAMED threat
actors for any of it to resolve onto. ATT&CK's intrusion-set catalog (~180 groups, G-ids,
rich aliases) is that substrate for the CTI persona (ADR 0094 D4). The G-id becomes a first-
class canonical-ID namespace so the same group ingested twice (or later referenced by an IOC
feed) converges onto ONE node, and two DIFFERENT groups can never fuse.

## Deliverables

### D1 — `gid` anchor namespace (the invariant surface)

1. `ontology/anchors.py`: add `"mitre_gid"` to `CANONICAL_ID_FIELDS`. This ALONE propagates to
   `set_anchor`/`get_anchors`/`get_anchor_conflicts`/`anchor_conflicts_across` (the
   catastrophic-merge guard's negative evidence), `graph/constraints.py::CANONICAL_ID_PROPERTIES`
   (node uniqueness constraint), and `resolution/divergence.py`'s anchor-key exclusion — all
   import the tuple. VERIFY by grep there is no OTHER hardcoded list of the four old fields
   anywhere in src/ or the P1 promote-point capture (`resolution/pipeline.py` context-claim
   capture; `resolution/projector.py` fold reinstatement via `set_anchor_claims`) — if any
   enumerates fields statically, extend it and note it in the PR.
2. `resolution/canonical.py`: extend `_PRECEDENCE` with
   `_Tier("gid", <context-only>, "mitre_gid", normalize=False, valid=_is_gid)` inserted AFTER
   `lei`, BEFORE `regno`. `_is_gid` = `^G\d{4}$`. `_Tier.ftm_prop` currently always names a real
   FtM property; G-id has no FtM home, so make the tier support a context-only mode (empty
   `ftm_prop` ⇒ skip the property read in `_tier_values`) — smallest possible change, existing
   tiers byte-identical. Inserting a NEW kind cannot re-anchor any existing entity (none carry
   `mitre_gid`); say so in the ADR.
3. Serialization: durable ids come out as `wm-anchor-gid-G0032` via the existing injective
   `_anchor_id` — no change needed; the property suite must confirm.

### D2 — `mitre_attack` connector (product surface)

`src/worldmonitor/plugins/connectors/mitre_attack/` mirroring the `opensanctions` package
shape (manifest + `config.schema.json` + connector + tests): mode `EXTERNAL_IMPORT`,
capability `passive`, `FtmBulkConnector` subclass.

- `collect(config)`: `guarded_stream("GET", url)` where `url` defaults to the PINNED
  attack-stix-data enterprise bundle release (builder: resolve the CURRENT latest versioned
  file, e.g. `enterprise-attack/enterprise-attack-<ver>.json` on
  `raw.githubusercontent.com/mitre-attack/attack-stix-data` — pin the exact version in the
  default config, never `master`'s floating file; verify it fetches). Parse the STIX bundle;
  yield one `RawRecord` per `intrusion-set` object that is neither `revoked` nor
  `x_mitre_deprecated`; `key` = the G-id; honor an optional `limit` config like opensanctions.
  Byte-cap the download read (fail-closed like the feeds connector's `_MAX_FEED_BYTES`; the
  bundle is ~40-60 MB — cap at 256 MiB).
- `map(record, provenance)`: FtM `Organization` — deterministic `id=f"mitre-{gid}"`,
  `name=[name]`, `alias=[...aliases minus the primary name]`, `datasets=["mitre_attack"]`;
  `set_anchor(entity, "mitre_gid", gid)`; `stamp(entity, provenance)`;
  `validate_or_raise`. The G-id is extracted from `external_references` where
  `source_name == "mitre-attack"`; a record without a valid G-id maps to `[]` (fail-soft,
  logged). NO FtM `topics` are stamped (same rule as extraction — leads not verdicts; a
  catalog membership is not a risk verdict).
- Seed: add `SeedSpec("mitre_attack", "enterprise", {…pinned defaults…}, enabled=True,
  category="cti")` to `db/seed.py` (idempotent-additive rollout like WP-2a).

### D3 — docs

ADR 0117 already drafted (`docs/decisions/0117-attack-catalog-gid-anchor.md`) — builder runs
`scripts/gen_adr_index.py`. Roadmap: add an S-3 line under a new "CTI on-ramp" bullet in the
Next section (keep it one line).

## Mandatory `@given` property suite (test-author; PURE, Docker-free)

`tests/property/test_prop_gid_anchor.py`:
- **P-GID-1 (never-merge)**: for any two entities carrying DISTINCT valid G-ids (with any other
  overlapping props/names), `anchor_conflicts_across([a, b])` flags `mitre_gid`, and their
  derived durable ids (via the canonical derivation entrypoint `build_durable_id`/equivalent —
  test-author reads `canonical.py` for the exact public fn) are DISTINCT.
- **P-GID-2 (convergence/injectivity)**: the SAME G-id on two records from different sources
  derives the IDENTICAL `wm-anchor-gid-<gid>` durable id.
- **P-GID-3 (precedence non-interference)**: an entity carrying BOTH a QID and a G-id anchors
  by QID (tier order), and an entity carrying ONLY old-tier anchors derives ids byte-identical
  to before the change (regression pin: no existing entity re-anchors).
- **P-GID-4 (validity gate)**: malformed G-ids (`G12`, `g0001`, `G00321`, `""`, junk) never
  produce a `gid` anchor.

Plus unit tests (test-author): connector map shape (Organization, aliases, anchor context,
provenance round-trip, deterministic id, revoked/deprecated skipped, no topics), collect over
a small in-test STIX bundle via `httpx.MockTransport` + stubbed DNS (mirror
`tests/unit/test_feed_connector.py`'s hermetic idiom), config-schema validation, seed-spec
validity (the existing `test_seed.py` catches it automatically — verify).

## Checker obligations

1. Reproduce the property suite + an adversarial probe: hand-build two intrusion sets with the
   same name but different G-ids → confirm conflict evidence + distinct durable ids; same G-id
   different names → same durable id.
2. Grep-verify NO other hardcoded anchor-field list missed (constraints/divergence/pipeline/
   projector/guard); confirm the graph uniqueness constraint for `mitre_gid` is emitted
   (read `ensure_constraints` output or its unit test).
3. Live-fetch the pinned bundle URL once (HEAD/first bytes) — the seeded default must not 404.
4. Full fast suite + local integration for the seed/constraints estates; ruff format repo-wide;
   pyright strict on the touched src files.
5. Confirm NOT person-affecting: no diff in merge thresholds, guard modes, erasure, or Person
   scoring paths.
