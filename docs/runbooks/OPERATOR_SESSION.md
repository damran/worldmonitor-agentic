# Operator session — every operator-blocked step, one sitting

> **Purpose:** every precondition in the program that is blocked on *you* (host, keys, sign-off) —
> consolidated from `docs/fable-review/82_GATE_3B_CUTOVER_PLAN.md` §8, `deploy/hermes/README.md`,
> `docs/fable-review/50_FABLE_REVIEW.md` §2.13, and `docs/fable-review/90_REREVIEW_2026-07-11.md`
> ("Blocked on the operator, nothing else"). Work top-to-bottom; §§1–3 are the product/measurement
> unlock, §§4–7 are the Gate-3b preconditions, §8 is Phase-3 S4, §9 is the (deferrable) cutover
> cosign. Everything except §9 is reversible.
>
> Commands assume the repo root on the always-on host. Python snippets run inside the `api`
> container: `docker compose -f deploy/compose.yaml exec api python - <<'PY' … PY`.

---

## 0. Preconditions

- Always-on host with Docker; repo at current `master`; `.env` built from `.env.example`.
- **Do not carry a dev `.env` over** — in particular do **not** set `ENFORCEMENT_PROFILE=off` on
  this host. Leave it unset: the code default is the strict production posture (ADR 0109).
- `NEO4J_PASSWORD` and the other core secrets set; `.env` stays out of git.

## 1. Deploy the always-on stack

```bash
cp .env.example .env    # fill values (or copy your maintained host .env)
docker compose -f deploy/compose.yaml --env-file .env up -d
./scripts/dev/zitadel_provision.sh     # once; idempotent — paste ZITADEL_* back into .env
docker compose -f deploy/compose.yaml ps           # everything healthy
curl -fsS http://localhost:8000/health             # api up
# open http://localhost:8000/app — globe + feed render (no LLM needed for these)
```

The `driver` service now collects the seeded sources continuously (feeds hourly; OpenSanctions
datasets on the same cadence) and resolves them every 5 minutes. Nothing else to schedule.

## 2. Enable extraction + AI briefs (Ollama)

```bash
# On the HOST (not in compose): install Ollama, then
ollama pull llama3.2                # the compose default; or any model you prefer
```

On a Windows host also set `OLLAMA_HOST=0.0.0.0:11434` and allow TCP 11434 through the firewall
(containers reach the host via `host.docker.internal`).

In `.env`: set `EXTRACTION_ENABLED=true` **and `FULLTEXT_ENABLED=true`** (ADR 0116 — extraction
then sees article bodies, not just headlines; and `LLM_OLLAMA_MODEL=<model>` if not `llama3.2`),
then:

```bash
docker compose -f deploy/compose.yaml --env-file .env up -d api driver   # pick up env
docker compose -f deploy/compose.yaml logs -f driver | grep -i extraction # first cycle ≤15 min
# verify: http://localhost:8000/app shows extracted Events; /api/dashboard/brief returns a synthesis
```

The containers reach Ollama at `host.docker.internal:11434` (`LLM_OLLAMA_BASE_URL` to override).

## 3. Real-seed corpus + calibration measurement (G7 unlock)

Leave the stack running **≥24–48 h** so the candidate corpus accumulates (OFAC SDN +
`us_dod_chinese_milcorps` + the curated feeds). This alone discharges the long-standing
"real-seed connector run" blocker. Then produce the calibration evidence:

```bash
# Label-sufficiency report (lands with WP-1 this stretch — the WP-1 PR updates this line
# with the exact CLI): labels by source, 0.5–0.95 boundary coverage, B³/CEAFe/over-merge CIs.
```

**Report back:** the sufficiency report output. Threshold promotion itself stays human-gated —
this session only produces the measurement.

## 4. Gate 2b backfill RUN (`82_GATE_3B_CUTOVER_PLAN.md` §8, first checkbox)

Dry-run first, review the counts, then commit, then assert completeness:

