"""Single-writer SoR-spine guard: a Postgres transaction-scoped advisory lock (ADR 0110).

ADR 0100 D1 gave the append-only statement/decision/context-claim log a server-assigned,
monotonic ``seq`` outbox column and a projector that folds incrementally on
``seq > watermark`` — a design that **assumed**, but did not enforce, a single writer, so
committed ``seq`` order matches assignment order. Under two concurrent spine writers a lower
``seq`` can commit *after* the watermark has advanced past it, and the incremental fold
silently — and permanently — skips it (ADR 0100 D1's named revisit trigger).

``acquire_spine_writer_lock`` turns that assumption into a fail-loud invariant
(**INV-SINGLE-WRITER**, ADR 0110 option (a)): at most one writer may hold the SoR-spine promote
transaction at a time. It takes ``pg_try_advisory_xact_lock`` — a **transaction-scoped** Postgres
advisory lock that auto-releases at ``COMMIT``/``ROLLBACK`` with no explicit unlock, no leak on
exception, and no cross-batch holding. A second concurrent writer that cannot acquire it is
refused (fail-closed) with :class:`ConcurrentSpineWriterError`, never allowed to interleave its
``seq``-assign/commit window with the holder's.

**Postgres-only.** On any other SQLAlchemy dialect (SQLite in the unit suite) this is a no-op:
those tests are single-connection with no concurrency hazard to guard, and
``pg_try_advisory_xact_lock`` does not exist outside Postgres.

Always enforced on Postgres — this is a data-integrity property (like provenance stamping, ADR
0109), not a person-affecting review guard, so it is intentionally **not** wired into
``enforcement_profile`` / ``Settings.is_enforced``.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from worldmonitor.settings import get_settings

logger = logging.getLogger(__name__)


class ConcurrentSpineWriterError(RuntimeError):
    """Raised when a second concurrent writer is refused the SoR-spine promote lock.

    ``INV-SINGLE-WRITER`` (ADR 0110): at most one writer may hold the transaction-scoped
    ``pg_try_advisory_xact_lock`` on the spine at a time. This is the fail-closed refusal —
    never allow a second writer to interleave its ``seq``-assign/commit window with the holder's.
    """


def acquire_spine_writer_lock(session: Session, *, key: int | None = None) -> None:
    """Take the transaction-scoped SoR-spine advisory lock, or raise ``ConcurrentSpineWriterError``.

    Postgres-only (ADR 0110): on any non-Postgres dialect (SQLite in the unit suite) this is a
    no-op — those tests are single-connection with no concurrency hazard to guard. If the bind's
    dialect cannot be resolved, this is treated defensively as non-Postgres (no-op); a genuine
    Postgres error from the lock call itself is never swallowed.

    On Postgres, executes ``SELECT pg_try_advisory_xact_lock(:key)`` with ``key`` defaulting to
    ``settings.spine_writer_lock_key``. A ``True`` result means the lock is now held for the rest
    of the current transaction — it auto-releases at ``COMMIT``/``ROLLBACK`` (no explicit unlock).
    A ``False`` result means another session already holds it: raise
    :class:`ConcurrentSpineWriterError` (fail-closed) rather than let a second writer proceed.
    """
    try:
        bind = session.get_bind()
        dialect_name = bind.dialect.name
    except Exception:
        # Defensive: an unresolvable bind is treated as non-Postgres (no-op), not swallowed as a
        # Postgres lock failure.
        return
    if dialect_name != "postgresql":
        return

    lock_key = key if key is not None else get_settings().spine_writer_lock_key
    acquired = session.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key}
    ).scalar()
    if not acquired:
        raise ConcurrentSpineWriterError(
            "INV-SINGLE-WRITER: a second concurrent SoR-spine writer was refused "
            f"(pg_try_advisory_xact_lock key={lock_key} already held) — fail-closed per ADR 0110"
        )
