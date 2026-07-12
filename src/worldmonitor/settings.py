"""Application settings — 12-factor: every value comes from the environment.

Field names map case-insensitively to env vars (e.g. ``zitadel_domain`` <-
``ZITADEL_DOMAIN``). Nothing here carries a real secret; defaults are safe
placeholders so the app can boot and serve ``/health`` before the stack is
fully provisioned. See ``.env.example``.
"""

import logging
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The runtime safety guards the enforcement switch (ADR 0109) can toggle. Each name maps to a
# `settings.enforce_<name>` override field and an `is_enforced("<name>")` call at the guard's
# choke point. Add a new guard here + its `enforce_<name>` field + one `is_enforced` check.
_ENFORCEABLE_GUARDS = ("merge_guard", "erasure_authorization")

# Placeholder/weak markers a non-development boot refuses (ADR 0061). A secret the app
# reads is rejected if it is empty or contains one of these — the realistic "forgot to
# replace change-me" footgun. NOT an entropy/strength check (a strong-looking but weak
# password passes anyway); a real Fernet key / any non-placeholder value is accepted.
_PLACEHOLDER_SECRET_MARKERS = ("change-me", "worldmonitor123")

# Local/CI environments where placeholder secrets are allowed (the unit suite boots create_app with
# environment="test"). Anything NOT in this set — production, staging, or an unknown/typo'd value —
# enforces the placeholder check (fail CLOSED on unknown). ADR 0061.
_LOCAL_ENVIRONMENTS = ("development", "test")


