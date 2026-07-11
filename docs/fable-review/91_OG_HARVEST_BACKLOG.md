# 91 — OG WorldMonitor harvest: ranked source/surface backlog

> **Date:** 2026-07-11. **Input:** deep-dive of `koala73/worldmonitor` (AGPL-3.0, ~62k
> stars) — actual config/source files, not just the README — by two research agents during
> the 2026-07-11 re-review (`90_REREVIEW_2026-07-11.md`). **Ground rule:** their AGPL code
> and curated prose are never copied; we harvest **source lists, endpoints, tier metadata,
> and design patterns**, re-implemented from scratch, each behind our own gate + ADR.
> Every item below passed a constraint check: pull-only, free, license-clean, CTI-persona
> first (ADR 0094 D4), self-hosted single-tenant, LLM egress only via the gateway.
>
> This is a **backlog**, not a schedule: nothing here pre-empts the P-gate sequence
> (`81_PRECUTOVER_GATE_SEQUENCE.md`). Items land as individual gates when capacity allows.

## What the OG actually is (for calibration)

A client-heavy real-time dashboard: 566 curated feeds across ~49 categories, 65+ providers,
6 CTI IOC feeds, a 159-actor APT catalog keyed to MITRE G-ids, CII v8 country-instability
scoring for 31 nations, a 35-source freshness tracker with "intelligence gaps" reporting, a
40-tool public MCP server, CLI + zero-dep SDKs, Vercel/Upstash/Tauri deployment. Their moat
is feed breadth surfaced raw-ish; ours is the resolved entity graph with provenance and
calibrated ER. Hence: harvest inputs and ergonomics, never the architecture.

## A. Sources → connector gates

| # | Item | What | First slice | Constraint check | Prio / effort |
|---|---|---|---|---|---|
| S-1 | **CTI/OSINT curated feed pack + source-tier registry** | ~25 security/intel feeds (Krebs, CISA, Ransomware.live RSS, Bellingcat, OCCRP, DFRLab, Oryx, USNI, think-tanks…) as data-only `FeedConnector` instances; their 4-tier credibility idea mapped to provenance `reliability` (tiers re-derived by us); state-affiliation as a `wm:` source attribute | `catalog/feeds_cti.json` seed + loader + property test that every emitted Article carries reliability + source_id; cross-check the operator's master spreadsheet (sheet 72) first | Pull-only RSS, free; URLs/tiers are facts not AGPL expression | **P0** / S (1 gate, mostly data) |
| S-2 | **abuse.ch IOC family** (Feodo Tracker first; URLhaus/ThreatFox/SSLBL after) | C2/malware-host IOCs from unauthenticated JSON endpoints → STIX 2.1 Indicators + provenance; ThreatFox exports STIX natively | Feodo connector: collect→map→landing zone→ER queue; config schema carries the free auth-key field for siblings | Pull-only, free (community tier; note fair-use in ADR); geo-enrichment deliberately NOT in the connector (later L4 enricher via self-hosted GeoLite2) | **P1** / S-M |
| S-3 | **MITRE ATT&CK actor catalog** (their APT layer, license-clean) | Ingest the upstream `attack-stix-data` enterprise bundle (intrusion-sets) → FtM Organization + aliases, **G-id as a new canonical-ID namespace** | Bulk importer + G-id unique constraint + `@given` test that ER never merges two distinct G-id actors (canonical-id invariant touched) | Pull-only static bundle, free, MITRE terms = attribution; strictly better than their AGPL file | **P1** / M — **strategic: the substrate that lets articles + IOCs resolve onto named actors** |
| S-4 | **Ransomware.live victim/group connector** | Groups (Organization) —[targeted]→ victims (Company), resolvable against OpenCorporates/OpenSanctions | Recent-victims endpoint → FtM + `wm:Event`, explicit low-reliability "criminal self-declaration" stamp; negative test: victims never auto-merge into sensitive entities | Pull-only, free personal tier — **T&C review is a gate step**; allegation-grade → leads-not-verdicts mandatory | **P1** / M |
| S-5 | Cloudflare Radar outage annotations | Country/ASN outage events → `wm:Event` | Annotations endpoint + cursor + token config | Pull-only, free token, CC BY-NC (fine self-hosted; attribution in provenance) | P2 / S |
| S-6 | GDELT doc 2.0 (+GKG later) | Already on our Phase-4 news lane; keep ingestion thin — their mention-count/keyword validation is enricher policy, not connector logic | Bounded doc-2.0 query connector → FtM Article + provenance | Pull-only, free, open license | P2 / S-M |
| S-7 | UCDP GED conflict events | License-clean half of their conflict pair (ACLED is restricted → OPEN-ADR later) | GED endpoint + version discovery → `wm:Event` incl. release version in provenance | Pull-only, free, attribution | P2 / S |
| S-8 | Community IOC extras: C2IntelFeeds CSV → OTX (G8 cursor) → AbuseIPDB (corroboration-only) | Same STIX Indicator lane as S-2 | C2IntelFeeds first (no key, GitHub CSV) | Pull-only, free tiers; verify per-feed licenses at gate time | P2 / S-M |
| S-9 | Sanctions-pressure velocity scorer | Their OFAC-diff idea, done as a pure query over **our own statement log** post-F1-cutover — no new connector | Scorer over per-country new-sanctions-statement velocity; replay-insensitivity property test | No egress at all; blocked on F1 cutover by design | P3 / S |
| S-10 | Infra reference pack (IMF PortWatch → cables → GPS-jam → hazards) | Physical-infrastructure context nodes CTI events can resolve against | PortWatch disruptions connector (clean ArcGIS JSON) | Pull-only, free; TeleGeography CC BY-NC-SA + gpsjam ADS-B-derived — verify at gate | P3 / M |
| S-11 | Telegram OSINT channels (StreamConnector) | 56-channel pattern; their MTProto hardening lessons are facts | 3-channel allowlist + per-channel cursor + flood budget | Read-only but personal-account ToS gray zone → **OPEN-ADR, user decision before any build** | P3 / L |

