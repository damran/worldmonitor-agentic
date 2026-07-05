"""Unit tests for application settings (merge-guard mode + ER batch size).

Tests that assert a *declared default* instantiate ``Settings(_env_file=None)`` so they are
independent of a developer's local ``.env`` (e.g. one that sets ``RESOLVE_BATCH_SIZE``), matching
CI, which has no project ``.env``. Tests passing explicit overrides keep a bare ``Settings(...)``.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from worldmonitor.settings import Settings


def test_merge_guard_mode_defaults_to_block() -> None:
    """Production posture (ADR 0031): the guard blocks + parks sensitive merges by default."""
    assert Settings(_env_file=None).merge_guard_mode == "block"  # type: ignore[call-arg]


def test_merge_guard_mode_accepts_alert() -> None:
    assert Settings(merge_guard_mode="alert").merge_guard_mode == "alert"


def test_merge_guard_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Settings(merge_guard_mode="lenient")  # type: ignore[arg-type]


def test_resolve_batch_size_defaults_to_1000() -> None:
    """ADR 0026: resolve_pending windows the queue in batches of this size."""
    assert Settings(_env_file=None).resolve_batch_size == 1000  # type: ignore[call-arg]


def test_resolve_batch_size_accepts_override() -> None:
    assert Settings(resolve_batch_size=250).resolve_batch_size == 250


def test_resolve_batch_size_rejects_non_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(resolve_batch_size=0)


def test_ingest_bounds_defaults() -> None:
    """ADR 0027: windowed commits + a wall-clock deadline + no record cap by default."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.ingest_commit_every == 1000
    assert settings.ingest_timeout_seconds == 1800.0
    assert settings.ingest_max_records is None


def test_ingest_timeout_allows_zero_to_disable() -> None:
    assert Settings(ingest_timeout_seconds=0).ingest_timeout_seconds == 0


def test_ingest_commit_every_rejects_non_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(ingest_commit_every=0)


def test_ingest_max_records_accepts_cap_and_rejects_non_positive() -> None:
    assert Settings(ingest_max_records=500).ingest_max_records == 500
    with pytest.raises(ValidationError):
        Settings(ingest_max_records=0)