class Settings(BaseSettings):
    """Process configuration, loaded from the environment (and ``.env`` in dev)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "development"

    # --- Zitadel / OIDC (auth-gated from day one) ---
    zitadel_domain: str = ""
    zitadel_client_id: str = ""
    # Confidential-client secret for the browser OIDC authorization-code flow (ADR 0068). Empty
    # default; fail-closed in prod when auth is configured (validate_production_secrets).
    zitadel_client_secret: str = ""
    # Signs the Starlette SessionMiddleware cookie that carries the browser session principal
    # (ADR 0068). An empty/guessable key would let anyone forge a session — fail-closed in prod.
    session_secret_key: str = ""
    # Absolute public base URL (e.g. ``https://wm.example.com``) used to build the absolute OIDC
    # ``redirect_uri`` (``app_base_url`` + ``/auth/callback``) — NOT a Host-derived URL (ADR 0068).
    app_base_url: str = ""

    # --- MCP server transport (Phase-3 S1, ADR 0090) ---
    # Default stdio (ADR 0063, unchanged: no port, local trust boundary). Set to
    # ``streamable-http`` to serve the authenticated HTTP transport a remote Hermes connects to;
    # HTTP is fail-closed (always bearer-gated by the Zitadel verifier).
    mcp_transport: str = "stdio"
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = 8765
    # This MCP server's own public base URL (RFC 9728 protected-resource metadata). Optional;
    # empty -> omitted. e.g. ``https://mcp.wm.example.com``.
    mcp_resource_server_url: str = ""

    # --- Backing services (URLs only; clients land in later phases) ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    postgres_dsn: str = "postgresql://worldmonitor:worldmonitor@localhost:5432/worldmonitor"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "worldmonitor"
    minio_secret_key: str = ""
    minio_secure: bool = False
    landing_bucket: str = "landing"

    # --- Entity resolution: catastrophic-merge guard (ADR 0024 → ADR 0031) ---
    # "block" (DEFAULT — production posture) parks flagged (oversized / PEP /
    # sanctioned) clusters in pending_review and never writes them; a human reviews
    # them via the sign-off mechanism (ADR 0031). "alert" was the TEMPORARY
    # build-phase mode that wrote flagged merges anyway with a durable merge_alerts
    # trail. The guard EVALUATION (resolution/review.py) is identical in both modes —
    # only the ACTION on a flagged cluster differs. Returned to "block" with human
    # sign-off, fulfilling the ADR 0024 obligation.
    merge_guard_mode: Literal["alert", "block"] = "block"

    # --- Enforcement switch (ADR 0109) ---
    # Master switch for the runtime safety guards. "strict" (DEFAULT — production posture)
    # enforces every guard; "off" bypasses all of them. Per-guard `enforce_*` overrides win
    # over the profile when set (None = inherit). Disabled guards are logged at startup
    # (log_enforcement_status) so an "off" profile can't silently ride into production.
    # This is operator config for a single-tenant, self-hosted deploy: keep it "strict" in
    # production; set "off" (or per-guard False) only in a dev/test instance on test data.
    enforcement_profile: Literal["strict", "off"] = "strict"
    enforce_merge_guard: bool | None = None
    enforce_erasure_authorization: bool | None = None

    def is_enforced(self, guard: str) -> bool:
        """Is ``guard`` active? A per-guard ``enforce_<guard>`` override wins; otherwise the
        guard follows ``enforcement_profile`` ("strict" -> on, "off" -> off)."""
        override = getattr(self, f"enforce_{guard}", None)
        if override is not None:
            return bool(override)
        return self.enforcement_profile == "strict"

    def disabled_enforcements(self) -> list[str]:
        """The guards currently bypassed (for the startup warning)."""
        return [g for g in _ENFORCEABLE_GUARDS if not self.is_enforced(g)]

    def log_enforcement_status(self) -> None:
        """Warn (once, at boot) if any safety guard is disabled — never silent."""
        disabled = self.disabled_enforcements()
        if disabled:
            logger.warning(
                "ENFORCEMENT: safety guards DISABLED (NOT production-safe) — %s "
                "[enforcement_profile=%s]. Set enforcement_profile=strict for production.",
                ", ".join(disabled),
                self.enforcement_profile,
            )

    # Max ER-queue candidates resolved per batch (ADR 0026). resolve_pending drains
    # the pending queue in windows of this size, committing per batch, so memory and
    # per-pass cost are bounded. Dedup is WITHIN a batch; cross-batch / incremental
    # dedup against already-resolved entities is deferred to the ER-streaming gate.
    resolve_batch_size: int = Field(default=1000, gt=0)

    # --- Ingest bounds + windowing (ADR 0027) ---
    # run_ingest drains a connector's collect() with these bounds so a stream that
    # never returns can't hang the run, and a long import commits its progress.
    #   ingest_commit_every   — land/map/enqueue this many records, then commit.
    #   ingest_timeout_seconds — wall-clock deadline on a single run; <= 0 disables.
    #   ingest_max_records     — optional hard cap on records pulled; None = no cap
    #                            (windowed commits already bound the blast radius).
    ingest_commit_every: int = Field(default=1000, gt=0)
    ingest_timeout_seconds: float = Field(default=1800.0, ge=0)
    ingest_max_records: int | None = Field(default=None, gt=0)

    # --- Ingest driver cadence (ADR 0029) ---
    # The long-running driver runs each enabled connector instance every
    # ``ingest_cadence_seconds`` and resolves the queue every
    # ``resolve_cadence_seconds`` (an INDEPENDENT cadence, not fired per ingest);
    # it wakes every ``driver_tick_seconds`` to check what is due. Single global
    # cadence for now (a per-connector cadence column is deferred).
    ingest_cadence_seconds: int = Field(default=3600, gt=0)
    resolve_cadence_seconds: int = Field(default=300, gt=0)
    driver_tick_seconds: float = Field(default=30.0, gt=0)
    # On ingest FAILURE the instance stays retryable (status="enabled") and is rescheduled with an
    # exponential backoff (ADR 0054): backoff = min(base * 2**(consecutive_failures-1), max). A
    # success resets the streak (back to ``ingest_cadence_seconds``). Connector SCHEDULING only.
    ingest_retry_base_seconds: int = Field(default=60, gt=0)
    ingest_retry_max_seconds: int = Field(default=3600, gt=0)
    # After this many CONSECUTIVE ingest failures an instance is hard-disabled (status="error",
    # terminal — the due-query selects only "enabled"), instead of retrying forever (ADR 0074
    # extends ADR 0054). A success resets the streak. ``0`` disables hard-disable (retry-forever).
    ingest_max_consecutive_failures: int = Field(default=10, ge=0)
    # Finished (ok/error) task_run rows older than this are pruned on driver startup
    # so the history table does not grow without bound (ADR 0029 follow-up). 0 disables.
    task_run_retention_days: int = Field(default=30, ge=0)
    # Dead-letter (``ingest_dead_letter``) rows older than this are pruned on driver startup so the
    # replayable error-audit table does not grow without bound (Gate B-4d / ADR 0053). 0 disables.
    dead_letter_retention_days: int = Field(default=30, ge=0)
    # The driver now runs its two prunes (``prune_task_runs`` + ``prune_dead_letters``) on a
    # PERIODIC cadence INSIDE the loop instead of only at startup (ADR 0075 D1), so a driver up for
    # weeks keeps ``task_run`` / ``ingest_dead_letter`` bounded mid-uptime. The first tick fires
    # (``last_maintenance is None``) so the boot-time prune is preserved; ``recover_stale`` stays
    # startup-only (NOT wrapped). The prunes' DELETE semantics are unchanged — only WHEN they run.
    maintenance_cadence_seconds: int = Field(default=3600, gt=0)
    # Wall-clock deadline on one resolve pass (ADR 0075 D2), mirroring ``ingest_timeout_seconds``
    # — ``<= 0`` disables it (drain to exhaustion exactly as today). ``resolve_pending`` checks it
    # BETWEEN batches; each batch is committed first (ADR 0026), so a timed-out pass loses no work
    # and the remaining backlog resumes on the next cadence tick.
    resolve_timeout_seconds: float = Field(default=600.0, ge=0)
    # After this many CONSECUTIVE non-blocking ``_resolve_lock`` skips (a prior pass still holds the
    # lock) the driver escalates the skip log from info to WARNING (ADR 0075 D3), so a wedged
    # resolve pass surfaces instead of silently starving resolution. A successful acquire resets it.
    resolve_lock_skip_alert_threshold: int = Field(default=3, gt=0)

    # --- Driver supervision & containerization (Gate B-4c / ADR 0051) ---
    # The driver writes a last-tick heartbeat FILE (per-container; not a table) once per loop
    # iteration; `python -m worldmonitor.runner.driver --healthcheck` reads ONLY this file and
    # exits 0 (alive) / 1 (missing-or-stale) — the container HEALTHCHECK. A stale heartbeat makes
    # a stalled pipeline detectable even while /health still echoes ok (the audit's false-confidence
    # signal). ``driver_heartbeat_stale_seconds`` is a safe multiple of ``driver_tick_seconds``
    # (default 90s ≈ 3 missed 30s ticks) so a single slow tick does not flap the healthcheck.
    driver_heartbeat_path: str = "/var/run/worldmonitor/driver.heartbeat"
    driver_heartbeat_stale_seconds: float = Field(default=90.0, gt=0)
    # Per-store bound on each /ready probe (Postgres/Neo4j/MinIO) so a hung store cannot hang
    # the readiness endpoint forever (spec §3.4).
    readiness_probe_timeout_seconds: float = Field(default=5.0, gt=0)
    # TCP port the driver's Prometheus ``/metrics`` exporter binds (Gate H-8c / ADR 0076). Started
    # ONCE at the top of ``run_forever`` (after recover_stale, before the loop) on a daemon thread,
    # so the H-8a/H-8b health signals (instances-in-error, the in-memory resolve-lock-skip counter,
    # the latest resolve stopped_reason, plus the queue/dead-letter/task/graph counts) are
    # scrapeable. ``0`` DISABLES the exporter entirely (no thread, no bound port) — today's
    # behaviour and the reversal lever. Default 9108 is clear of the node_exporter/Prometheus
    # defaults 9100/9090; bound 0.0.0.0 in-container only (the compose service publishes no host).
    driver_metrics_port: int = Field(default=9108, ge=0)

    # --- Fail-closed sensitivity guard (Gate E / ADR 0047) ---
    # The guard's sensitive-topic SET is loaded programmatically from FtM's own
    # ``registry.topic.RISKS`` (never configured — deny-by-default cannot be opened);
    # these knobs tune only the Stage-2 k-hop traversal and the Stage-3 Chow abstain band.
    #   sensitivity_khop_depth   — Stage-2 graph traversal depth, f-string-inlined into the
    #                              ``[*1..k]`` variable-length bound (int-validated, never a
    #                              $param). ``0`` disables Stage 2 (the kill-switch).
    #   sensitivity_abstain_low  — Stage-3 Chow band lower bound (INCLUSIVE).
    #   sensitivity_abstain_high — Stage-3 Chow band upper bound (EXCLUSIVE). ``low == high``
    #                              ⇒ an empty half-open interval ⇒ the band is OFF (the default).
    # The band defaults to ``[0.92, 0.92)`` (empty) so slice-2 over-parks NOTHING until a human
    # tunes it; ``low > high`` is rejected by the validator below (an inverted, nonsensical band).
    # le=4 caps the f-string-inlined ``[*1..k]`` traversal (exponential in k) so a misconfiguration
    # cannot stall the resolve hot path on a dense graph (spec §15 / ADR 0047 §6); 0 = kill-switch.
    sensitivity_khop_depth: int = Field(default=1, ge=0, le=4)
    sensitivity_abstain_low: float = Field(default=0.92, ge=0.0, le=1.0)
    sensitivity_abstain_high: float = Field(default=0.92, ge=0.0, le=1.0)

    # --- GeoNames connector: local-path confinement (Gate H-6/H-7 / ADR 0052) ---
    # The GeoNames connector accepts an optional local ``path`` override (load-bearing for the
    # VA.txt fixture + offline dev). Left unconstrained it is an arbitrary file read (LFI), so the
    # runtime confines it to an allowlist DIRECTORY — the security boundary is THIS check, not the
    # JSON schema (which doubles as the self-service UI-form driver and is bypassable by a direct
    # caller).
    #   geonames_allowed_path_dir — allowlist base dir for the local ``path`` override. DEFAULT
    #     EMPTY ⇒ the ``path`` override is rejected entirely (DEFAULT-DENY); production that never
    #     sets it cannot be tricked into a local read. Dev/test points it at the fixtures dir. The
    #     runtime resolves both the allowlist and the candidate via realpath (defeating ``..`` AND
    #     symlinks) and requires the candidate to be inside the allowlist.
    #   geonames_max_path_bytes — defense-in-depth size cap (256 MiB) on a confined local read;
    #     an over-cap file is rejected before it is read.
    geonames_allowed_path_dir: str = ""
    geonames_max_path_bytes: int = Field(default=268_435_456, gt=0)

    # --- Online-migration safety (ADR 0084 / audit M-5) ---
    # Dialect-aware lock_timeout + statement_timeout applied by env.py::_run BEFORE every
    # migration batch via SET LOCAL (scoped to the migration transaction, reverts on
    # commit/rollback — no bleed onto the shared app connection). Postgres-only GUCs:
    # silently skipped on SQLite and other non-Postgres dialects. Closes the M-5 finding:
    # a migration that cannot acquire its ACCESS EXCLUSIVE lock FAILS FAST instead of
    # stalling the driver's enqueue path indefinitely.
    #
    #   migration_lock_timeout_ms     — abort if DDL lock cannot be acquired within this
    #                                   many ms. DEFAULT 3000 (3 s): fail-fast for online
    #                                   execution. 0 = opt-out (Postgres default = no
    #                                   timeout). Use 0 in the migrate-while-stopped runbook
    #                                   where no live traffic competes for the lock.
    #   migration_statement_timeout_ms — hard wall-clock cap on ANY single SQL statement
    #                                   in the migration transaction (including long index
    #                                   builds). DEFAULT 0 = do NOT set (off by default;
    #                                   lock_timeout is the key guard).
    migration_lock_timeout_ms: int = Field(default=3000, ge=0)
    migration_statement_timeout_ms: int = Field(default=0, ge=0)

    # --- Landing-zone orphan GC (ADR 0083 / audit M-6) ---
    # The GC scans the landing zone for S3 objects not referenced by either er_queue_item or
    # ingest_dead_letter, and optionally deletes them (the backstop for the put-before-commit race).
    # All three settings default to the SAFE OFF position so this gate adds no behaviour change.
    #
    #   landing_gc_enabled          — master gate; when False the GC pass never runs in the
    #                                 maintenance cadence (DEFAULT False: no behaviour change).
    #   landing_gc_delete_enabled   — deletion opt-in; when False the pass is REPORT-ONLY —
    #                                 orphan counts/bytes are computed and exposed via Prometheus
    #                                 but nothing is deleted (DEFAULT False: report-only).
    #   landing_gc_min_age_seconds  — grace window in seconds; objects younger than this are
    #                                 NEVER swept, closing the put-before-commit race.
    #                                 DEFAULT 86400 (1 day) — generous enough that any in-flight
    #                                 put-before-commit is always safe, even on a slow host.
    #                                 Set to 0.0 to disable the grace window (sweep all orphans).
    landing_gc_enabled: bool = False
    landing_gc_delete_enabled: bool = False
    landing_gc_min_age_seconds: float = Field(default=86400.0, ge=0.0)

    # --- Projection rebuild-and-diff guard (ADR 0102 D2 / Gate 3a-ii-B) ---
    # The scheduled full-fold divergence guard: on its own cadence, folds the WHOLE statement
    # log (project(full_rebuild=True)) into an operator-provisioned, ISOLATED second Neo4j and
    # measures how much of the LIVE graph the fold cannot explain. DORMANT by default — a
    # runtime no-op, NOT a boot failure (no model_validator; validate_production_secrets stays
    # untouched, ADR 0061 FROZEN). Neo4j Community is single-database (ADR 0094 D5): there is no
    # free shadow DB on the live instance, so a distinct target MUST be a distinct instance.
    #
    #   projection_diff_enabled          — master gate; the guard runs ONLY when this is True
    #                                       AND projection_diff_neo4j_uri is non-empty.
    #   projection_diff_neo4j_uri        — the isolated diff target's Neo4j URI. Empty (default)
    #                                       ⇒ dormant no-op even if enabled=True (D2 B1 posture).
    #   projection_diff_neo4j_user       — the diff target's Neo4j user.
    #   projection_diff_neo4j_password   — SecretStr so it never echoes in repr/log/traceback;
    #                                       needed at USE, not boot (like sandbox_runner_secret).
    #   projection_diff_cadence_seconds  — minimum seconds between guard runs. DEFAULT 86400
    #                                       (daily; a full fold is O(log size), so daily is ample).
    projection_diff_enabled: bool = False
    projection_diff_neo4j_uri: str = ""
    projection_diff_neo4j_user: str = ""
    projection_diff_neo4j_password: SecretStr = SecretStr("")
    projection_diff_cadence_seconds: float = Field(default=86400.0, gt=0)

    # --- ACTIVE heavy-tool sandbox gate (ADR 0072 §1) ---
    # A CliTool connector declares a ``sandbox`` level ("subprocess" | "container"). A
    # ``sandbox=="container"`` connector (e.g. nmap — an un-sandboxed network scanner from the host
    # violates "heavy CLI tools in containers") is REFUSED by the operator-run path until a real
    # container/egress sandbox exists. This flag is that gate; DEFAULT ``False`` (no container
    # runtime in v1) means every container-level tool's EXECUTION is refused
    # (SandboxUnavailableError -> REST 409) BEFORE any subprocess/landing. Subprocess-level tools
    # (whois/dig) are unaffected. When the Stage-4 container sandbox lands, flipping this True lets
    # those tools run — no connector change. Not person-affecting; single-tenant.
    container_sandbox_enabled: bool = False

    # --- Sandbox-runner sidecar (ADR 0077 §D3, Slice 1) ---
    # Where the app-side ContainerRunner delegates a container-level CLI tool's EXECUTION: a tiny
    # in-network FastAPI sidecar exposing ``POST /run``. DEFAULT EMPTY ⇒ "not configured": with the
    # flag ON but no URL/secret the operator-run path STILL refuses container tools
    # (SandboxUnavailableError) — the flag alone never runs nmap un-sandboxed (INV-2). Set both
    # (plus ``container_sandbox_enabled=True``) to route container tools through the sidecar.
    sandbox_runner_url: str = ""
    # Shared secret the ContainerRunner sends in the ``X-Sandbox-Secret`` header and the sidecar
    # compares constant-time. A ``SecretStr`` so it never echoes in a repr/log/traceback; the
    # plaintext is read only via ``.get_secret_value()`` at the routing point. NOT added to
    # ``validate_production_secrets`` (ADR 0061 frozen) — it is required at routing, not at boot.
    sandbox_runner_secret: SecretStr = SecretStr("")

    # --- LLM gateway (Phase-3 S2, ADR 0091) ---
    # The single service-side LLM-egress choke point — one gateway method routes every
    # call through litellm, attaches the active mode's confidentiality label, and writes
    # a per-call egress record before the provider is contacted. Three modes (user-finalized,
    # locked by ADR 0091 §2):
    #   local           — Ollama loopback, confidential, no egress (DEFAULT).
    #   claude_headless — claude -p shim, external egress → Anthropic (ToS-gray caveat).
    #   openrouter      — openrouter/<model>, external egress → OpenRouter.
    llm_mode: Literal["local", "claude_headless", "openrouter"] = "local"
    # Ollama (LOCAL mode) — loopback address + local model name. No key, no egress.
    llm_ollama_base_url: str = "http://localhost:11434"
    llm_ollama_model: str = "llama3.2"
    # OpenRouter (OPENROUTER mode) — key as SecretStr so it never echoes in repr/log.
    llm_openrouter_api_key: SecretStr = SecretStr("")
    llm_openrouter_model: str = "openai/gpt-4o"
    # Claude headless (CLAUDE_HEADLESS mode) — argv-list subprocess; ToS-gray caveat.
    llm_claude_binary: str = "claude"
    llm_claude_model_label: str = "claude"
    llm_claude_timeout_seconds: float = Field(default=30.0, gt=0)
    # Master egress-log toggle. When False, emit() is skipped; the write-before-call
    # ordering invariant is never bypassed (ADR 0091 §3). Default True.
    llm_egress_log_enabled: bool = True
    # Durable, append-only LLM-egress audit (ADR 0105 / F2). When True (and the master
    # llm_egress_log_enabled is also True), each crossing writes a durable Postgres row; an
    # EXTERNAL crossing is fail-closed on that write (DB down / sink unwired ⇒ refuse). Default
    # False: DORMANT — behaviour is byte-identical to L1 until an operator enables it after
    # applying migration 0011 and confirming the Postgres sink.
    llm_egress_durable_enabled: bool = False

    # --- Secrets ---
    # Fernet key for encrypting connector-instance config at rest. Required in
    # any environment that persists connector configs; empty default fails fast.
    config_encryption_key: str = ""
    # Comma/whitespace-separated decryption-only OLD keys (ADR 0058). During a key
    # rotation the previous CONFIG_ENCRYPTION_KEY moves here so tokens written under it
    # still decrypt; empty default => one-key cipher == today's behaviour.
    config_encryption_key_fallbacks: str = ""

    @model_validator(mode="after")
    def _validate_abstain_band(self) -> "Settings":
        """Reject an inverted Chow abstain band (``low > high``) — spec §6 / ADR 0047 Decision 6.

        ``low == high`` is allowed (the OFF / empty-band configuration); only a strictly inverted
        band is nonsensical. The band never touches ``DEFAULT_MERGE_THRESHOLD`` — it is a distinct
        park-vs-promote axis on an already-formed cluster (spec §3.4).
        """
        if self.sensitivity_abstain_low > self.sensitivity_abstain_high:
            raise ValueError(
                "sensitivity_abstain_low must be <= sensitivity_abstain_high "
                f"(got low={self.sensitivity_abstain_low}, high={self.sensitivity_abstain_high})"
            )
        return self

    @model_validator(mode="after")
    def _validate_grace_window_guard(self) -> "Settings":
        """Fail-closed grace-window guard for the landing-zone GC (ADR 0086 D1).

        Only fires when ``landing_gc_delete_enabled=True`` — report-only mode is purely read
        and safe at any grace.  With deletion enabled, the grace window MUST cover at least one
        full ingest timeout, otherwise a GC pass can delete a put-before-commit object of an
        ingest that is still in-flight (provenance destroyed for a record about to be committed).

        Three unsafe conditions are rejected (fail-closed, NOT silent clamp):

        * ``landing_gc_min_age_seconds == 0``: no grace window + deletion is unsafe.
        * ``ingest_timeout_seconds == 0``: the ingest deadline is disabled ⇒ the in-flight
          window is unbounded; no finite grace is provably safe.
        * ``0 < landing_gc_min_age_seconds < ingest_timeout_seconds``: grace is shorter than
          the maximum in-flight window.

        Boundary ``landing_gc_min_age_seconds >= ingest_timeout_seconds (> 0)`` is accepted.
        """
        if not self.landing_gc_delete_enabled:
            # Report-only (delete=False) is purely read — no provenance can be lost.
            return self
        if self.landing_gc_min_age_seconds == 0:
            raise ValueError(
                "landing_gc_min_age_seconds must be > 0 when landing_gc_delete_enabled=True; "
                "a grace window of zero with deletion enabled can delete a put-before-commit "
                "object before its referencing er_queue row is committed (ADR 0086 D1). "
                "Set landing_gc_min_age_seconds >= ingest_timeout_seconds or disable deletion."
            )
        if self.ingest_timeout_seconds == 0:
            raise ValueError(
                "ingest_timeout_seconds must be > 0 when landing_gc_delete_enabled=True; "
                "when the ingest deadline is disabled (ingest_timeout_seconds=0), the "
                "in-flight window is unbounded and no finite landing_gc_min_age_seconds is "
                "provably safe (ADR 0086 D1). Set a finite ingest_timeout_seconds or disable "
                "landing_gc_delete_enabled."
            )
        if self.landing_gc_min_age_seconds < self.ingest_timeout_seconds:
            raise ValueError(
                f"landing_gc_min_age_seconds ({self.landing_gc_min_age_seconds}) must be >= "
                f"ingest_timeout_seconds ({self.ingest_timeout_seconds}) when "
                "landing_gc_delete_enabled=True; a shorter grace window can delete a "
                "put-before-commit object of an ingest still in-flight (ADR 0086 D1). "
                "Either raise landing_gc_min_age_seconds or lower ingest_timeout_seconds."
            )
        return self

    def validate_production_secrets(self) -> None:
        """Fail closed outside the local/test environments on a placeholder secret (ADR 0061).

        LOCAL environments ``development``/``test`` allow placeholders (the unit suite boots
        ``create_app`` with ``environment="test"`` and no ``.env``) and return immediately. EVERY
        other value — ``production``, ``staging``, or an unrecognized/typo'd string — enforces (fail
        CLOSED on unknown): raise ``ValueError`` naming the field if a secret the app actually reads
        is a placeholder/weak value: ``config_encryption_key`` empty or ``change-me``; or
        ``change-me``/``worldmonitor123`` inside ``postgres_dsn``/``redis_url``/``neo4j_password``/
        ``minio_secret_key``. A plain marker check — NO entropy scoring; a real Fernet key passes.

        ADR 0068: ``session_secret_key`` is required UNCONDITIONALLY (the session cookie is signed
        on every boot, auth wired or not), while ``zitadel_client_secret`` is required only when
        auth is configured (``auth_configured`` — a confidential client cannot ship without it).
        """
        if self.environment in _LOCAL_ENVIRONMENTS:
            return

        if not self.config_encryption_key or self.config_encryption_key == "change-me":
            raise ValueError(
                "config_encryption_key is a placeholder/empty value; set a real secret "
                f"(non-development environment={self.environment!r} refuses placeholders, ADR 0061)"
            )

        guarded_fields = ("postgres_dsn", "redis_url", "neo4j_password", "minio_secret_key")
        for field in guarded_fields:
            value: str = getattr(self, field)
            for marker in _PLACEHOLDER_SECRET_MARKERS:
                if marker in value:
                    raise ValueError(
                        f"{field} contains placeholder/guessable marker {marker!r}; set a real "
                        f"secret (non-development environment={self.environment!r} refuses "
                        "placeholders, ADR 0061)"
                    )

        # UNCONDITIONAL (ADR 0068 security fix): the SessionMiddleware cookie is signed on EVERY
        # boot (with ``session_secret_key`` or, in dev/test only, a per-process random fallback),
        # whether or not OIDC is wired. An empty/guessable ``session_secret_key`` therefore lets
        # anyone forge a browser session REGARDLESS of ``auth_configured`` — so it is required
        # outside the local/test environments unconditionally (NOT gated behind auth_configured).
        if not self.session_secret_key or any(
            marker in self.session_secret_key for marker in _PLACEHOLDER_SECRET_MARKERS
        ):
            raise ValueError(
                "session_secret_key is a placeholder/empty value; set a real secret "
                f"(non-development environment={self.environment!r} refuses placeholders, ADR 0068)"
            )

        # AUTH-GATED (ADR 0068): the confidential-client secret only becomes load-bearing once the
        # browser OIDC flow is actually wired up (``zitadel_domain`` + ``client_id``) — a
        # confidential client cannot ship without its secret. With auth unconfigured no OIDC client
        # is built, so it is never read.
        if self.auth_configured and (
            not self.zitadel_client_secret
            or any(marker in self.zitadel_client_secret for marker in _PLACEHOLDER_SECRET_MARKERS)
        ):
            raise ValueError(
                "zitadel_client_secret is a placeholder/empty value but auth is configured; set a "
                f"real secret (non-development environment={self.environment!r} refuses "
                "placeholders, ADR 0068)"
            )

    @property
    def sqlalchemy_dsn(self) -> str:
        """The Postgres DSN with the psycopg (v3) driver SQLAlchemy expects."""
        if self.postgres_dsn.startswith("postgresql://"):
            return self.postgres_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
        return self.postgres_dsn

    @property
    def oidc_issuer(self) -> str:
        """The OIDC issuer URL Zitadel signs tokens with."""
        return f"https://{self.zitadel_domain}" if self.zitadel_domain else ""

    @property
    def oidc_jwks_uri(self) -> str:
        """Where to fetch Zitadel's signing keys."""
        return f"https://{self.zitadel_domain}/oauth/v2/keys" if self.zitadel_domain else ""

    @property
    def oidc_discovery_url(self) -> str:
        """Zitadel's OIDC discovery document (Authlib registers the client from it, ADR 0068)."""
        return (
            f"https://{self.zitadel_domain}/.well-known/openid-configuration"
            if self.zitadel_domain
            else ""
        )

    @property
    def auth_configured(self) -> bool:
        """True once Zitadel domain + client id are set (auth can be enforced)."""
        return bool(self.zitadel_domain and self.zitadel_client_id)


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
