# 50 — Agent Layer (Hermes) & Self-Improvement

> `v0.4` · June 2026 · WorldMonitor exposes data + tools (L8, see `60`); **Hermes Agent is the agent
> layer on top.** Decision: **adopt Hermes as-is** (don't build a custom agent runtime). This doc
> covers the integration, LLM pluggability, and the **gated self-improvement loop** — the most
> powerful and the riskiest subsystem in the platform.

## 1. Why Hermes, and the division of labour

**Hermes Agent** (Nous Research, MIT, actively developed) is "the self-improving AI agent." It already
ships four things we'd otherwise build:
- a **self-improving learning loop** (creates skills from experience, improves them in use, curated
  memory with nudges, searches its own past sessions) and **trajectory generation/compression for
  training** the next tool-calling model;
- **any-LLM** support (OpenRouter, **your own endpoint = Ollama**, OpenAI, Nous Portal, …) switched
  with `hermes model` — no code changes;
- a **messaging gateway** (Telegram/Discord/Slack/Signal) + a **cron scheduler** delivering unattended
  reports/alerts in natural language;
- **MCP** — it connects to any MCP server and can serve as one.

**Division of labour:**
- **WorldMonitor owns** the data, ontology, graph, plugins, resolution, analytics, and the **API + MCP
  surface** (`60`). It is the source of truth.
- **Hermes owns** the agentic experience: user interaction (Telegram/CLI), autonomous investigation,
  scheduled reporting, and self-improvement. It is a **consumer** of WorldMonitor's MCP tools.

So WorldMonitor's job is to expose *excellent tools* (query the graph, traverse, enrich, alert); Hermes
drives them. We do not re-implement the agent loop, the LLM gateway, the Telegram bot, or memory.

## 2. Integration (how they connect)
- WorldMonitor runs its **MCP server** (L8). Hermes is configured (its MCP integration) to connect to
  that server → WorldMonitor's tools (`query_graph`, `get_entity`, `find_paths`, `enrich`,
  `run_connector`, `list_alerts`, …) appear in Hermes' toolset.
- Hermes authenticates to the MCP/API as a **service principal** in Zitadel with a scoped role
  (read + run-passive by default; run-active requires the human-in-the-loop gate).
- **Reports/alerts:** two complementary paths —
  1. **Rich agentic reports** — Hermes cron jobs query WorldMonitor and compose natural-language
     briefings, delivered to Telegram ("daily intelligence brief", "what changed about entity X").
  2. **Plain system alerts** — WorldMonitor's own **`TelegramNotifier`** plugin (`30`) sends
     deterministic alerts (a rule fired, a pipeline failed) independent of the agent, so notifications
     work even if the agent is down.
- Deployment: Hermes runs locally, on the always-on host, or a cheap VPS / serverless backend
  (it supports local/Docker/SSH/Modal/Daytona) — aligns with the "transportable" requirement.

## 3. LLM pluggability (Ollama / OpenRouter / …)
- **Agent-side:** handled by Hermes natively (`hermes model`) — Ollama via own-endpoint, OpenRouter,
  etc. No lock-in.
- **Service-side:** WorldMonitor's own LLM use (NLP enrichers doing entity extraction/summarization)
  goes through **LiteLLM** as a provider-agnostic gateway, so even non-agent LLM calls are
  Ollama/OpenRouter/Anthropic-swappable via config. One env change swaps providers everywhere.

## 4. The self-improvement loop (gated — this is the careful part)

You chose **all three** improvement mechanisms. They differ in power, cost, and risk — and **all share
one rule: nothing self-modifies silently in place.** Every change flows:

```
PROPOSE  → an agent/loop proposes a change (a skill, a tuned param/rule, or a candidate model)
EVALUATE → measure on held-out data / a benchmark (calibration, accuracy, safety checks)
GATE     → auto-promote ONLY if it beats baseline + passes safety; HUMAN sign-off for sensitive changes
PROMOTE  → versioned artifact swapped in; previous version retained for instant ROLLBACK
AUDIT    → what changed, why, by whom, eval results — logged (ties into the provenance ledger)
```

### 4a. Hermes learning loop — skills & memory (no GPU, always on)
Hermes' native loop: skills created from experience, improved in use; curated memory. **Scope:** the
agent's own procedural knowledge and recall — it does **not** touch WorldMonitor's data, params, or
graph. Lowest risk; runs continuously. Guardrail: skills that would call **active** tools or write to
the graph are still subject to the capability gates.

### 4b. Model fine-tuning from trajectories (needs GPU — batch, off the hot path)
Hermes generates/compresses **trajectories** from runs → curate into a training set → **fine-tune** the
tool-calling model → **evaluate on a benchmark** → promote the new model only if it beats the incumbent;
keep the old one for rollback. **Never** auto-deploy an unevaluated model to itself.
- **Where it runs:** the 64 GB box has no training GPU. Fine-tuning is a **batch job on a GPU path** —
  a rented/serverless GPU service (e.g. Modal/Novita/RunPod) or a local GPU if added — **not** on the
  always-on host. This is an **OPEN decision** (`decisions/`), and a **late phase** — it requires the
  spine + agent loop to exist and enough trajectory volume to be worth it.

### 4c. Agents tuning WorldMonitor's params/rules (the highest-stakes — strict gates)
Agents may **propose** changes to scoring weights, ER thresholds, and rules based on outcomes. These go
through the full pipeline, with extra constraints:
- **Versioned config/rules** — every param set and rule is a versioned artifact; instant rollback.
- **Evaluation is mandatory** — a proposed threshold/weight change is measured (e.g. does it improve
  calibration / precision on a labeled set?) before promotion.
- **Human sign-off is mandatory for anything affecting a real person** — ER thresholds (they change
  merges), and any scoring that flags/ranks individuals. These are **never** auto-promoted.
- **Bounded autonomy** — agents can auto-tune only within pre-declared safe ranges for non-sensitive
  parameters; outside the range → human gate.
- **Audit** — every change recorded; the system can always answer "what did the agent change, when, why."

> This design keeps the platform's "leads not verdicts / calibrate / human review" ethos intact even as
> the system improves itself. Power with provenance and a brake.

## 5. Build sequencing (see `40`)
Agent layer comes **after** the spine + API/MCP exist (you can't drive tools that aren't there). Order:
spine (P1) → API/MCP (P-with-2) → **Hermes connected + reports/alerts (P-agent)** → param/rule
auto-tuning with gates → trajectory fine-tuning (latest, GPU). The Hermes learning loop (4a) is on from
the moment Hermes is connected; 4b/4c are unlocked deliberately.

## 6. Open decisions (need the user — see `decisions/`)
Where fine-tuning runs (serverless GPU vs local) · which params are in agents' "safe auto-tune" ranges ·
report cadence/content · whether Hermes runs on the always-on host vs a separate VPS/serverless.