def test_driver_cadence_defaults() -> None:
    """ADR 0029: per-connector ingest cadence + an independent resolution cadence."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.ingest_cadence_seconds == 3600
    assert settings.resolve_cadence_seconds == 300
    assert settings.driver_tick_seconds == 30.0


def test_driver_cadence_rejects_non_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(resolve_cadence_seconds=0)
    with pytest.raises(ValidationError):
        Settings(driver_tick_seconds=0)


# -- H-8b: periodic maintenance cadence + resolve liveness (ADR 0075) -------------------------- #


def test_maintenance_cadence_defaults_to_3600() -> None:
    """ADR 0075 D1: the driver runs prune_task_runs + prune_dead_letters on this cadence
    (default hourly) instead of only at startup."""
    assert Settings(_env_file=None).maintenance_cadence_seconds == 3600  # type: ignore[call-arg]


def test_maintenance_cadence_accepts_override() -> None:
    assert Settings(maintenance_cadence_seconds=900).maintenance_cadence_seconds == 900


def test_maintenance_cadence_rejects_non_positive() -> None:
    """gt=0: a zero/negative cadence is nonsensical (it would prune every tick / never)."""
    with pytest.raises(ValidationError):
        Settings(maintenance_cadence_seconds=0)
    with pytest.raises(ValidationError):
        Settings(maintenance_cadence_seconds=-1)


def test_resolve_timeout_defaults_to_600() -> None:
    """ADR 0075 D2: a resolve pass is wall-clock-bounded (default 600s), mirroring
    ingest_timeout_seconds (ge=0, <=0 disables)."""
    assert Settings(_env_file=None).resolve_timeout_seconds == 600.0  # type: ignore[call-arg]


def test_resolve_timeout_accepts_override() -> None:
    assert Settings(resolve_timeout_seconds=120.0).resolve_timeout_seconds == 120.0


def test_resolve_timeout_allows_zero_to_disable() -> None:
    """ge=0 / <=0 disables the bound (drain to exhaustion exactly as today) — mirrors
    ingest_timeout_seconds."""
    assert Settings(resolve_timeout_seconds=0).resolve_timeout_seconds == 0


def test_resolve_timeout_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        Settings(resolve_timeout_seconds=-1)


def test_resolve_lock_skip_alert_threshold_defaults_to_3() -> None:
    """ADR 0075 D3: escalate info->WARNING after this many CONSECUTIVE non-blocking lock-skips."""
    assert Settings(_env_file=None).resolve_lock_skip_alert_threshold == 3  # type: ignore[call-arg]


def test_resolve_lock_skip_alert_threshold_accepts_override() -> None:
    assert Settings(resolve_lock_skip_alert_threshold=5).resolve_lock_skip_alert_threshold == 5


def test_resolve_lock_skip_alert_threshold_rejects_non_positive() -> None:
    """gt=0: a zero/negative threshold would escalate on the first skip / never."""
    with pytest.raises(ValidationError):
        Settings(resolve_lock_skip_alert_threshold=0)
    with pytest.raises(ValidationError):
        Settings(resolve_lock_skip_alert_threshold=-2)


# -- H-8c: Prometheus /metrics exporter port (ADR 0076) ---------------------------------------- #


def test_driver_metrics_port_defaults_to_9108() -> None:
    """ADR 0076 §2.2: the driver exposes the Prometheus /metrics endpoint on this port by default
    (9108 — clear of the node_exporter/Prometheus defaults 9100/9090)."""
    assert Settings(_env_file=None).driver_metrics_port == 9108  # type: ignore[call-arg]


def test_driver_metrics_port_accepts_override() -> None:
    assert Settings(driver_metrics_port=9200).driver_metrics_port == 9200


def test_driver_metrics_port_allows_zero_to_disable() -> None:
    """ge=0: ``0`` DISABLES the exporter entirely (no thread, no bound port) — today's behaviour
    and the opt-out / reversal lever (ADR 0076)."""
    assert Settings(driver_metrics_port=0).driver_metrics_port == 0


def test_driver_metrics_port_rejects_negative() -> None:
    """ge=0: a negative port is nonsensical (it is not a valid TCP port / disable sentinel)."""
    with pytest.raises(ValidationError):
        Settings(driver_metrics_port=-1)


# -- Sandbox-runner sidecar settings (ADR 0077 Slice 1, spec §2.1 / gate.scope) ---------------- #
#
# RED today: ``Settings`` carries neither ``sandbox_runner_url`` nor ``sandbox_runner_secret``, so
# every assertion below fails with ``AttributeError``. The sidecar base URL defaults to "" (empty ⇒
# container-level tools stay refused even with the flag on, INV-2); the shared secret is a
# ``SecretStr`` (never echoed in a repr/log). NEITHER is added to ``validate_production_secrets``
# (ADR 0061 frozen) — the secret is required at the ROUTING point, not at boot.


def test_sandbox_runner_url_defaults_to_empty() -> None:
    """The sidecar base URL defaults to "" — empty means 'not configured' (the routing gate then
    REFUSES container tools even when the flag is on, INV-2)."""
    assert Settings(_env_file=None).sandbox_runner_url == ""  # type: ignore[call-arg]


def test_sandbox_runner_url_accepts_override() -> None:
    settings = Settings(sandbox_runner_url="http://sandbox-runner:9000")  # type: ignore[call-arg]
    assert settings.sandbox_runner_url == "http://sandbox-runner:9000"


def test_sandbox_runner_secret_defaults_to_empty_secretstr() -> None:
    """The shared secret is a ``SecretStr`` and defaults to empty (no secret ⇒ unconfigured)."""
    secret = Settings(_env_file=None).sandbox_runner_secret  # type: ignore[call-arg]
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == ""


def test_sandbox_runner_secret_accepts_override() -> None:
    settings = Settings(sandbox_runner_secret="sidecar-shared-secret")  # type: ignore[call-arg]
    assert isinstance(settings.sandbox_runner_secret, SecretStr)
    assert settings.sandbox_runner_secret.get_secret_value() == "sidecar-shared-secret"


def test_sandbox_runner_secret_is_not_leaked_in_repr() -> None:
    """A ``SecretStr`` must NOT echo its value in ``repr()``/``str()`` (so the shared secret never
    lands in a log line or a traceback that renders the ``Settings``); only ``.get_secret_value()``
    returns the plaintext."""
    value = "do-not-leak-9f3a2b"
    settings = Settings(sandbox_runner_secret=value)  # type: ignore[call-arg]
    assert value not in repr(settings.sandbox_runner_secret)
    assert value not in str(settings.sandbox_runner_secret)
    assert value not in repr(settings)
    # The plaintext is still retrievable via the explicit accessor (so routing can send it).
    assert settings.sandbox_runner_secret.get_secret_value() == value


# -- Durable LLM-egress audit flag (ADR 0105 / Gate F2, spec §2.5) ------------------------------ #
#
# Additive: default False (DORMANT — behaviour is byte-identical to L1 until an operator opts in
# after applying migration 0011 and confirming the Postgres sink); an explicit override is honoured.


def test_llm_egress_durable_enabled_defaults_to_false() -> None:
    """Dormant by construction (ADR 0105 / INV-DORMANT): no operator opt-in required to get
    L1's byte-identical behaviour."""
    assert Settings(_env_file=None).llm_egress_durable_enabled is False  # type: ignore[call-arg]


def test_llm_egress_durable_enabled_accepts_override() -> None:
    settings = Settings(llm_egress_durable_enabled=True)  # type: ignore[call-arg]
    assert settings.llm_egress_durable_enabled is True
