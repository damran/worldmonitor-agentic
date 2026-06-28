# 0070 — StreamConnector (Bluesky Jetstream) + the G8 cursor / long-running-driver protocol (Phase-2 Stage-3 slice 5)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** `gate/3f-stream-connector` (off `master`). The Phase-2 STREAM path — the audit's "single biggest
  contract stress" (Q3 / G8). Uses ADR 0019 (periodic re-batch resolution — unchanged here).
- **human_fork:** false (the cursor model, scheduling, and post→Article mapping are reversible defaults).

## Context

`Mode.STREAM` exists but nothing handles it — every connector flows through the cadence-based
`run_due_ingests`. `run_ingest` already **bounds** any forever-yielding `collect()` (its `timeout` /
`max_records` cut a run off — `ingest.py:218-223`), so the run won't hang. **What's missing is cursor
persistence**: a stream that restarts each window loses its place (audit X1). This slice adds the
**resume protocol** and ships the first stream — **Bluesky Jetstream** (verified:
`wss://jetstream2.us-east.bsky.network/subscribe`, query params `wantedCollections` + `cursor` [time-based,
unix-µs], JSON events over WebSocket, absent cursor → live-tail).

## Decision

**A self-bounding windowed `collect()` + a cursor that survives windows + a driver that keeps a stream warm —
reusing the existing `run_ingest`/cadence machinery, not a parallel runtime.**

### 1. The cursor protocol (the G8 core)
- **`RawRecord` gains `cursor: str | None = None`** — the source position *after* this record. Optional +
  default `None` → **every existing batch connector is unaffected** (they never set it).
- **`run_ingest` tracks the last *committed* record's cursor** and returns it on **`IngestStats.last_cursor`**
  (new field, default `None`). Only the cursor of records that survived a window commit is reported (so a
  crash mid-window re-reads from the last durable cursor — at-least-once, no silent gaps).
- **`ConnectorInstance` gains a nullable `stream_cursor` column** (migration `0007_stream_cursor`).
- **The driver** (`_ingest_instance` / `_finalize`):
  - **injects** the saved cursor *before* the run: `config["_cursor"] = instance.stream_cursor` (only set
    when non-null) — a stream `collect()` reads `config.get("_cursor")` to resume;
  - **persists** the new cursor *after* a run: `instance.stream_cursor = stats.last_cursor` (only when the
    run reported one) — transactional, in the same finalize commit as the `TaskRun` result;
  - **keeps streams warm:** for `manifest.mode is Mode.STREAM`, `next_run = now` after a window (re-run ASAP
    → continuous windowed consumption) instead of `now + cadence`. Backoff-on-failure is unchanged.

