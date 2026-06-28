"""The operator-run path — the ONLY way an ACTIVE connector executes (ADR 0071 §3).

``run_connector_once`` is callable from the authed REST endpoint (and later the UI), DISTINCT from
the cadence ``IngestDriver._ingest_instance`` (which is UNCHANGED — it still refuses every ACTIVE
connector). It is reachable ONLY from an authenticated operator — never the cadence, never an
agent/MCP tool — so "never agent-auto-run" holds.

For an **ACTIVE** connector a ``scope`` is REQUIRED (else it refuses with ``ValueError``, which the
REST route maps to 422); it mints a tamper-evident scope token, verifies it (defense in depth),
injects ``config["_scope"] = scope`` and records ``run_mode="operator"`` / ``triggered_by`` /
``scope_token`` on the ``task_run``. A **PASSIVE** connector may be run-now without a token. The run
goes through the existing ``run_ingest`` (landing + ER queue, unchanged) — read-only, never the
graph. An ACTIVE run also emits a distinct, higher-visibility audit log line carrying
connector/instance/operator ONLY — never the token plaintext, never a config secret.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.models import ConnectorInstance, TaskRun
from worldmonitor.plugins import scope_token
from worldmonitor.plugins.base import Capability, Connector
from worldmonitor.runner.ingest import run_ingest
from worldmonitor.settings import Settings
from worldmonitor.storage.landing import LandingStore

logger = logging.getLogger(__name__)

# A distinct, higher-visibility audit logger for ACTIVE operator runs (ADR 0071 §2, separate
# logging). It carries connector/instance/operator ONLY — never the scope-token plaintext, never a
# config secret (those live solely in the encrypted config / the ``task_run.scope_token`` column).
active_logger = logging.getLogger("worldmonitor.active")

_ERROR_SUMMARY_MAX = 2000


class SandboxUnavailableError(RuntimeError):
    """An ACTIVE connector requires a sandbox that is not enabled (ADR 0072 §1).

    Raised by :func:`run_connector_once` BEFORE any runner/landing when a connector declares
    ``sandbox == "container"`` and ``settings.container_sandbox_enabled`` is False — the heavy-tool
    gate (nmap is refused un-sandboxed in v1). It is deliberately a ``RuntimeError`` (NOT a
    ``ValueError``): the REST route maps it to **409** (a refused capability), distinct from the
    ``ValueError`` -> **422** an invalid scope / out-of-allowlist target gets.
    """


def run_connector_once(
    instance: ConnectorInstance,
    connector: Connector,
    *,
    scope: dict[str, Any] | None,
    operator: str,
    sessions: sessionmaker[Session],
    landing: LandingStore,
    settings: Settings,
) -> str:
    """Run ``connector`` once for ``operator`` and return the audit ``task_run`` id.

    ACTIVE → refuses without a ``scope`` (``ValueError``), else mints + verifies a scope token and
    injects ``config["_scope"]``. PASSIVE → run-now, no token. Always records the operator audit
    fields on the ``task_run``. Does NOT call ``validate_config`` (the injected ``_scope`` key is
    not part of the schema); ``run_ingest`` drives the connector's ``collect``/``map``.
    """
    manifest = connector.manifest
    is_active = manifest.capability is Capability.ACTIVE

    config: dict[str, Any] = json.loads(
        ConfigCipher.from_settings(settings).decrypt(instance.config_encrypted)
    )

    token: str | None = None
    if is_active:
        if not scope:
            # Refuse BEFORE persisting any audit row or running anything (never agent-auto-run).
            raise ValueError(
                f"connector '{manifest.connector_id}' is ACTIVE-capability; an operator run "
                "requires an authorized scope — refused"
            )
        token = scope_token.mint(
            manifest.connector_id, instance.id, scope, operator, settings=settings
        )
        # Defense in depth: the freshly minted token must verify for THIS connector + instance.
        scope_token.verify(
            token,
            expected_connector_id=manifest.connector_id,
            expected_instance_id=instance.id,
            settings=settings,
        )
        config = {**config, "_scope": dict(scope)}

    # Heavy-tool sandbox gate (ADR 0072 §1): a connector that requires a container sandbox is
    # REFUSED until one is enabled — raised BEFORE any runner/landing/audit row, so a heavy tool
    # (nmap) can never run un-sandboxed in v1. The REST route maps this to 409 (refused capability).
    if (
        getattr(connector, "sandbox", "subprocess") == "container"
        and not settings.container_sandbox_enabled
    ):
        raise SandboxUnavailableError(
            f"connector '{manifest.connector_id}' requires a container sandbox which is not enabled"
        )

    task_id = str(uuid.uuid4())
    with sessions() as session:
        session.add(
            TaskRun(
                id=task_id,
                connector_instance_id=instance.id,
                kind="ingest",
                status="running",
                run_mode="operator",
                triggered_by=operator,
                scope_token=token,
            )
        )
        session.commit()

    # The distinct ACTIVE-run marker (connector/instance/operator ONLY — no token, no secret).
    if is_active:
        active_logger.warning(
            "ACTIVE operator run authorized: connector=%s instance=%s operator=%s task=%s",
            manifest.connector_id,
            instance.id,
            operator,
            task_id,
        )

    status, error, stats = "ok", "", None
    # A connector pre-flight refusal (the SHARED target validator / the enforced allowlist, ADR 0072
    # §2/§3) raises ``ValueError`` from ``collect`` BEFORE any landing — record it as a failed run
    # AND re-raise so the REST route maps it to 422 (a refused scope/target, distinct from a generic
    # run error which stays a recorded ``error`` status). The success/PASSIVE flow is unchanged.
    refusal: ValueError | None = None
    try:
        with sessions() as work:
            result = run_ingest(connector, config, landing=landing, session=work)
        stats = asdict(result)
    except ValueError as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"[:_ERROR_SUMMARY_MAX]
        refusal = exc
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"[:_ERROR_SUMMARY_MAX]
        logger.warning("operator run failed [%s]: %s", manifest.connector_id, type(exc).__name__)

    with sessions() as session:
        task = session.get(TaskRun, task_id)
        if task is not None:
            task.status = status
            task.error = error
            task.stats = stats
            task.finished_at = datetime.now(UTC)
        session.commit()
    if refusal is not None:
        raise refusal
    return task_id