```bash
docker compose -f deploy/compose.yaml exec api python - <<'PY'
from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.resolution.backfill import backfill_spine, assert_backfill_complete

neo4j = Neo4jClient.from_settings()
with session_factory(engine_from_settings())() as session:
    print(backfill_spine(session, neo4j=neo4j, dry_run=True))   # review, then rerun with dry_run=False
    # backfill_spine(session, neo4j=neo4j, dry_run=False)
    # assert_backfill_complete(session)                          # must NOT raise
PY
```

Then the **per-cohort fidelity spike** (ADR 0113 SF-4): sample per-source cohorts and byte-compare
against `er_queue.raw_entity` per `docs/reviews/GATE_2B_BACKFILL_SPEC.md`.

## 5. Erasure convergence (§4 R1b)

```bash
docker compose -f deploy/compose.yaml exec api python - <<'PY'
from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.resolution.erasure_scrub import scrub_stock

neo4j = Neo4jClient.from_settings()
with session_factory(engine_from_settings())() as session:
    results = scrub_stock(session, neo4j=neo4j)
    session.commit()
    print(results)          # re-run until it reports nothing further to scrub
PY
```

Re-run to convergence; a subsequent rebuild must contain no erased source
(`82_GATE_3B_CUTOVER_PLAN.md` §4 R1b).

## 6. Rebuild-and-diff guard: enabled, isolated, green over N cycles

In `.env`: `PROJECTION_DIFF_ENABLED=true` and `PROJECTION_DIFF_NEO4J_URI=<ISOLATED Neo4j>` — a
**separate** Neo4j instance (e.g. a second container), never the live one. Restart the driver and
let it run **N cycles green** (record your N; it is part of the §9 cosign evidence). Watch the
`worldmonitor_projection_*` metrics (`/metrics`, port 9108, or the Prometheus `monitoring` profile)
and the driver log for divergence or `ProjectionDiffMisconfiguredError` refusals.

## 7. One-time reconciliation instruments (PR #185; §4 of the cutover plan)

Run the two-directional + count + label reconciliation over the real corpus (requires the §6
`PROJECTION_DIFF_*` settings — the same isolated target; the CLI double-fences exactly like the
scheduled guard and exits 2 if the target is not provably distinct):

```bash
docker compose -f deploy/compose.yaml exec driver \
  python -m worldmonitor.resolution.reconcile_cli
```

**Exit 0 = all four instruments PASS** (counts R11/R11b, label parity §3.1, erased-source residue
R9b, co-present value divergence R9c) — `compare_labels`' loss direction and
`find_erased_source_residue` are the executed closure of the two red-team CRITICALs (topic-label
loss; erased-value resurrection). Paste the printed report into §10.

## 8. Hermes S4 — deploy, verify, first Telegram brief (Phase-3 resume condition)

Follow `deploy/hermes/README.md` §§1–6 (image, Zitadel provisioning + the two bearers, `.env`,
`--profile agent up -d`, log checks). Then **report back**, verbatim:

1. The exact Hermes run command, resolved config dir (`HERMES_HOME`), and transport key used.
2. Proof the WorldMonitor MCP tools are visible from Hermes (tool list in the Hermes log — expect
   the 4 read tools).
3. An `llm-egress` audit line with `caller=hermes` from the `api` log (proves the S3a /v1 path).
4. The first scheduled Telegram brief received — this is the S4 done-condition.

## 9. ADR 0114 cosign (LAST; person-affecting, irreversible — defer freely)

Only after §§4–7 are green: review `docs/decisions/0114-gate-3b-cutover.md` (including the D-1
SWAP-vs-in-place choice), cosign it, and the cutover PR flips it PROPOSED→ACCEPTED. Until then the
cutover stays paused and Neo4j remains the live SoR — that is a supported steady state.

## 10. Report-back checklist (paste to the agent)

- [ ] §1 `ps` output healthy; `/app` renders
- [ ] §2 extraction on; a brief generated
- [ ] §3 sufficiency-report output (after the accumulation window)
- [ ] §4 backfill counts + `assert_backfill_complete` clean; fidelity-spike result
- [ ] §5 scrub-to-convergence result
- [ ] §6 your N + N-cycles-green evidence (metric/log excerpt)
- [ ] §7 four reconciliation PASSes
- [ ] §8 items 1–4 (Hermes proofs + first Telegram brief)
- [ ] §9 cosign decision (or explicit deferral)