### 2. `StreamConnector` — a self-bounding windowed base (`plugins/stream.py`)
A `Connector` subclass whose `collect()` is **bounded by the connector itself** (so it returns — it is NOT a
forever-blocking iterator the driver has to interrupt): it consumes the source for at most `window_seconds`
(or `max_events`), yielding one `RawRecord` per event (with the event's `cursor`), then returns. The
transport is behind an **injectable seam** `_event_source(cursor, window_seconds, max_events) -> Iterator[dict]`
— production opens the WS; **tests inject canned events** (no live network, no deep WS mocking). Subclasses
implement `map()` + the manifest + the schema.

### 3. The Bluesky Jetstream connector (`plugins/connectors/bluesky/`)
`Mode.STREAM`, `EXTERNAL_IMPORT`, **`PASSIVE`** (read-only firehose — the driver refuses ACTIVE). The real
`_event_source` connects to the configured Jetstream endpoint over the async `websockets` lib (already
locked), driven from the sync `collect()` via `asyncio.run` for the bounded window, with `wantedCollections`
+ the resume `cursor` as query params. `config.schema.json`: `wanted_collections` (default
`["app.bsky.feed.post"]`), `window_seconds` (int, bounded default e.g. 20), `max_events` (int, bounded
default e.g. 500), optional `endpoint` (default the public Jetstream host). `map()` → an FtM-native
**`Article`** per post (mirrors the Feed connector — `text`→`title`/`bodyText`, `did`/handle→`author`,
`createdAt`→`publishedAt`, the `at://` URI→`sourceUrl`; id `bluesky-{did}-{rkey}`) with provenance; only
post-create commits map (deletes/other skipped). A social post as `Article` is the FtM-native fit; a richer
`wm:SocialPost` extension is a noted future refinement (not v1 — no L2 change).

### 4. Safety
- **Bounded**: the window (`window_seconds`/`max_events`) bounds each run; `run_ingest`'s `timeout`/
  `max_records` remain a backstop; a per-page/event byte sanity cap on the raw event.
- **Hostile input**: each WS event is treated as hostile bytes → `RawRecord.data`; `map()` validates via FtM
  (`validate_or_raise`) and fail-soft-skips a malformed event. The WS host is operator-config (a fixed
  trusted firehose), not attacker input → `assert_public_host` on the endpoint (defense-in-depth) but the
  `guarded_stream` HTTP-redirect machinery doesn't apply to a WS.
- **Read-only**: raw → landing, candidates → ER queue; never the graph.

## Alternatives considered
- **A separate long-running per-stream task / process.** More faithful to "always-on" but a parallel runtime
  with its own supervision; the windowed-cadence reuse is simpler and crash-safe (each window is a bounded,
  checkpointed `TaskRun`). Deferred unless window latency proves inadequate.
- **Forever-blocking `collect()` interrupted by `run_ingest`'s timeout.** Fragile: a quiet stream blocks the
  iterator so the deadline isn't checked between records. Self-bounding `collect()` (returns after the window)
  is deterministic.
- **Cursor in a separate checkpoint table / in the connector's config.** A nullable column on
  `ConnectorInstance` is transactional, per-instance, and needs no new table; config is UI-owned + encrypted.
- **`websocket-client` (sync) dep.** `websockets` is already locked; reuse it via `asyncio.run` per window.

## Consequences
- The platform gains its first **real-time STREAM** source, and the **G8 resume protocol** any future stream
  reuses. The driver run-model now keeps a stream warm (windowed, checkpointed) without a parallel runtime.
- **Core touch (behaviour-preserving for batch):** `RawRecord.cursor` + `IngestStats.last_cursor` (both
  optional/`None`), `run_ingest` cursor tracking, `ConnectorInstance.stream_cursor` (+ migration), and the
  driver's stream-only inject/persist/`next_run=now`. Every existing batch connector + the driver/ingest
  batch path are **unchanged** (the new fields default `None`; the stream behaviour is gated on
  `manifest.mode is Mode.STREAM`). **New dep:** `websockets` (explicit; already resolved).
- **Migration** `0007_stream_cursor` (the drift guard must pass). **Not person-affecting.** **Single-tenant.**

## Reversibility
Reversible: drop the connector + the stream branch; the `stream_cursor` column + the optional record/stats
fields are inert for batch. Reversal cost: low-medium (one migration to revert). Revisit triggers: window
latency too high → a dedicated long-running task; richer post semantics → a `wm:SocialPost` extension;
back-pressure / volume → rate-limit or a queue.

## Invariant gate note
Not an ER/merge/canonical-id invariant (a candidate-emitting connector) → no `@given` mandatory; provenance
IS exercised (every mapped Article carries `prov_*`). **Failing-test-first:** `RawRecord.cursor` +
`IngestStats.last_cursor` exist and default `None`; `run_ingest` reports the last **committed** record's
cursor; the driver **injects** `stream_cursor`→`config["_cursor"]` and **persists** `stats.last_cursor`→
`stream_cursor`, and sets `next_run=now` for `Mode.STREAM` (and `now+cadence` for batch — unchanged); the
Bluesky connector is `STREAM`/`PASSIVE`, its `collect()` resumes from `config["_cursor"]`, is bounded by
`window_seconds`/`max_events`, and `map()` emits an FtM `Article`+provenance over canned events; the
migration drift-guard passes; **every existing batch connector + driver/ingest test stays green**. All over
an injected fake `event_source` + a testcontainer Postgres — no live firehose.
