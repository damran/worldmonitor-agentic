# Gate S-2 — `wm:Indicator` L2 extension + Feodo Tracker connector (spec)

> 2026-07-18 · OG-harvest S-2 (91 backlog, P1) · ADR 0118 · Operator chose the full
> `wm:Indicator` lane in-session. FIRST L2 extension (CLAUDE.md: "wm: extensions only where FtM
> can't reach — each additive, each with an ADR"). L2-contract + ER-adjacency ⇒ mandatory
> `@given` property test + lean fleet (test-author → builder → ONE checker) + cosign offered at
> the PR (S-3 precedent).

## Verified facts (probed on followthemoney==4.9.2 — record in VERIFIED_API.md)

`model.schemata["Indicator"] = Schema(model, "Indicator", spec)` followed by `model.generate()`
registers a new schema on the GLOBAL singleton (the one ftmg/nomenklatura import); entities
construct, `to_dict()`/`get_proxy()` round-trip. CORRECTION (builder re-probe): FtM's
`Schema.__init__` DEFAULTS `matchable` to **True** when the key is omitted — the original
probe carried an explicit `matchable: False` in its spec dict and this section had
over-generalized that as "the default". The schema therefore declares **`matchable: false`
EXPLICITLY** — FtM's own switch keeping the type out of every matchable-gated
fuzzy-resolution path. `Schema.__init__(model, name, data)`;
`SchemaSpec` is the YAML-shaped dict. The ftm-schema CI gate diffs INSTALLED-vs-vendored FtM
YAMLs only — a wm schema in OUR tree does not touch it.

## Deliverables

### D1 — the `wm:Indicator` schema + injection machinery

1. `src/worldmonitor/ontology/schema/wm/Indicator.yaml` — vendored, reviewable data (ADR 0098
   philosophy). Shape (builder: keep EXACTLY this surface; additions need a new ADR):
   `label: Indicator`, `plural: Indicators`, `extends: [Thing]`, **explicit `matchable: false`** (load-bearing —
   FtM defaults matchable to True; see the corrected Verified facts), properties: `indicatorValue` (string),
   `indicatorType` (string; values like ipv4/domain/url/sha256), `malwareFamily` (string),
   `firstSeenAt` (date), `lastSeenAt` (date), `indicates` (entity, range Organization — the
   future actor edge; unused by Feodo slice 1).
2. `src/worldmonitor/ontology/ftm.py`: `register_wm_schemata()` — loads every YAML under
   `ontology/schema/wm/` (stdlib yaml? the project has PyYAML via deps — verify; else
   `json`-shaped file), injects into the global model exactly as probed, then
   `model.generate()`. IDEMPOTENT (re-import/second call = no-op, no duplicate/generate churn)
   and invoked at `worldmonitor.ontology.ftm` import time so every consumer (writer, resolver,
   validation, tests) sees it without any env-var plumbing. `validate_or_raise` must accept
   Indicator entities unchanged.
3. ER-lane safety (the invariant): Indicators are dedup-by-id ONLY. Verify + pin: (a)
   `schema.matchable is False`; (b) the resolution pipeline never fuzzy-pairs an Indicator —
   read `resolution/splink_model.py` + `pipeline.py` for how non-matchable/non-person schemas
   flow today (Articles/Events already pass through: mirror whatever keeps THEM safe and pin
   it for Indicator); (c) FtM refuses an Indicator↔Organization merge (no common schema).

### D2 — `feodo` connector (mirror the `mitre_attack` package shape exactly)

EXTERNAL_IMPORT/passive. `collect()`: guarded_stream GET of
`https://feodotracker.abuse.ch/downloads/ipblocklist.json` (unauthenticated; VERIFY live +
inspect the actual field names first — do not trust this spec's memory of them), byte-capped
(16 MiB fail-closed), one RawRecord per entry (key = the ip:port), optional `limit`.
`map()`: one `Indicator` — deterministic `id=f"feodo-{sha1(value)}"` where `value=f"{ip}:{port}"`,
`name=[value]`, `indicatorValue=[value]`, `indicatorType=["ipv4"]`, `malwareFamily=[malware]`
when present, `firstSeenAt`/`lastSeenAt` from the feed's timestamps (ISO-normalized,
fail-soft on junk), `datasets=["feodo"]`, provenance stamped (reliability "B" — curated
community CTI), `validate_or_raise`. NO topics, NO country property (the feed's geo is ASN
metadata, not an event location — keep Indicators off the dashboard globe). Malformed entries
map to `[]` logged. Seed: `SeedSpec("feodo", "ipblocklist", {...}, enabled=True,
category="cti")`. ADR notes abuse.ch fair-use.

### D3 — docs
ADR 0118 (drafted; builder regenerates the ADR index) · roadmap CTI bullet: S-2 landed.

## Mandatory tests

- `tests/property/test_prop_indicator_lane.py` (@given, pure): P-IND-1 same IOC value from two
  sources → identical deterministic id (converges by id, not by matching); P-IND-2 an
  Indicator and an Organization NEVER merge: FtM common-schema refusal (probe
  `model.common_schema(indicator, org)` raising / cluster refusal at the merge helper level —
  test-author picks the strongest cheap pin) AND `Indicator.matchable is False`; P-IND-3
  injection idempotency (call register twice; model object count/schema identity stable;
  round-trip still works); P-IND-4 hostile property values (huge strings, junk dates) never
  crash `validate_or_raise`-based construction (fail-soft at map level is the connector's job).
- `tests/unit/test_feodo_connector.py`: hermetic MockTransport + stubbed DNS (feeds-connector
  idiom); collect/map/schema/limit/malformed cases; no-topics; deterministic id.
- Existing estates green untouched (`test_seed.py` picks up the new SeedSpec automatically).

## Checker obligations
1. Re-probe injection + matchable + common-schema refusal INDEPENDENTLY (own /tmp script).
2. Read the resolution pipeline path for Indicator flow; adversarially attempt to make an
   Indicator fuzzy-pair with an Organization through the real `cluster_and_merge` on
   hand-built entities — must be impossible.
3. Live-verify the Feodo URL (bounded fetch) + that the connector's field mapping matches the
   REAL feed shape.
4. ftm-schema gate unaffected (`uv run python scripts/check_ftm_schema.py` passes).
5. Full fast suite + integration seed estate + ruff format repo-wide + pyright on touched src.
6. Scope review vs gate.scope; ADR 0118 classification sanity (cosign is offered to the
   operator at the PR — flag, don't fail, if the line is absent at check time).
