"""Gate 0 Slice 0c PRIMARY invariant — FtM version-pinning + schema vendoring guard.

This test encodes the four Slice 0c assertions BEFORE the builder lands them.
It FAILS on the current (pre-builder) tree for the right reasons:

  - test 1: pyproject.toml uses ``>=`` floors, not exact ``==`` pins.
  - test 2: currently PASSES (installed followthemoney==4.9.2 matches pin).
  - test 3: ``ontology/vendor/ftm/`` does not exist yet → AssertionError.
  - test 4: ``ontology/vendor/ftm/`` does not exist yet → AssertionError.

A fully GREEN run proves Slice 0c is complete: exact pins are in pyproject.toml,
the 69 FtM schema YAMLs are vendored byte-identical to the pinned install, and
the provenance note is present.

No network, no Docker, no ``@given``.  The schema diff IS the guard.
This module is SELF-CONTAINED: it does NOT import any ``scripts/`` helper.
"""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Locate repo root from this file: tests/test_ftm_schema_vendored.py -> parents[1].
REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact == pins that Slice 0c must establish in pyproject.toml.
EXPECTED_PINS: dict[str, str] = {
    "followthemoney": "4.9.2",
    "followthemoney-graph": "0.1.0",
    "nomenklatura": "4.10.0",
}

# Authoritative count of YAML files in the pinned FtM schema dir.
# Explicit anchor: guards against both dirs being accidentally empty.
EXPECTED_YAML_COUNT = 69

