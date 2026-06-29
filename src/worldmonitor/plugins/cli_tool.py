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
import re
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from worldmonitor.plugins.base import Connector, RawRecord
from worldmonitor.runner.subprocess import RunResult, run_command

# Default per-command wall-clock bound when a config omits ``timeout`` (run_command is bounded).
_DEFAULT_TIMEOUT = 30.0

# The SHARED, hardened target shape (ADR 0072 §3): a plain domain / IP — alphanumerics plus
# dot/colon/hyphen ONLY (no whitespace, no shell metachar, no path separator). The validator below
# layers the additional rules (no leading ``-``, length <= 253, no ``..`` substring) on top.
_TARGET_RE = re.compile(r"[A-Za-z0-9.:-]+")

# DNS names cap at 253 chars; a longer "target" is hostile padding, not a real host (ADR 0072 §3).
_MAX_TARGET_LEN = 253

# An injectable ``run_command``-compatible async callable: ``runner(argv, *, timeout) -> RunResult``
Runner = Callable[..., Coroutine[Any, Any, RunResult]]


def _is_str_list(value: object) -> bool:
    """True iff ``value`` is a ``list`` of ``str`` (the no-shell argv shape)."""
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) for item in cast("list[Any]", value))


def _target_allowed(target: str, allowed: list[str]) -> bool:
    """Return True iff ``target`` is permitted by the ``allowed`` list (ADR 0082).

    Semantics:

    * Empty ``allowed`` → True (any valid target; the per-run scope token is the primary auth).
    * A non-``*.`` entry: case-insensitive **exact** match only.  No implicit sub-domain expansion.
    * A ``*.<domain>`` entry: ``target`` must end with ``"." + domain``, i.e. it is a **strict
      subdomain** of ``<domain>``.  The dot-boundary is the load-bearing security invariant:

      - The apex ``<domain>`` itself does **NOT** match (``"example.com".endswith(".example.com")``
        is False).
      - A sibling without a dot boundary (``"evil-example.com"``, ``"xexample.com"``) does NOT
        match.
      - A suffix-spoof (``"<domain>.attacker.com"``) does NOT match (the string ends with
        ``.attacker.com``, not with ``.<domain>``).

    * A malformed wildcard — ``*.`` with an empty domain, or a domain that still contains ``*`` —
      is silently skipped: it matches nothing and can never become a catch-all bypass.

    Both sides are lowercased before comparison (DNS is case-insensitive).
    """
    if not allowed:
        return True

    t = target.lower()
    for entry in allowed:
        e = entry.lower()
        if e.startswith("*."):
            domain = e[2:]  # strip the "*."; what remains must be the parent domain
            # Malformed: empty domain or nested wildcard → skip (match nothing, not a catch-all)
            if not domain or "*" in domain:
                continue
            # Strict-subdomain check anchored at a dot boundary.
            # "a.example.com".endswith(".example.com")  → True  ✓
            # "example.com".endswith(".example.com")    → False (apex excluded) ✓
            # "evil-example.com".endswith(".example.com") → False (no dot) ✓
            if t.endswith("." + domain):
                return True
        else:
            # Non-wildcard: exact case-insensitive match
            if t == e:
                return True

    return False


class CliToolConnector(Connector):
    """ACTIVE base: validate target → enforce allowlist → build argv list → run → yield stdout."""

    # Sandbox level the operator-run gate keys on (ADR 0072 §1). ``"subprocess"`` (the default —
    # read-only / light tools like whois/dig run via the subprocess seam) vs ``"container"`` (a
    # heavy tool — e.g. nmap — whose EXECUTION ``run_connector_once`` refuses until a container
    # sandbox is enabled). A class attribute so a subclass declares it once; never in the argv.
    sandbox: str = "subprocess"

    def __init__(self, *, runner: Runner | None = None) -> None:
        """Store the injectable runner seam; defaults to the real :func:`run_command`."""
        self._runner: Runner = runner or run_command

    def use_runner(self, runner: Runner) -> None:
        """Replace the execution runner (the public seam ``operator_run`` uses to route a
        container-level tool through the sandbox-runner sidecar, ADR 0077 §D3). Keeps the swap an
        explicit, typed operation rather than reaching into the private attribute."""
        self._runner = runner

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Validate the scope target, enforce the allowlist, build an argv LIST, run it, and yield
        the captured stdout.

        A generator: the target validation + allowlist check fire on first iteration, so a hostile
        or out-of-list target raises ``ValueError`` (and the runner is NEVER invoked). The argv is
        asserted to be a ``list[str]`` before it reaches the runner — the no-shell invariant.
        """
        # Validate only the user-declared config; runtime-injected `_scope`/`_cursor` aren't schema.
        self.validate_config({k: v for k, v in config.items() if not k.startswith("_")})
        scope = cast("Mapping[str, Any]", config.get("_scope") or {})
        target = scope.get("target")
        self._validate_target(target)  # raises ValueError on a bad target, before any exec

        # Enforced instance allowlist (ADR 0072 §2, extended by ADR 0082): a non-empty
        # ``allowed_targets`` pre-restricts an instance to a fixed target set.  An absent or empty
        # list means "any valid target" (the per-run scope token remains the primary authorization).
        # Entries of the form ``*.<domain>`` match strict subdomains only (dot-boundary anchored —
        # see ``_target_allowed``).  Non-``*.`` entries keep EXACT-MATCH semantics.
        allowed = config.get("allowed_targets")
        if isinstance(allowed, list) and not _target_allowed(
            cast("str", target), cast("list[str]", allowed)
        ):
            raise ValueError(
                f"target {target!r} is not in the configured allowed_targets — refused"
            )

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

    def _validate_target(self, target: Any) -> None:
        """The SHARED, hardened target validator (ADR 0072 §3) — the default for every CLI tool.

        Refuse anything that is not a plain domain / IP, closing the bare-``..`` and over-length
        gaps the 6a checker flagged. Accept ONLY a non-empty ``str`` that matches
        ``^[A-Za-z0-9.:-]+$``, does not start with ``-`` (a flag), is ``<= 253`` chars, and carries
        no ``..`` substring. Subclasses inherit this (whois/dig); they MAY override but default to
        it. Raise ``ValueError`` on anything hostile, BEFORE the target can become an argv element.
        """
        if not isinstance(target, str) or not target:
            raise ValueError(f"target must be a non-empty string: {target!r}")
        if target.startswith("-"):
            raise ValueError(f"target may not start with '-' (flag injection): {target!r}")
        if len(target) > _MAX_TARGET_LEN:
            raise ValueError(
                f"target exceeds the {_MAX_TARGET_LEN}-char limit ({len(target)} chars)"
            )
        if ".." in target:
            raise ValueError(f"target may not contain '..' (traversal): {target!r}")
        if _TARGET_RE.fullmatch(target) is None:
            raise ValueError(f"target has an illegal character: {target!r}")

    @abstractmethod
    def _build_argv(self, scope: Mapping[str, Any]) -> list[str]:
        """Build the argv LIST for the subprocess (never a shell string)."""
