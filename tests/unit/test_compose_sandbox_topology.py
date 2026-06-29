"""Primary invariant tests (RED) for the sandbox-runner DEPLOY topology — ADR 0077 Slice 2.

These parse ``deploy/compose.yaml`` (and, best-effort, the ``Dockerfile``) as text/YAML — NO image
build, NO Docker, NO network — so they run anywhere the unit suite runs (the WSL box can't build
images; CI ``compose-boot`` is the live proof). They pin the egress-isolation + deploy-hardening
invariants from ``.claude/gate.scope``:

* INV-2 (egress network isolation): a ``sandbox-runner`` service exists (``build.target ==
  "sandbox-runner"``), is attached to ``sandbox_net`` and NOT to ``default`` (so by topology it
  cannot reach the stores), and publishes NO host ``ports:``. The store services
  (postgres/neo4j/minio/redis/zitadel) are NOT on ``sandbox_net``. ``api`` + ``driver`` join BOTH
  ``default`` and ``sandbox_net`` (so they can POST to the sidecar yet keep store reachability), and
  define ``SANDBOX_RUNNER_URL`` + ``SANDBOX_RUNNER_SECRET``. A top-level ``sandbox_net`` is defined.
* INV-4 (resource bounds + non-root + read-only): the sandbox-runner service carries a ``mem_limit``
  AND a ``pids_limit``, is ``read_only: true``, and has a ``healthcheck``.
* INV-3 (slim app image): the nmap/dnsutils/whois binaries are apt-installed ONLY in a dedicated
  ``... AS sandbox-runner`` Dockerfile stage — never in the base ``runtime`` stage (kept lenient,
  string checks, so a reasonable Dockerfile passes).

RED today: ``deploy/compose.yaml`` declares no ``networks:`` and no ``sandbox-runner`` service, and
the ``Dockerfile`` has no ``AS sandbox-runner`` stage — so the existence/topology/stage assertions
fail (the correct RED). The store-isolation guards are vacuously green now and stay meaningful once
the sidecar lands.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "deploy" / "compose.yaml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"

# The compose names the stores live under; the sidecar must NOT share their network (INV-2).
_STORE_SERVICES = ["postgres", "neo4j", "minio", "redis", "zitadel"]
# The app services that delegate to the sidecar: they need BOTH networks + the wiring env (INV-5).
_APP_SERVICES = ["api", "driver"]


def _load_compose() -> dict[str, Any]:
    """Parse ``deploy/compose.yaml`` (YAML → dict). Read once per test (mirrors the house style in
    ``test_production_secret_hygiene.py``)."""
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _service(compose: dict[str, Any], name: str) -> dict[str, Any]:
    """The service block ``name`` — asserts presence with a readable message (vs a raw KeyError)."""
    services = compose.get("services") or {}
    assert name in services, f"deploy/compose.yaml must define a `{name}` service"
    return services[name]


def _service_networks(service: dict[str, Any]) -> set[str]:
    """The set of network NAMES a service is attached to — handles both the list form
    (``networks: [a, b]``) and the mapping form (``networks: {a: {}, b: {}}``); ``None`` ⇒ empty."""
    nets = service.get("networks")
    if isinstance(nets, list):
        return {str(n) for n in nets}
    if isinstance(nets, dict):
        return {str(n) for n in nets}
    return set()


def _env_keys(service: dict[str, Any]) -> set[str]:
    """The set of environment-variable NAMES a service defines — handles both the mapping form
    (``environment: {K: v}``) and the list form (``environment: ["K=v"]``)."""
    env = service.get("environment")
    if isinstance(env, dict):
        return {str(k) for k in env}
    if isinstance(env, list):
        return {str(item).split("=", 1)[0] for item in env if isinstance(item, str)}
    return set()


# --------------------------------------------------------------------------- #
# INV-2 — the sandbox-runner service exists, isolated, with no host ports.
# --------------------------------------------------------------------------- #


def test_sandbox_runner_service_exists_with_build_target() -> None:
    """The ``sandbox-runner`` service exists, built from its dedicated stage (INV-2/INV-3)."""
    svc = _service(_load_compose(), "sandbox-runner")
    build = svc.get("build")
    assert isinstance(build, dict), (
        "sandbox-runner must use a `build:` mapping selecting the dedicated stage (build.target)"
    )
    assert build.get("target") == "sandbox-runner", (
        f"sandbox-runner.build.target must be 'sandbox-runner', got {build.get('target')!r}"
    )


def test_sandbox_runner_isolated_from_the_stores_network() -> None:
    """INV-2: the sidecar is on ``sandbox_net`` and NOT on ``default`` — by topology it cannot reach
    the stores (the data-sovereignty / constrained-egress goal)."""
    nets = _service_networks(_service(_load_compose(), "sandbox-runner"))
    assert "sandbox_net" in nets, f"sandbox-runner must be on `sandbox_net`, got {sorted(nets)}"
    assert "default" not in nets, (
        f"egress isolation: sandbox-runner must NOT be on the stores' `default` network, "
        f"got {sorted(nets)}"
    )


def test_sandbox_runner_publishes_no_host_port() -> None:
    """INV-2: no ``ports:`` — the sidecar is reachable IN-NETWORK only (no host publish)."""
    svc = _service(_load_compose(), "sandbox-runner")
    assert "ports" not in svc, (
        "sandbox-runner must NOT publish a host port (in-network only); remove the `ports:` key"
    )


def test_store_services_not_on_sandbox_net() -> None:
    """INV-2: every store service that IS present is OFF ``sandbox_net`` (the sidecar must not be
    able to reach it). Iterates the present stores (no skip — an absent store is just not asserted,
    and at least one store must exist so the check is never vacuous)."""
    services = _load_compose().get("services") or {}
    present = [s for s in _STORE_SERVICES if s in services]
    assert present, f"expected at least one store service from {_STORE_SERVICES} in compose"
    on_sandbox = {
        s: sorted(_service_networks(services[s]))
        for s in present
        if "sandbox_net" in _service_networks(services[s])
    }
    assert not on_sandbox, (
        f"these store services must NOT be on `sandbox_net` (egress isolation): {on_sandbox}"
    )


def test_top_level_sandbox_net_declared() -> None:
    """INV-2: the top-level ``networks:`` block declares ``sandbox_net``."""
    networks = _load_compose().get("networks") or {}
    assert "sandbox_net" in networks, (
        f"top-level `networks:` must declare `sandbox_net`, got {sorted(networks)}"
    )


# --------------------------------------------------------------------------- #
# INV-4 — resource bounds + read-only + healthcheck on the sidecar.
# --------------------------------------------------------------------------- #


def test_sandbox_runner_has_resource_bounds_and_health() -> None:
    """INV-4: the sidecar carries a ``mem_limit`` AND a ``pids_limit``, is ``read_only: true``, and
    has a ``healthcheck`` (so a runaway scan is bounded and the stack can probe its liveness)."""
    svc = _service(_load_compose(), "sandbox-runner")
    assert "mem_limit" in svc, "sandbox-runner must set a `mem_limit` (resource bound)"
    assert "pids_limit" in svc, "sandbox-runner must set a `pids_limit` (resource bound)"
    assert svc.get("read_only") is True, (
        "sandbox-runner must run `read_only: true` (immutable rootfs)"
    )
    assert "healthcheck" in svc, "sandbox-runner must define a `healthcheck`"


# --------------------------------------------------------------------------- #
# INV-5 — api + driver are on BOTH networks and carry the sidecar wiring.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", _APP_SERVICES)
def test_app_service_on_both_networks_and_wired(name: str) -> None:
    """INV-5: ``api`` and ``driver`` each join BOTH ``default`` (store reachability) and
    ``sandbox_net`` (to POST to the sidecar), and define ``SANDBOX_RUNNER_URL`` +
    ``SANDBOX_RUNNER_SECRET`` in ``environment``."""
    svc = _service(_load_compose(), name)
    nets = _service_networks(svc)
    assert {"default", "sandbox_net"} <= nets, (
        f"{name} must be on BOTH `default` and `sandbox_net`, got {sorted(nets)}"
    )
    env = _env_keys(svc)
    assert "SANDBOX_RUNNER_URL" in env, f"{name} must define SANDBOX_RUNNER_URL"
    assert "SANDBOX_RUNNER_SECRET" in env, f"{name} must define SANDBOX_RUNNER_SECRET"


# --------------------------------------------------------------------------- #
# INV-3 — the tool binaries live ONLY in the sandbox-runner Dockerfile stage.
# --------------------------------------------------------------------------- #


def test_dockerfile_tools_only_in_sandbox_runner_stage() -> None:
    """INV-3 (best-effort, lenient string checks): the Dockerfile declares a ``AS sandbox-runner``
    stage, and the apt-install of nmap/dnsutils/whois appears ONLY AFTER that stage line — never in
    the base ``runtime`` stage (so the api/driver image stays slim/apt-free)."""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()

    stage_idx = next(
        (i for i, ln in enumerate(lines) if re.search(r"\bAS\s+sandbox-runner\b", ln)), None
    )
    assert stage_idx is not None, (
        "Dockerfile must declare a dedicated `FROM runtime AS sandbox-runner` build stage (INV-3)"
    )

    tool_re = re.compile(r"\b(nmap|dnsutils|whois)\b")
    tool_line_idxs = [i for i, ln in enumerate(lines) if tool_re.search(ln)]
    assert tool_line_idxs, (
        "expected an apt-install of nmap/dnsutils/whois somewhere in the sandbox-runner stage"
    )
    assert min(tool_line_idxs) > stage_idx, (
        "nmap/dnsutils/whois must be installed ONLY in the `sandbox-runner` stage (after its "
        "`AS sandbox-runner` line), not in the base runtime stage — keep the app image slim (INV-3)"
    )
