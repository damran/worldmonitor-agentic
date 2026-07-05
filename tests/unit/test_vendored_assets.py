"""Gate 1a — INV-VENDOR: htmx v2 is vendored (0BSD), zero-egress, PROVENANCE'd (ADR 0103/0098).

No app, no DB, no network — pure filesystem/text assertions against the tree, so this is the
cheapest possible RED-first oracle and cannot be satisfied by any runtime behaviour:

  * ``api/static/vendor/htmx.min.js`` exists and is non-trivial (> 5 KB) — a CDN-loaded / stub /
    placeholder file cannot pass.
  * ``api/templates/review.html`` references ONLY ``/static/...`` assets: no ``http://``,
    ``https://``, ``unpkg``, ``jsdelivr``, or ``cdn`` anywhere in the template source (zero-egress,
    `docs/70` §1.7/§9).
  * ``api/static/vendor/PROVENANCE.md`` records a version whose MAJOR is ``2``, the ``0BSD``
    license, and a sha256 that EQUALS the actual sha256 of the vendored ``htmx.min.js`` (so the
    provenance can never silently drift from the shipped bytes, mirroring the ADR-0098 FtM
    vendor-as-data pattern).

RED now: none of ``htmx.min.js`` / ``review.html`` / ``PROVENANCE.md`` exist yet under
``src/worldmonitor/api``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[2] / "src" / "worldmonitor" / "api"
_VENDOR_DIR = _API_DIR / "static" / "vendor"
_HTMX_PATH = _VENDOR_DIR / "htmx.min.js"
_PROVENANCE_PATH = _VENDOR_DIR / "PROVENANCE.md"
_REVIEW_TEMPLATE_PATH = _API_DIR / "templates" / "review.html"

_MIN_HTMX_BYTES = 5 * 1024
_FORBIDDEN_HOSTS = ("http://", "https://", "unpkg", "jsdelivr", "cdn")
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
_VERSION_RE = re.compile(r"\b(2)\.\d+\.\d+\b")


def test_htmx_is_vendored_and_non_trivial() -> None:
    assert _HTMX_PATH.is_file(), (
        f"vendored htmx missing at {_HTMX_PATH} — htmx v2 must be downloaded at build time and "
        "committed under api/static/vendor/htmx.min.js (ADR 0103 Decision E)"
    )
    size = _HTMX_PATH.stat().st_size
    assert size > _MIN_HTMX_BYTES, (
        f"vendored htmx.min.js looks truncated/placeholder-like: {size} bytes "
        f"(expected > {_MIN_HTMX_BYTES})"
    )


def test_review_template_references_only_local_static_assets_no_cdn() -> None:
    assert _REVIEW_TEMPLATE_PATH.is_file(), f"review.html missing at {_REVIEW_TEMPLATE_PATH}"
    html = _REVIEW_TEMPLATE_PATH.read_text(encoding="utf-8")
    lowered = html.lower()
    for forbidden in _FORBIDDEN_HOSTS:
        assert forbidden not in lowered, (
            f"review.html must be zero-egress (self-hosted only) but contains {forbidden!r} — "
            "no CDN-loaded front-end library is allowed (ADR 0103 Decision E)"
        )
    assert "/static/" in html, "review.html must load its vendored assets from /static/..."


def test_provenance_records_major_2_0bsd_license_and_matching_sha256() -> None:
    assert _PROVENANCE_PATH.is_file(), f"PROVENANCE.md missing at {_PROVENANCE_PATH}"
    assert _HTMX_PATH.is_file(), "htmx.min.js must exist to verify the recorded sha256 against it"
    text = _PROVENANCE_PATH.read_text(encoding="utf-8")

    version_match = _VERSION_RE.search(text)
    assert version_match is not None, (
        "PROVENANCE.md must record a pinned htmx version with MAJOR 2 (a 2.x.y semver string)"
    )

    assert "0BSD" in text, "PROVENANCE.md must record the 0BSD license"

    actual_sha256 = hashlib.sha256(_HTMX_PATH.read_bytes()).hexdigest()
    sha_match = _SHA256_RE.search(text)
    assert sha_match is not None, "PROVENANCE.md must record a sha256 hex digest of htmx.min.js"
    recorded_sha256 = sha_match.group(1).lower()
    assert recorded_sha256 == actual_sha256, (
        f"PROVENANCE.md sha256 {recorded_sha256!r} does not match the ACTUAL vendored file hash "
        f"{actual_sha256!r} — the provenance record has drifted from the shipped bytes"
    )
