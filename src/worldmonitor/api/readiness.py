"""``/ready`` — a REAL, fail-closed store-reachability probe (Gate B-4c, ADR 0051).

Unlike ``/health`` (a cheap, dependency-free liveness echo — deliberately UNCHANGED, see
``api/main.py``), ``/ready`` actually reaches each backing store read-only and reports
**not-ready** the moment one is unreachable. This is the audit's live-vs-dead distinction
(spec §2): the five stores can be down (or the driver dead) while ``/health`` still echoes
ok — ``/ready`` is the surface that fails closed.

Design (spec §3.2):

* :func:`check_readiness` takes three **injected** zero-arg probes (so the decision logic is
  unit-testable with fakes and needs no live stack). Each probe RAISES on failure; a raising
  probe records that component ``"down"`` (never a 500).
* :func:`build_default_readiness` wires the REAL probes from settings/clients for production:
  Postgres ``SELECT 1``, ``Neo4jClient.verify()``, and a read-only ``head_bucket`` on the
  landing bucket via the existing landing client (never a write, never ``ensure_bucket`` — so
  ``storage/landing.py`` is untouched). Each probe is bounded by
  ``readiness_probe_timeout_seconds`` so a hung store can't hang ``/ready`` forever.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text

from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.settings import Settings, get_settings
from worldmonitor.storage.landing import LandingStore

Probe = Callable[[], None]

# Component order is stable so the body reads the same every call.
_COMPONENTS = ("postgres", "neo4j", "minio")


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    """The outcome of a readiness sweep: overall ``ready`` + a per-component map."""

    ready: bool
    checks: dict[str, str]


def check_readiness(
    *,
    postgres_probe: Probe,
    neo4j_probe: Probe,
    minio_probe: Probe,
) -> ReadinessResult:
    """Run each injected probe; ready IFF all three succeed (fail-closed).

    A probe that raises (for ANY reason) marks its component ``"down"`` — never a 500.
    The reachable components stay ``"ok"`` so the body always names exactly what failed.
    """
    probes: dict[str, Probe] = {
        "postgres": postgres_probe,
        "neo4j": neo4j_probe,
        "minio": minio_probe,
    }
    checks: dict[str, str] = {}
    for component in _COMPONENTS:
        try:
            probes[component]()
            checks[component] = "ok"
        except Exception:  # noqa: BLE001 - any failure means the store is unreachable -> "down"
            checks[component] = "down"
    ready = all(state == "ok" for state in checks.values())
    return ReadinessResult(ready=ready, checks=checks)


def _bounded(probe: Probe, timeout_seconds: float) -> Probe:
    """Wrap ``probe`` so it cannot block longer than ``timeout_seconds``.

    A hung store (TCP black-hole, frozen server) must not hang ``/ready`` forever. The probe
    runs in a daemon thread; if it doesn't finish in time we raise (recorded as ``"down"``).
    A leaked daemon thread is acceptable for a bounded health probe.
    """

    def bounded() -> None:
        outcome: list[BaseException | None] = [None]
        done = threading.Event()

        def runner() -> None:
            try:
                probe()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread below
                outcome[0] = exc
            finally:
                done.set()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        if not done.wait(timeout_seconds):
            raise TimeoutError(f"readiness probe exceeded {timeout_seconds}s")
        error = outcome[0]
        if error is not None:
            raise error

    return bounded


def build_default_readiness(
    settings: Settings | None = None,
) -> Callable[[], ReadinessResult]:
    """Build the production readiness callable from settings + the real store clients.

    Returns a zero-arg callable (matching the ``readiness=`` injection contract of
    :func:`worldmonitor.api.main.create_app`). The clients are constructed once here (driver,
    engine and boto3 client all connect lazily — building them does not touch the network);
    each probe opens a short-lived, read-only check on call and is timeout-bounded.
    """
    settings = settings or get_settings()
    timeout = settings.readiness_probe_timeout_seconds
    sessions = session_factory(engine_from_settings(settings))
    neo4j = Neo4jClient.from_settings(settings)
    landing = LandingStore.from_settings(settings)

    def postgres_probe() -> None:
        with sessions() as session:
            session.execute(text("SELECT 1"))

    def neo4j_probe() -> None:
        neo4j.verify()

    def minio_probe() -> None:
        # Read-only: head_bucket on the landing bucket via the existing client. Never a write,
        # never ensure_bucket (storage/landing.py stays untouched).
        landing.client.head_bucket(Bucket=landing.bucket)

    return lambda: check_readiness(
        postgres_probe=_bounded(postgres_probe, timeout),
        neo4j_probe=_bounded(neo4j_probe, timeout),
        minio_probe=_bounded(minio_probe, timeout),
    )
