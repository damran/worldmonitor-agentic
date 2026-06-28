"""CliToolConnector — the ACTIVE CLI-tool connector base (ADR 0071 §5).

The base every ACTIVE CLI-tool connector subclasses. It runs a real binary via ``run_command``
(``asyncio.create_subprocess_exec`` — an argv **LIST**, never a shell string, so there is no
shell-interpolation surface) and yields the captured stdout as a :class:`RawRecord`.

``collect(config)``:

* validates the user-declared config (runtime-injected ``_``-prefixed keys — ``_scope`` /
  ``_cursor`` — are NOT part of the JSON schema, so they are stripped before validation);
* reads ``config["_scope"]`` and validates its ``target`` via the subclass hook
  :meth:`_validate_target` (raises ``ValueError`` BEFORE any subprocess runs — a hostile target
  never reaches the runner);
* builds the argv via the subclass hook :meth:`_build_argv` (always a ``list[str]``);
* runs it through the INJECTABLE runner seam (``run_command``-compatible async callable; tests
  inject a fake), bounded by a timeout, and yields ONE :class:`RawRecord` of the captured stdout.

The capability is :data:`Capability.ACTIVE` — the gate the cadence driver refuses; the authorized
operator-run path (``runner.operator_run``) is the only way these execute.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from worldmonitor.plugins.base import Connector, RawRecord
from worldmonitor.runner.subprocess import RunResult, run_command

# Default per-command wall-clock bound when a config omits ``timeout`` (run_command is bounded).
_DEFAULT_TIMEOUT = 30.0

# An injectable ``run_command``-compatible async callable: ``runner(argv, *, timeout) -> RunResult``
Runner = Callable[..., Coroutine[Any, Any, RunResult]]


def _is_str_list(value: object) -> bool:
    """True iff ``value`` is a ``list`` of ``str`` (the no-shell argv shape)."""
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) for item in cast("list[Any]", value))


class CliToolConnector(Connector):
    """ACTIVE base: validate target → build argv list → run (no shell) → yield stdout."""

    def __init__(self, *, runner: Runner | None = None) -> None:
        """Store the injectable runner seam; defaults to the real :func:`run_command`."""
        self._runner: Runner = runner or run_command

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Validate the scope target, build an argv LIST, run it, and yield the captured stdout.

        A generator: the target validation fires on first iteration, so a hostile target raises
        ``ValueError`` (and the runner is NEVER invoked). The argv is asserted to be a ``list[str]``
        before it reaches the runner — the no-shell invariant.
        """
        # Validate only the user-declared config; runtime-injected `_scope`/`_cursor` aren't schema.
        self.validate_config({k: v for k, v in config.items() if not k.startswith("_")})
        scope = cast("Mapping[str, Any]", config.get("_scope") or {})
        target = scope.get("target")
        self._validate_target(target)  # raises ValueError on a bad target, before any exec

        argv = self._build_argv(scope)
        if not _is_str_list(argv):
            raise ValueError("argv must be a list[str] (never a shell string)")

        timeout = float(config.get("timeout", _DEFAULT_TIMEOUT))
        result = asyncio.run(self._runner(argv, timeout=timeout))
        yield RawRecord(
            key=str(target),
            data=result.stdout,
            retrieved_at=datetime.now(UTC).isoformat(),
            content_type="text/plain",
        )

    @abstractmethod
    def _validate_target(self, target: Any) -> None:
        """Validate the scope target's shape; raise ``ValueError`` on anything hostile."""

    @abstractmethod
    def _build_argv(self, scope: Mapping[str, Any]) -> list[str]:
        """Build the argv LIST for the subprocess (never a shell string)."""
