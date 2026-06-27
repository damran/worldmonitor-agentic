"""H-7 — GeoNames local-``path`` LFI / confinement negative tests (Gate H-6/H-7, ADR 0052).

The ``path`` config override on ``GeoNamesConnector.collect()`` is an arbitrary file read today:
``Path(str(local_path)).read_text("utf-8")`` runs verbatim with no base-dir confinement and no
size cap, so ``path: "/etc/passwd"`` / ``"/proc/self/environ"`` / ``"../../.env"`` reads that
file straight into the GDPR-readable landing zone (ADR 0052 §H-7).

These tests pin the FAIL-CLOSED contract the builder must satisfy (ADR 0052 D2):
  * default-deny: ``path`` with no ``geonames_allowed_path_dir`` configured is REJECTED;
  * a ``path`` that *resolves* outside the allowlist — absolute escape, ``..`` traversal, AND a
    symlink pointing outside (proving realpath resolution, not string-prefix) — is REJECTED;
  * an over-``geonames_max_path_bytes`` file is REJECTED;
  * the rejection raises ``GeoNamesPathError`` (a ``ValueError`` subclass) and yields NO records;
  * a genuinely IN-allowlist file (the real ``VA.txt`` fixture) still WORKS (the positive control
    proving the fix did not just disable ``path``).

RED today: confinement does not exist — ``collect()`` reads the out-of-tree file and yields
records, so ``pytest.raises`` fails; ``GeoNamesPathError`` does not exist yet either.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from worldmonitor.plugins.connectors.geonames.connector import GeoNamesConnector
from worldmonitor.settings import get_settings

# The builder defines ``GeoNamesPathError(ValueError)`` (ADR 0052 D2). Until it lands we fall
# back to ``Exception`` so the negative tests still RUN and demonstrate the vuln directly
# (records yielded / no raise) rather than erroring at import — RED for the *right* reason.
# ``test_path_error_type`` pins that the named subclass exists once the builder lands.
try:  # pragma: no cover - import shim, exercised both pre- and post-build
    from worldmonitor.plugins.connectors.geonames.connector import GeoNamesPathError
except ImportError:  # pragma: no cover
    GeoNamesPathError = None  # type: ignore[assignment, misc]

_EXPECTED_ERR: type[BaseException] = (
    GeoNamesPathError if GeoNamesPathError is not None else Exception
)

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "geonames"
_VA_FIXTURE = _FIXTURES_DIR / "VA.txt"


@pytest.fixture(autouse=True)
def _isolate_settings_cache() -> pytest.FixtureRequest:  # type: ignore[misc]
    """Clear the cached settings around every test so env wiring is read fresh and never leaks."""
    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


@pytest.fixture
def configure(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Set / clear the H-7 settings via env (like the rest of the suite) and refresh the cache."""

    def _apply(
        *, allowed_dir: str | os.PathLike[str] | None = None, max_bytes: int | None = None
    ) -> None:
        if allowed_dir is None:
            monkeypatch.delenv("GEONAMES_ALLOWED_PATH_DIR", raising=False)
        else:
            monkeypatch.setenv("GEONAMES_ALLOWED_PATH_DIR", str(allowed_dir))
        if max_bytes is None:
            monkeypatch.delenv("GEONAMES_MAX_PATH_BYTES", raising=False)
        else:
            monkeypatch.setenv("GEONAMES_MAX_PATH_BYTES", str(max_bytes))
        get_settings.cache_clear()

    return _apply


def _assert_rejected_yielding_nothing(connector: GeoNamesConnector, config: dict[str, str]) -> None:
    """collect(config) must RAISE the confinement error and yield NOT ONE record before raising."""
    collected: list[object] = []
    with pytest.raises(_EXPECTED_ERR):
        for record in connector.collect(config):
            collected.append(record)
    assert collected == [], "confinement must fail closed BEFORE yielding any record"


# --------------------------------------------------------------------------------------------------
# The four bypass classes (abs / .. / symlink / default-deny) + the size cap — each REJECTED.
# --------------------------------------------------------------------------------------------------


