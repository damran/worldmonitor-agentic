# 0089 — Phase 3: the Hermes agent layer (umbrella)

- **Status:** ACCEPTED (2026-06-30)
- **Date:** 2026-06-30
- **Milestone:** Phase 3 — Agent layer (Hermes) connected (`docs/40_ROADMAP.md:71`). Done-condition:
  *"you can ask WorldMonitor questions from Telegram and receive scheduled briefings, driven by
  Hermes over the MCP tools."*
- **human_fork:** false. Every decision below was made by the user and is **reversible** (default +
  reversal-cost + revisit-trigger per the build-discipline rule). The two genuinely-irreversible /
  person-affecting subsystems (model fine-tuning §4b, param/rule auto-tuning §4c, and any
  active/write MCP tool) are **explicitly OUT of Phase 3** and deferred to Phase 6, human-gated.
- **Supersedes/realizes:** ADR 0063 §1+§3 revisit trigger ("flip to HTTP+bearer when Hermes goes
  remote"). That trigger fires now → S1 (ADR 0090).

## Context

`docs/50_AGENT_LAYER.md` and CLAUDE.md lock the shape: **adopt Hermes as the agent layer** (don't
build a runtime); Hermes is a **consumer** of WorldMonitor's MCP tools as a Zitadel service principal
(read + run-passive); WorldMonitor owns the data/graph/tools. Phase 2 shipped the read surface — REST
(ADR 0062) + a FastMCP **stdio** read server with 4 read-only tools (ADR 0063). The agent-side LLM
gateway dir `src/worldmonitor/llm/` exists but is empty. This ADR records the three user decisions
that open Phase 3 and breaks the phase into five independently-mergeable slices (S1–S5). Only **S1**
is fully specced now (companion ADR 0090 + `docs/reviews/GATE_S1_MCP_HTTP_AUTH_SPEC.md`); S2–S5 are
scoped here and specced when reached.

## Decision

### D1. Adopt Hermes `NousResearch/hermes-agent` v0.17.0 (MIT) as its own container — reversible
Hermes runs as its **own container on the always-on host**, NOT vendored into `src/` (CLAUDE.md repo
layout note: *"Hermes runs as its own process/container … not vendored"*). v0.17.0 ships what we need
off-the-shelf: the self-improving skills/memory loop, an MCP client supporting **both stdio and
HTTP/SSE with `Authorization: Bearer` headers**, a `tools.include` allowlist, `hermes model`
provider-switching, a Telegram gateway, and a built-in cron scheduler. We adopt/depend on it — never
fork as foundation (CLAUDE.md).
- **Reversal cost:** low — Hermes is a config-connected external consumer behind our MCP contract;
  swapping to another MCP-speaking agent runtime changes no `src/` code, only the compose service.
- **Revisit trigger:** Hermes upstream stalls/changes license, or we need an agent capability it
  lacks → swap the agent container; the MCP contract is the stable seam.

### D2. MCP transport = remote HTTP + Zitadel bearer (stdio retained as fallback) — reversible
The user chose a **remote HTTP** MCP transport with **Zitadel bearer auth** over a co-located stdio
spawn. So the MCP server gains an **authenticated HTTP transport**; the existing **stdio transport is
retained unchanged** for local/admin use and as a fallback. Transport is selected by config. This is
the realization of ADR 0063's named revisit trigger and is the **security boundary** of the whole
phase — hence it is **S1**, a prerequisite for everything else, and the only slice fully specced now.
- **Reversal cost:** low — both transports coexist behind one `build_server`; flipping the default
  back to stdio is a config change. The bearer-auth path reuses the existing `ZitadelTokenVerifier`.
- **Revisit trigger:** Hermes is moved co-located onto the same host/container as the MCP server (no
  network hop) → stdio can become the default again; the HTTP path stays available.

### D3. LLM = hybrid via our LiteLLM gateway, operator-selectable with visible confidentiality — reversible
WorldMonitor's service-side LLM use routes through a **LiteLLM gateway** (`src/worldmonitor/llm/`),
which is the **single auditable LLM-egress point** (a data-sovereignty control — the only place entity
data can leave the perimeter for an external model). On top of the gateway, the operator gets a
**confidential selector**: a runtime-switchable control (UI/control-surface/API) that changes the live
LLM mode, where **each mode is labeled with its confidentiality status at selection time** so the
operator always knows whether data leaves the perimeter when they pick it. Exactly **three modes**:
1. **Local — confidential** (Ollama, **no egress**) = **DEFAULT**.
2. **Claude headless** (`claude -p` Claude-Code headless via an OpenAI-compatible shim behind LiteLLM;
   data → Anthropic) — **off by default**, external opt-in. Carries a **ToS/brittleness caveat**: a
   consumer subscription used programmatically — may break or violate terms; label it as such.
3. **OpenRouter** (data → OpenRouter; external opt-in) — off by default.

External modes (2 + 3) send entity data **off-perimeter** → opt-in only, clearly labeled. Hermes'
**agent-side** model choice is Hermes' own concern (`hermes model`); this decision governs
**WorldMonitor's service-side** egress and the operator's visible control over it.
- **Reversal cost:** low — modes are LiteLLM routes behind one selector; adding/removing a provider is
  config. Ollama-only is always a safe fallback (zero egress).
- **Revisit trigger:** Anthropic ships a supported headless/API path (drop the `claude -p` shim
  caveat); or a new provider is needed (add a mode + its confidentiality badge).

## Reversible defaults for the smaller open points
`docs/50_AGENT_LAYER.md` §6 listed open points; per build-discipline we pick sensible reversible
defaults with revisit triggers rather than manufacture a human fork:
- **Where Hermes runs:** the **always-on host as a compose service** (default). Reversal: move to a
  separate VPS/serverless — config + network reachability to the MCP URL. Revisit trigger: the host
  is resource-constrained by Hermes load, or isolation requires a separate box.
- **Report cadence:** **daily** brief, **configurable** (Hermes cron expression). Reversal: change the
  cron. Revisit trigger: operator wants intra-day or event-driven briefs.
- **Fine-tuning location (§4b) / safe auto-tune ranges (§4c):** still **OPEN and OUT of Phase 3** —
  unchanged; resolved with the user when Phase 6 begins.

## Slice breakdown (S1–S5, independently mergeable)

| Slice | Scope | Done-when | Status |
|---|---|---|---|
| **S1** | **MCP HTTP transport + Zitadel bearer auth** on `mcp/server.py` (the security boundary). Reuse `ZitadelTokenVerifier`; mirror REST's bearer pattern; stdio retained; read-only 4-tool set unchanged; service-principal role scoping. | A bearer-authenticated HTTP MCP surface exposes exactly the 4 read tools; no anonymous access; stdio still works. | **Specced now** — ADR 0090 + `GATE_S1_MCP_HTTP_AUTH_SPEC.md`. |
| **S2** | **LiteLLM gateway** in `src/worldmonitor/llm/`: the three-mode **confidential selector** (Local default + Claude-headless opt-in + OpenRouter opt-in), per-mode confidentiality badge surfaced to the operator, and **per-call egress logging** at the single gateway choke point. | Operator can switch modes live, always sees each mode's confidentiality status, and every external call is logged at the gateway. | Scoped; spec at S2. |
| **S3** | **Hermes deploy + connect**: compose service on the always-on host; MCP via `url`+`bearer` with `tools.include:[get_entity,get_neighbors,get_provenance,find_paths]`; verify Ollama + one external model via `hermes model`. | Hermes lists exactly the 4 read tools and answers a graph question end-to-end. | Scoped; spec at S3 (needs S1). |
| **S4** | **First scheduled brief**: Hermes cron → "what changed / who connects to entity X" → Telegram. | A scheduled brief arrives in Telegram (the phase done-condition). | Scoped; spec at S4 (needs S1+S3). |
| **S5** | **Operator console (in-app chat)**: a **server-rendered chat page in the existing FastAPI/Jinja app** (NOT a SPA, NOT Open WebUI, NOT Hermes' bundled :9119 dashboard) + WorldMonitor's **first SSE streaming endpoint** proxying **Hermes' OpenAI-compatible gateway** (`hermes gateway run`, :8642, `/v1/chat/completions` streaming + `/api/jobs`). Reuses the existing Zitadel browser login + session cookie + CSRF. Operator can run general queries, run agents, and start reports/investigations; the S2 confidential selector lives on this page. Chat routes **through Hermes** so it gets the agent (our MCP tools + skills); Hermes' model = our LiteLLM gateway, so sovereignty holds. | Operator opens an authenticated in-app chat, asks a graph question, and streams an agent answer; can trigger a report/investigation. | Scoped; spec at S5 (needs S1+S2+S3). **Deliberately pulls forward a Phase-6-deferred UI item** (see below). |

S2 and S1 are independent (S2 is service-side LLM; S1 is the MCP boundary) and may land in either
order; S3 needs S1; S4 needs S1+S3; S5 needs S1+S2+S3.

**Note — S5 unlocks a deferred item.** "UI beyond the current phase / custom React later" is on the
`docs/10_ARCHITECTURE.md` §8 deferred list (roadmap Phase 6). S5 pulls a *constrained* version forward
at the user's explicit direction (2026-06-30): a server-rendered chat page in the existing app — **not**
the full graph-explorer/dashboard SPA, which stays deferred. This is recorded so the unlock is
deliberate, not scope creep.

## Explicitly OUT of Phase 3 (deferred, human-gated)
- **§4b model fine-tuning from trajectories** (GPU path; fine-tuning location still OPEN).
- **§4c agents tuning WorldMonitor params/rules/ER-thresholds/scores** — person-affecting; always
  human sign-off; bounded auto-tune ranges still OPEN.
- **All active / write / enrich / run-connector / scoring MCP tools.** Phase 3 keeps the MCP surface
  **read-only** (the 4 ADR 0063 tools). Any write/active tool is a separate, individually-gated slice
  behind the capability gates (`docs/10` §6) — **not** in Phase 3.
These remain **Phase 6** and are flagged here so no Phase-3 slice quietly pulls them in.

## Consequences
- The MCP read contract (ADR 0062/0063) is the stable seam; Hermes is a swappable consumer behind it.
- One new network ingress (the HTTP MCP port) — secured by S1 before it is ever exposed.
- One new auditable egress choke point (the LiteLLM gateway) with operator-visible confidentiality.
- Not person-affecting (read surface + agent reporting). No new datastore. Single-tenant (D1/ADR 0042).

## Reversibility
All three decisions and both smaller defaults are reversible (see each §). No data-shape lock-in,
nothing irreversibly public-facing, no deletion. The irreversible/person-affecting subsystems are held
OUT of the phase. **No human fork** — proceed to S1.
</content>
</invoke>