## B. Surfaces → API/MCP/CLI gates

| # | Item | What | First slice | Prio / effort |
|---|---|---|---|---|
| F-1 | **Source-freshness surface + intelligence-gap reporting** | Their 6-state freshness machine (`fresh/stale/very_stale/no_data/error/disabled`, per-source max-stale budgets, `requiredForRisk` gating, "what analysts can't see" reporting) on substrate we already have (`ConnectorInstance.last_run` + Prometheus exporter). **Closes re-review finding 9 (no staleness metric).** | Derived freshness_status per instance + labeled Prometheus gauge + 1 alert + `GET /sources/freshness` + matching read-only MCP tool; per-manifest `max_stale_min` = slice 2 | **P0** / S (1 gate) |
| F-2 | **MCP contract polish** | `readOnlyHint`/`idempotentHint` annotations, typed output schemas, structured `{error, hint}` envelopes on our 4 tools | One XS gate, no behavior change | **P0** / XS |
| F-3 | `get_entity_dossier` | Their brief-tool concept, ours: slice 1 = **deterministic** assembly (entity + neighbors + provenance + merge history from existing query helpers, zero LLM); slice 2 = gateway-only LLM narrative with mandatory sources array + CTI `framework` param (diamond-model/kill-chain/ACH) recorded in the egress audit | Slice 1: MCP tool + `GET /entities/{id}/dossier` | P1 / M (2 gates) |
| F-4 | MCP prompts as analyst playbooks | `entity-workup`, `freshness-audit` — declarative step+purpose workflows for Hermes | One gate, both transports, arg length-caps tested | P1 / S |
| F-5 | `summary` context-budget flag | `{count, sample[3]}` on `get_neighbors`/`find_paths`, shared helper so REST + MCP stay lockstep | One gate | P1 / S |
| F-6 | Thin read-only `wm` CLI | `[project.scripts] wm` over OUR REST (WM_BASE_URL + bearer), exit-code contract; noun expansion later | `wm health / ready / entity <id>` + tests | P1 / S-M |
| F-7 | OpenAPI as artifact | Response models on graph routes, committed spec + CI drift check | One gate | P2 / S |
| F-8 | MCP live-smoke in CI | compose-boot step: authenticated `tools/list` asserts exactly the registered tool set | XS rider on compose-boot | P2 / XS |
| F-9 | `describe_tool` + compressed tools/list | Token-tax control — pointless at 4 tools; trigger at ~10+ | Trigger-based | P2 / S |
| F-10 | JMESPath response projection | Server-side projection param; queue behind F-5 | One gate | P2 / S |
| F-11 | MCP resources (freshness probe, entity URI template) | For resource-aware hosts; Hermes speaks tools first | After F-1 | P3 / S |
| F-12 | Zero-dep Python client module | Demand-driven; CLI + MCP cover today's consumers | — | P3 / S |

## C. Design patterns harvested (no code, no gate — inputs to future ADRs)

- **CII decomposition** (Phase-5 scorer): per-component provenance tags, capped named boosts,
  floors, freshness-degraded scoring (`COVERAGE_PARTIAL` = our F-1 feeding the scorer),
  editorial baseline as an explicit uncalibrated prior. Their weights are NOT evidence; ours
  must pass the ADR 0043 harness and ship as calibrated leads, never verdicts.
- **Freshness-as-gap-reporting**: consumers (scorers, MCP tools) must be able to see which
  upstream sources are dark before trusting derived output.
- **Per-source tier/credibility registry** with propaganda-risk/state-affiliation flags —
  lands as data in S-1, informs reliability calibration later.

## D. Explicit rejections (recorded so we don't relitigate)

WAF/JA3-evasion fetch paths (OREF residential proxy, Polymarket TLS bypass) — anti-bot
evasion, against hostile-data discipline · Vercel/Upstash/Railway SaaS cache spine — violates
data sovereignty (pull-only, no external brokers) · protobuf contract layer — L2/FtM is the
contract · Tauri desktop, six site variants, dashboard/globe machinery — we are a backend;
UI beyond integrations/review is Phase-6 · anonymous/monetized API surface — auth-gated
single-tenant, no exceptions · in-tool direct LLM provider calls — gateway choke point only ·
connector-side dedup (their Haversine grid merge) — ER is central (L3), hard rule ·
Yahoo-Finance-grade unofficial APIs, retailer scraping, PizzINT — ToS/provenance/calibration
failures · copying any curated AGPL dataset (apt-groups, datacenters, tier tables verbatim) —
go upstream (MITRE STIX, Epoch CC-BY, registries) · AviationStack simulated-data fallback —
synthetic data presented as monitoring is a pattern we never adopt · 65-provider breadth as a
target — connector growth stays governed by the OSINT master inventory, one slice at a time ·
ACLED (restricted license) and LiveUAMap (no official API) — soft-rejected pending explicit
OPEN-ADRs.