def test_absolute_path_outside_allowlist_is_rejected(
    tmp_path: Path, configure: Callable[..., None]
) -> None:
    """An absolute ``path`` outside the allowlist (a fake /etc/passwd) raises + yields nothing."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "passwd"  # outside the allowlist tree
    secret.write_text("root:x:0:0:root:/root:/bin/bash\nbackup-key:SUPERSECRET\n", encoding="utf-8")
    configure(allowed_dir=allowed)

    _assert_rejected_yielding_nothing(GeoNamesConnector(), {"country": "VA", "path": str(secret)})


def test_real_etc_passwd_is_rejected(tmp_path: Path, configure: Callable[..., None]) -> None:
    """The canonical exfil target ``/etc/passwd`` (a real out-of-tree file) is rejected."""
    if not Path("/etc/passwd").exists():  # pragma: no cover - non-POSIX hosts
        pytest.skip("/etc/passwd not present on this platform")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    configure(allowed_dir=allowed)

    _assert_rejected_yielding_nothing(GeoNamesConnector(), {"country": "VA", "path": "/etc/passwd"})


def test_dotdot_traversal_escaping_allowlist_is_rejected(
    tmp_path: Path, configure: Callable[..., None]
) -> None:
    """A ``..`` traversal that resolves outside the allowlist raises + yields nothing."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret.txt"  # one level above the allowlist
    secret.write_text("1\tleaked\tx\t\t1\t2\t\t\tVA\n", encoding="utf-8")
    configure(allowed_dir=allowed)

    escape = str(allowed / ".." / "secret.txt")
    _assert_rejected_yielding_nothing(GeoNamesConnector(), {"country": "VA", "path": escape})


def test_symlink_inside_allowlist_pointing_outside_is_rejected(
    tmp_path: Path, configure: Callable[..., None]
) -> None:
    """A symlink that LIVES in the allowlist but TARGETS outside it is rejected.

    This proves realpath (``Path.resolve``) resolution defeats the attack — a naive string-prefix
    check on the symlink's own path would wrongly accept it.
    """
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("1\tleaked-via-symlink\tx\t\t1\t2\t\t\tVA\n", encoding="utf-8")
    link = allowed / "innocent.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - symlink-less platform
        pytest.skip("platform does not support symlinks")
    # The symlink's own path *is* inside the allowlist (string-prefix would pass)...
    assert str(link).startswith(str(allowed))
    configure(allowed_dir=allowed)

    # ...but it must still be rejected because it RESOLVES outside.
    _assert_rejected_yielding_nothing(GeoNamesConnector(), {"country": "VA", "path": str(link)})


def test_path_with_no_allowlist_configured_is_rejected(configure: Callable[..., None]) -> None:
    """Default-deny: a ``path`` supplied with ``geonames_allowed_path_dir`` unset is rejected.

    Even though the target (the real VA fixture) is a perfectly valid dump, production that
    never sets an allowlist must be safe by construction.
    """
    configure(allowed_dir=None)  # no allowlist at all

    _assert_rejected_yielding_nothing(
        GeoNamesConnector(), {"country": "VA", "path": str(_VA_FIXTURE)}
    )


def test_oversize_file_inside_allowlist_is_rejected(
    tmp_path: Path, configure: Callable[..., None]
) -> None:
    """An in-allowlist file larger than ``geonames_max_path_bytes`` is rejected (size cap)."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    big = allowed / "VA.txt"
    big.write_text("1\tx\t\t\t1\t2\t\t\tVA\n" * 200, encoding="utf-8")  # comfortably > 16 bytes
    assert big.stat().st_size > 16
    configure(allowed_dir=allowed, max_bytes=16)

    _assert_rejected_yielding_nothing(GeoNamesConnector(), {"country": "VA", "path": str(big)})


# --------------------------------------------------------------------------------------------------
# Positive control — an IN-allowlist path still works (the fix must not just disable ``path``).
# --------------------------------------------------------------------------------------------------


def test_in_allowlist_path_still_yields_records(configure: Callable[..., None]) -> None:
    """The real VA.txt fixture (allowlist = fixtures dir) still yields records."""
    configure(allowed_dir=_FIXTURES_DIR)
    records = list(GeoNamesConnector().collect({"country": "VA", "path": str(_VA_FIXTURE)}))
    assert len(records) >= 100  # the Vatican dump is ~130 rows
    assert "3164670" in {record.key for record in records}  # State of the Vatican City


# --------------------------------------------------------------------------------------------------
# The error type is the named, specific ValueError subclass the ADR mandates.
# --------------------------------------------------------------------------------------------------


def test_path_error_type_is_specific_value_error_subclass() -> None:
    """``GeoNamesPathError`` exists and is a ``ValueError`` subclass (ADR 0052 D2)."""
    from worldmonitor.plugins.connectors.geonames.connector import GeoNamesPathError as Err

    assert issubclass(Err, ValueError)
