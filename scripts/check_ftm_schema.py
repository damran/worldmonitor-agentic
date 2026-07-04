#!/usr/bin/env python3
"""Schema-diff CI gate for the vendored FtM L2 schema (ADR 0098).

Asserts:
  1. The installed followthemoney version matches the == pin in pyproject.toml.
  2. The installed schema *.yaml files are byte-identical to ontology/vendor/ftm/.

Usage:
  uv run python scripts/check_ftm_schema.py

Exits 0 on full match; exits 1 with a clear stderr message on any divergence.
Stdlib-only (plus followthemoney for schema path location).
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import sys
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate repo root (this file lives in scripts/, one level below the root).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_pinned_version() -> str:
    """Parse the == pin for followthemoney from pyproject.toml.

    Reads [project].dependencies, finds the followthemoney entry, and extracts
    the version from the ``==`` specifier. Exits 1 if not found or not pinned exactly.
    """
    pyproject = REPO_ROOT / "pyproject.toml"
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)

    deps: list[str] = data.get("project", {}).get("dependencies", [])
    for dep in deps:
        # Match "followthemoney==X.Y.Z" (case-insensitive, allow hyphen/underscore).
        m = re.match(r"followthemoney\s*==\s*([\d.]+)", dep, re.IGNORECASE)
        if m:
            return m.group(1)

    print(
        "ERROR: followthemoney == pin not found in [project].dependencies of pyproject.toml.\n"
        "Expected an entry like: followthemoney==4.9.2",
        file=sys.stderr,
    )
    sys.exit(1)


def _check_version(pinned: str) -> None:
    """Assert the installed followthemoney version matches the pin."""
    installed = importlib.metadata.version("followthemoney")
    if installed != pinned:
        print(
            f"ERROR: installed followthemoney=={installed} does not match "
            f"the pinned version {pinned!r} in pyproject.toml.\n"
            "Re-vendor the schema YAMLs and update the == pin, or ensure the "
            "correct version is installed.",
            file=sys.stderr,
        )
        sys.exit(1)


def _locate_installed_schema() -> Path:
    """Return the path to the installed followthemoney/schema directory."""
    import followthemoney

    schema_dir = Path(os.path.dirname(followthemoney.__file__)) / "schema"
    if not schema_dir.is_dir():
        print(
            f"ERROR: Expected followthemoney schema directory at {schema_dir}. "
            "Is followthemoney installed?",
            file=sys.stderr,
        )
        sys.exit(1)
    return schema_dir


def _compare_schemas(installed_dir: Path, vendor_dir: Path) -> list[str]:
    """Compare *.yaml sets and bytes; return a list of error strings (empty = ok).

    Only *.yaml files are compared. PROVENANCE.md and other non-YAML files in
    the vendor directory are intentionally ignored so they do not self-trip the gate.
    """
    installed_yamls = {f.name for f in installed_dir.glob("*.yaml")}
    vendored_yamls = {f.name for f in vendor_dir.glob("*.yaml")}

    errors: list[str] = []

    missing = installed_yamls - vendored_yamls
    if missing:
        errors.append(
            "Files present in installed schema but MISSING from vendor dir:\n"
            + "\n".join(f"  {name}" for name in sorted(missing))
        )

    extra = vendored_yamls - installed_yamls
    if extra:
        errors.append(
            "Files in vendor dir that are NOT in the installed schema:\n"
            + "\n".join(f"  {name}" for name in sorted(extra))
        )

    # Byte-identity check on the shared set.
    for name in sorted(installed_yamls & vendored_yamls):
        installed_bytes = (installed_dir / name).read_bytes()
        vendored_bytes = (vendor_dir / name).read_bytes()
        if installed_bytes != vendored_bytes:
            errors.append(f"Byte difference in {name!r}: installed schema != vendored copy.")

    return errors


def main() -> None:
    pinned = _read_pinned_version()
    _check_version(pinned)

    installed_dir = _locate_installed_schema()
    vendor_dir = REPO_ROOT / "ontology" / "vendor" / "ftm"

    if not vendor_dir.is_dir():
        print(
            f"ERROR: Vendor directory {vendor_dir} does not exist.\n"
            "Run Slice 0c setup to vendor the FtM schema YAMLs.",
            file=sys.stderr,
        )
        sys.exit(1)

    errors = _compare_schemas(installed_dir, vendor_dir)
    if errors:
        print(
            "ERROR: Installed FtM schema diverges from vendored copy in "
            "ontology/vendor/ftm/:\n" + "\n".join(errors),
            file=sys.stderr,
        )
        sys.exit(1)

    installed_count = sum(1 for _ in installed_dir.glob("*.yaml"))
    print(
        f"OK: followthemoney=={pinned} installed; {installed_count} schema YAMLs "
        "byte-identical to ontology/vendor/ftm/."
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
