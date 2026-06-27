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
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.runner.heartbeat import Heartbeat
from worldmonitor.settings import Settings, get_settings
from worldmonitor.storage.landing import LandingStore

Probe = Callable[[], None]
# The driver probe is NON-FATAL: it RETURNS a freshness string ("ok"/"stale"/"unknown")
# rather than raising. If it does raise, the caller degrades it to "unknown" (ADR 0059).
DriverProbe = Callable[[], str]

# Store component order is stable so the body reads the same every call. The driver field is
# appended after these — it is observability only and is EXCLUDED from the fatal ready gate.
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
    driver_probe: DriverProbe,
) -> ReadinessResult:
    """Run the three STORE probes; ready IFF all three succeed (fail-closed).

    A store probe that raises (for ANY reason) marks its component ``"down"`` — never a 500.
    The reachable components stay ``"ok"`` so the body always names exactly what failed.

    The ``driver_probe`` is **non-fatal** (ADR 0059): it RETURNS a freshness string
    (``"ok"``/``"stale"``/``"unknown"``) recorded under ``checks["driver"]``; if it raises it
    degrades to ``"unknown"``. It is pure observability — it NEVER flips ``ready`` or the HTTP
    status. ``ready`` is computed over the THREE STORES ONLY, before the driver field is added.
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
    # The ready gate is STORES-ONLY — fix it BEFORE the non-fatal driver field is appended.
    ready = all(checks[component] == "ok" for component in _COMPONENTS)
    try:
        checks["driver"] = driver_probe()
    except Exception:  # noqa: BLE001 - driver is non-fatal: any failure degrades to "unknown"
        checks["driver"] = "unknown"
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

    def driver_probe() -> str:
        # NON-FATAL driver-heartbeat freshness (ADR 0059): read B's existing FILE heartbeat
        # (runner.heartbeat.Heartbeat) — no new table/migration. Fresh -> "ok"; missing/stale/
        # unparseable -> "stale" (is_alive fails closed); path unset/empty or an unexpected read
        # error -> "unknown". This NEVER raises into the fatal path and NEVER flips ``ready``.
        path = settings.driver_heartbeat_path
        if not path:
            return "unknown"
        heartbeat = Heartbeat(Path(path), settings.driver_heartbeat_stale_seconds)
        status: dict[str, bool] = {}

        def read() -> None:
            status["alive"] = heartbeat.is_alive(datetime.now(UTC))

        try:
            # Bound the read like the store probes so a wedged filesystem can't hang /ready.
            _bounded(read, timeout)()
        except Exception:  # noqa: BLE001 - any failure (timeout/unexpected) degrades to "unknown"
            return "unknown"
        return "ok" if status["alive"] else "stale"

    return lambda: check_readiness(
        postgres_probe=_bounded(postgres_probe, timeout),
        neo4j_probe=_bounded(neo4j_probe, timeout),
        minio_probe=_bounded(minio_probe, timeout),
        driver_probe=driver_probe,
    )
