# 20 — Ontology

> `v0.4` · June 2026 · The L2 contract. Everything below it produces these objects; everything above
> consumes the resolved graph. The highest-leverage spec in the system.

## 1. Principle

WorldMonitor does **not** invent an entity model. It adopts **FollowTheMoney (FtM 4.x)** as the core
ontology, **extends** it (`wm:` namespace) only where FtM doesn't reach, uses **STIX 2.1** as the
vocabulary for the CTI domain (one plugin domain, not special), and anchors every entity to **stable
canonical IDs**. The graph is a **property graph** (Neo4j); `followthemoney-graph` bridges; Splink +
`nomenklatura` resolve.

## 2. How the ontology works (the mechanics)

Three pieces — a **vocabulary**, a **mapping step**, a **store** — deliberately data-driven, which is
what makes it expandable.

1. **Vocabulary = data, not code.** FtM defines entity types and relationship types, each a *schema
   file* listing allowed properties and which properties point at other entities (those become edges).
   Adopt FtM's schemas; add `wm:` schemas as additional files. **Adding a type or property = adding a
   schema file.** Downstream reads the schema, so nothing else changes.
2. **Mapping = a source's raw fields → typed entities.** Each connector's `map()` emits FtM/STIX
   objects, **validated against the schema** (the FtM lib rejects non-conforming data). The only place
   that knows a source's quirks; it stamps provenance + canonical IDs.
3. **Store = Neo4j.** Resolved FtM Things → nodes (labelled by type); FtM relationship-entities → edges
   (or intermediate nodes when they carry their own properties/provenance), via `followthemoney-graph`.
   Canonical IDs indexed/unique-constrained; every node/edge carries provenance + `tenant_id`.

## 3. FollowTheMoney core (use as-is)

- **Things (nodes):** `Person`, `Company`, `Organization`, `LegalEntity`, `Asset`, `Security`,
  `BankAccount`, `Address`, `Vehicle`, `Vessel`, `Airplane`, `RealEstate`, `Document`, `Identification`.
- **Intervals (relationships as first-class entities):** `Ownership`, `Directorship`, `Membership`,
  `Family`, `Associate`, `Employment`, `Payment`, `Representation`, `Sanction`, `CourtCase`, `Documentation`.

FtM's "relationships are entities too" design is a feature: an `Ownership` carries dates, percentage,
and **source** — a node with provenance, not a bare edge.

## 4. Extensions (`wm:` — only where FtM doesn't reach)

Minimal, additive, each with an ADR entry. Before adding, check it isn't expressible as an FtM schema + properties.

| Domain | Extension entities (proposed) | Notes |
|---|---|---|
| News / events | `wm:Article`, `wm:Event` (CAMEO), `wm:Narrative`, `wm:Claim` | GDELT GKG maps here; events link FtM actors + `wm:Place` + time |
| Social | `wm:Account`, `wm:Post`, `wm:Channel` | account → resolves-to → `Person`/`Organization`; coordination edges |
| Geospatial | `wm:Place`, `wm:Observation`, `wm:AOI` | keyed to GeoNames ID + coordinates; imagery observations attach |
| CTI / infra | `wm:Domain`, `wm:IPAddress`, `wm:Certificate`, `wm:Host` | mapped from STIX SCOs; fingerprint clusters as edges |
| Crypto | `wm:CryptoWallet`, `wm:CryptoTx`, `wm:CryptoCluster` | taint/flow as weighted edges; USDT-on-Tron prioritized |
| Markets | `wm:Instrument`, `wm:Position`, `wm:MarketSignal` | prediction-market insider-signal leads as scored signals |

## 5. STIX 2.1 for the CTI domain

Use STIX for CTI rather than re-expressing it: SDOs (`indicator`, `malware`, `threat-actor`,
`campaign`, `infrastructure`, `attack-pattern`) + SCOs (`domain-name`, `ipv4-addr`, `x509-certificate`,
…). **Bridge by canonical identity:** a STIX `threat-actor` that is a real org links to the FtM
`Organization`; SCOs become `wm:Domain`/`wm:IPAddress` nodes with the STIX object retained as payload.
OpenCTI/MISP (if used) are upstream STIX *sources*; WorldMonitor's graph stays the system of record.

## 6. Canonical IDs (the anchor — non-negotiable)

| Identifier | For | Source |
|---|---|---|
| **Wikidata Q** | everything | SPARQL (targeted) / dumps (bulk) — the universal join key |
| **GeoNames ID** | places | dumps (incl. transliterations) |
| **LEI** | legal entities | GLEIF (free) |
| **OpenCorporates ID** | companies | OpenCorporates |
| **VIAF / ISNI** | named people | authority files |
| **ISO 3166** | countries | normalize all country fields |

Reference base layers (Wikidata, GeoNames, GLEIF, **OpenSanctions** [FtM-native, first-class],
OpenCorporates, World Bank/IMF context, **ACLED** [labeled events — gold for Sec 8], DBpedia) load as
enrichment keyed on these IDs. **Coverage is uneven** — treat absence as "unknown," never "doesn't exist."

## 7. Provenance model (on every node and edge)
```
provenance: { source_id, source_record (pointer to raw in MinIO), retrieved_at, reliability, assertion }
```
A node merged from N sources carries N entries → enables catastrophic-merge protection **and** the
GDPR/audit log.

## 8. Entity resolution & the catastrophic-merge guard (L3)

Connectors emit **candidates**; **L3 owns canonicalization** (never in a connector). **Splink** (DuckDB,
unsupervised Fellegi–Sunter, multiple blocking keys) + **nomenklatura** (FtM-native merge / OpenSanctions
logic) cluster candidates to canonical entities; **yente** can serve a matching/reconciliation API.
**The dominant failure mode is the catastrophic merge** — one wrong high-confidence link fuses two
unrelated people. Mandatory mitigations: (1) require multiple independent agreement fields (and ideally
independent sources) before merging; (2) keep the merge audit trail; (3) queue any high
size/value/sensitivity merge for **human review** — never auto-merge a sensitive entity.

## 9. Expandability (your core requirement)
- **New entity/relation type** → a `wm:` schema file. Data, not code.
- **New source** → a connector plugin (manifest + schema + collect + map).
- **New method** (resolver, enricher, rule, scorer) → a plugin (see `30`).
- **The contract (L2) is fixed; everything plugs into it** — a new capability never ripples upward.

## 10. Open ontology decisions (need the user — see `decisions/`)
Exact `wm:` set & naming per domain · `wm:Place` vs extend FtM `Address` · STIX retention (payload vs full decomposition).