# The vendored schema directory created by Slice 0c.
VENDOR_DIR = REPO_ROOT / "ontology" / "vendor" / "ftm"

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Canonicalize a PEP 503 package name: lowercase + hyphens for underscores."""
    return name.lower().replace("_", "-")


def _get_pyproject_deps() -> list[str]:
    """Return the raw [project].dependencies list from pyproject.toml."""
    pyproject = REPO_ROOT / "pyproject.toml"
    assert pyproject.is_file(), f"pyproject.toml not found at {pyproject}"
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    deps: list[str] = data.get("project", {}).get("dependencies", [])
    return deps


def _installed_schema_dir() -> Path:
    """Locate the installed followthemoney/schema directory robustly.

    Uses ``import followthemoney`` to anchor the path — not a hard-coded
    site-packages string — so it works regardless of venv layout.
    """
    import os

    import followthemoney

    schema_dir = Path(os.path.dirname(followthemoney.__file__)) / "schema"
    assert schema_dir.is_dir(), (
        f"Expected followthemoney schema directory at {schema_dir}. "
        "Is followthemoney installed in this environment?"
    )
    return schema_dir


# ---------------------------------------------------------------------------
# Test 1 — exact == pins in pyproject.toml
# ---------------------------------------------------------------------------


def test_ftm_family_pinned_exactly() -> None:
    """pyproject.toml [project].dependencies must have exact == pins for the FtM family.

    Currently FAILS because the three entries use >= floors:

      followthemoney>=4.9.2     (must be ==4.9.2)
      followthemoney-graph>=0.1.0  (must be ==0.1.0)
      nomenklatura>=4.10        (must be ==4.10.0)

    The == pin is the silent-drift guard: if a CI runner resolves a newer FtM
    version, the installed schema and the vendored copy diverge without any alarm.
    An exact pin makes the drift visible immediately.

    Matching is case-insensitive and ``-``/``_`` equivalent (PEP 503 normalisation).
    """
    deps = _get_pyproject_deps()

    # Build normalised-name -> Requirement mapping for every declared dep.
    parsed: dict[str, Requirement] = {}
    for dep_str in deps:
        req = Requirement(dep_str)
        parsed[_normalize_name(req.name)] = req

    failures: list[str] = []
    for pkg_name, expected_version in EXPECTED_PINS.items():
        norm = _normalize_name(pkg_name)
        if norm not in parsed:
            failures.append(f"{pkg_name!r}: not found in [project].dependencies")
            continue

        req = parsed[norm]
        specs = list(req.specifier)

        if len(specs) != 1:
            failures.append(
                f"{pkg_name!r}: expected exactly one version specifier, "
                f"got {len(specs)}: {req.specifier!r}"
            )
            continue

        spec = specs[0]

        if spec.operator != "==":
            failures.append(
                f"{pkg_name!r}: operator is {spec.operator!r}, not '==' "
                f"(full specifier: {str(req.specifier)!r}). "
                f"Required: {pkg_name}=={expected_version}"
            )

        if spec.version != expected_version:
            failures.append(
                f"{pkg_name!r}: version is {spec.version!r}, "
                f"expected {expected_version!r}. "
                f"Required: {pkg_name}=={expected_version}"
            )

    assert not failures, (
        "FtM-family packages are NOT exactly == pinned in pyproject.toml:\n"
        + "\n".join(f"  - {f}" for f in failures)
        + "\n\nFix: replace every >= floor with an exact == pin:\n"
        + "".join(f"  {name}=={ver}\n" for name, ver in EXPECTED_PINS.items())
    )


# ---------------------------------------------------------------------------
# Test 2 — installed version matches the expected pin
# ---------------------------------------------------------------------------


def test_installed_ftm_version_matches_pin() -> None:
    """Installed followthemoney version must equal the expected pin 4.9.2.

    This catches the case where FtM was upgraded in the venv (resolving a newer
    version) without also re-vendoring the schema.  If the installed version
    drifts from the pin, the vendored snapshot is stale and must be regenerated.

    If this test fails after an intentional FtM upgrade: re-vendor the 69 *.yaml
    files, update pyproject.toml to ``followthemoney==<new_version>``, and update
    ``EXPECTED_PINS["followthemoney"]`` in this test.
    """
    installed = importlib.metadata.version("followthemoney")
    expected = EXPECTED_PINS["followthemoney"]
    assert installed == expected, (
        f"Installed followthemoney=={installed!r} does not match the "
        f"expected pin {expected!r}. "
        "If FtM was intentionally upgraded: re-copy the 69 *.yaml files from "
        "the installed package into ontology/vendor/ftm/, update "
        "pyproject.toml to the new == pin, and update EXPECTED_PINS in this test."
    )


# ---------------------------------------------------------------------------
# Test 3 — vendor dir exists and carries a provenance note
# ---------------------------------------------------------------------------


def test_vendored_schema_dir_exists_with_provenance() -> None:
    """ontology/vendor/ftm/ must exist and contain a provenance note.

    The provenance note (any non-YAML file whose text mentions both
    ``followthemoney`` and ``4.9.2``) makes the snapshot's origin unambiguous
    for future maintainers and auditors.

    Currently FAILS because ontology/vendor/ftm/ does not exist.
    """
    assert VENDOR_DIR.is_dir(), (
        f"Vendor directory {VENDOR_DIR} does not exist. "
        "Slice 0c must create ontology/vendor/ftm/ and populate it with the "
        f"{EXPECTED_YAML_COUNT} FtM schema YAML files plus a PROVENANCE note."
    )

    # Collect all non-YAML files; the provenance note is among them.
    non_yaml_files = [
        f for f in VENDOR_DIR.iterdir() if f.is_file() and f.suffix.lower() not in {".yaml", ".yml"}
    ]
    assert non_yaml_files, (
        f"No provenance/readme file found in {VENDOR_DIR}. "
        "Add a PROVENANCE.md (or README.md / PROVENANCE) that records the "
        "upstream package name and exact version (followthemoney==4.9.2) and "
        "the retrieval date."
    )

    # At least one non-YAML file must mention 'followthemoney' AND '4.9.2'.
    prov_found = False
    for candidate in non_yaml_files:
        try:
            text = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "followthemoney" in text and "4.9.2" in text:
            prov_found = True
            break

    assert prov_found, (
        f"No file in {VENDOR_DIR} (non-YAML) mentions both 'followthemoney' "
        "and '4.9.2'. The provenance note must state the upstream package name "
        "and the exact version the snapshot was taken from."
    )


# ---------------------------------------------------------------------------
# Test 4 — vendored *.yaml files are byte-identical to the installed schema
# ---------------------------------------------------------------------------


def test_vendored_schema_matches_installed() -> None:
    """Every vendored *.yaml must be byte-identical to the installed FtM schema file.

    This is the CORE L2-drift guard.  If FtM silently upgrades (adds a property,
    renames a schema type, removes an edge label) without a matching pin bump, the
    installed copy and the vendored copy diverge, and this assertion fires — making
    the drift visible at build time rather than at runtime.

    Scoping: ONLY *.yaml files are compared.  The provenance note (and any other
    non-YAML file) in the vendor dir is intentionally ignored so it does not
    trigger a false failure.

    Explicit count anchor: asserts len(vendored_yaml) == 69 so a partial/empty
    vendor dir cannot silently pass the set-equality check.

    Currently FAILS because ontology/vendor/ftm/ does not exist.
    """
    assert VENDOR_DIR.is_dir(), (
        f"Vendor directory {VENDOR_DIR} does not exist — "
        "run Slice 0c to create and populate it with the "
        f"{EXPECTED_YAML_COUNT} FtM schema YAML files."
    )

    installed_dir = _installed_schema_dir()

    installed_yaml_names = {f.name for f in installed_dir.glob("*.yaml")}
    vendored_yaml_names = {f.name for f in VENDOR_DIR.glob("*.yaml")}

    missing_from_vendor = installed_yaml_names - vendored_yaml_names
    extra_in_vendor = vendored_yaml_names - installed_yaml_names

    assert not missing_from_vendor, (
        f"The following installed FtM schema files are MISSING from {VENDOR_DIR}:\n"
        + "\n".join(f"  {name}" for name in sorted(missing_from_vendor))
        + "\nCopy them from the installed followthemoney package."
    )
    assert not extra_in_vendor, (
        f"The following *.yaml files in {VENDOR_DIR} are NOT in the installed "
        f"followthemoney=={importlib.metadata.version('followthemoney')} schema:\n"
        + "\n".join(f"  {name}" for name in sorted(extra_in_vendor))
        + "\nRemove files that were not vendored from the pinned release."
    )

    # Explicit count anchor: prevents a partial/empty pair from passing silently.
    assert len(vendored_yaml_names) == EXPECTED_YAML_COUNT, (
        f"Expected {EXPECTED_YAML_COUNT} vendored YAML files, "
        f"found {len(vendored_yaml_names)} in {VENDOR_DIR}. "
        "The vendored schema must contain exactly all FtM schema YAMLs from "
        f"followthemoney=={EXPECTED_PINS['followthemoney']}."
    )

    # Byte-identity check: iterate in sorted order so the first diff is deterministic.
    first_diff: str | None = None
    for name in sorted(vendored_yaml_names):
        installed_bytes = (installed_dir / name).read_bytes()
        vendored_bytes = (VENDOR_DIR / name).read_bytes()
        if installed_bytes != vendored_bytes:
            first_diff = name
            break

    installed_ver = importlib.metadata.version("followthemoney")
    assert first_diff is None, (
        f"Vendored schema file {first_diff!r} is NOT byte-identical to the "
        f"installed followthemoney=={installed_ver} schema. "
        "Re-copy the schema files from the installed package into "
        "ontology/vendor/ftm/. "
        "If FtM was upgraded: update the == pin in pyproject.toml, "
        "re-vendor all YAML files, and update EXPECTED_PINS in this test."
    )
