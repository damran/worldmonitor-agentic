"""GeoNames connector — reference gazetteer anchored on geonames_id.

GeoNames publishes per-country tab-separated dumps at ``download.geonames.org``.
Each feature becomes an FtM ``Address`` (the closest FtM-native geo schema; a
``wm:Place`` extension is a noted future refinement) carrying the ``geonames_id``
canonical anchor. A passive ``EXTERNAL_IMPORT`` connector — it never writes to the
graph. Supports a local ``path`` override for environments where the live source
is unreachable.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from worldmonitor.net.ssrf import guarded_stream
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.plugins.base import (
    Capability,
    Connector,
    Kind,
    Manifest,
    Mode,
    RawRecord,
    Status,
)
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.settings import get_settings

_BASE_URL = "https://download.geonames.org/export/dump"
_HTTP_TIMEOUT = 120.0
# Chunk size for streaming the .zip to disk (one chunk lives in RAM at a time).
_STREAM_CHUNK_BYTES = 65536

# Column indices in the GeoNames dump (tab-separated, see its readme.txt).
_COL_GEONAMEID = 0
_COL_NAME = 1
_COL_LAT = 4
_COL_LON = 5
_COL_COUNTRY = 8


class GeoNamesPathError(ValueError):
    """Raised when a local ``path`` override violates the confinement / size policy (ADR 0052 D2).

    A ``ValueError`` subclass so callers can catch it precisely while it still reads as the
    "bad input" category. Raised for: no allowlist configured (default-deny), a path that resolves
    outside the allowlist (absolute escape / ``..`` traversal / symlink), a non-existent target, or
    a file larger than ``geonames_max_path_bytes``.
    """


def _stream_to_tempfile(response: Any) -> Path:
    """Stream an httpx response body to a temp FILE via ``iter_bytes`` — never via ``.content``.

    The whole ``.zip`` lands on DISK (a zip's central directory is at the end, so the archive must
    be fully present to be opened); peak RAM is one chunk, not the whole file. Returns the temp-file
    path; the caller owns cleanup.
    """
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        for chunk in response.iter_bytes(_STREAM_CHUNK_BYTES):
            tmp.write(chunk)
        return Path(tmp.name)


def _iter_zip_lines(zip_path: Path, member: str) -> Iterator[str]:
    """Yield the member's lines LAZILY (incremental decompression), one line at a time.

    Opens the zip from a path and wraps the member in ``io.TextIOWrapper`` so decompression and
    decoding happen incrementally — the whole decompressed text is never materialized in RAM. Lines
    are yielded WITH their trailing terminator (universal-newline translated); the caller strips it.
    """
    with (
        zipfile.ZipFile(zip_path) as archive,
        io.TextIOWrapper(archive.open(member), encoding="utf-8") as reader,
    ):
        yield from reader


def _download_lines(country: str) -> Iterator[str]:
    """Stream the country ``.zip`` to a temp file, then yield its dump lines lazily.

    ``httpx.stream(...)`` + ``iter_bytes`` → temp file on disk → lazy zip-member iteration; the temp
    file is removed when the generator is exhausted or closed.
    """
    url = f"{_BASE_URL}/{country}.zip"
    with guarded_stream("GET", url, timeout=_HTTP_TIMEOUT) as response:
        response.raise_for_status()
        zip_path = _stream_to_tempfile(response)
    try:
        yield from _iter_zip_lines(zip_path, f"{country}.txt")
    finally:
        zip_path.unlink(missing_ok=True)


def _iter_local_lines(path: Path) -> Iterator[str]:
    """Yield a local dump's lines LAZILY: iterate the handle, never ``read_text``/``splitlines``.

    The file is opened in text mode (universal newlines). Lines are yielded WITH their trailing
    terminator (universal-newline mode translates ``\\r\\n`` -> ``\\n``); the caller strips it so
    each emitted record is byte-identical to the legacy read.
    """
    with path.open("r", encoding="utf-8") as handle:
        yield from handle


class GeoNamesConnector(Connector):
    """Loads GeoNames places (FtM Address) with the ``geonames_id`` anchor."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="geonames",
            name="GeoNames",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description="Reference gazetteer; anchors places to GeoNames IDs.",
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Yield one raw record per GeoNames dump line for the configured country.

        Lazy end-to-end (H-6): the local read iterates the file handle and the download streams
        the ``.zip`` to a temp file and iterates the zip member, so peak RAM is bounded to ~one
        line. The local ``path`` override is confined to ``geonames_allowed_path_dir`` (H-7) —
        confinement runs before any record is yielded, so a rejected path fails closed (no records).
        """
        self.validate_config(config)
        retrieved_at = datetime.now(UTC).isoformat()
        local_path = config.get("path")
        if local_path:
            lines = _iter_local_lines(self._resolve_confined_path(str(local_path)))
        else:
            lines = _download_lines(str(config["country"]).upper())
        for raw_line in lines:
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue
            geoname_id = line.split("\t", 1)[0]
            yield RawRecord(
                key=geoname_id,
                data=line.encode("utf-8"),
                retrieved_at=retrieved_at,
                content_type="text/tab-separated-values",
            )

    @staticmethod
    def _resolve_confined_path(local_path: str) -> Path:
        """Resolve + confine a local ``path`` override to the allowlist; raise on any violation.

        Fail-closed, default-deny (ADR 0052 D2): the RUNTIME realpath-inside-allowlist check is the
        security boundary (NOT the JSON schema). ``Path.resolve(strict=True)`` defeats ``..`` AND
        symlinks (realpath, not string-prefix) and raises on a non-existent target. The size cap is
        defense-in-depth against a huge in-allowlist file.
        """
        settings = get_settings()
        allowed = settings.geonames_allowed_path_dir
        if not allowed:
            raise GeoNamesPathError(
                "local `path` override requires `geonames_allowed_path_dir` to be configured "
                "(default-deny)"
            )
        try:
            base = Path(allowed).resolve(strict=True)
        except OSError as exc:
            raise GeoNamesPathError(
                f"configured geonames_allowed_path_dir does not exist: {allowed!r}"
            ) from exc
        try:
            real = Path(local_path).resolve(strict=True)
        except OSError as exc:
            raise GeoNamesPathError(
                f"local `path` does not resolve to an existing file: {local_path!r}"
            ) from exc
        if not real.is_relative_to(base):
            raise GeoNamesPathError(
                f"local `path` resolves outside the allowlist {str(base)!r}: {str(real)!r}"
            )
        size = real.stat().st_size
        if size > settings.geonames_max_path_bytes:
            raise GeoNamesPathError(
                f"local `path` is {size} bytes, over the geonames_max_path_bytes cap "
                f"({settings.geonames_max_path_bytes})"
            )
        return real

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Parse one GeoNames dump line into an FtM Address with its geonames_id anchor."""
        columns = record.data.decode("utf-8").split("\t")
        if len(columns) <= _COL_COUNTRY:
            return []
        geoname_id = columns[_COL_GEONAMEID].strip()
        name = columns[_COL_NAME].strip()
        if not geoname_id or not name:
            return []
        properties: dict[str, list[str]] = {"name": [name]}
        if columns[_COL_COUNTRY].strip():
            properties["country"] = [columns[_COL_COUNTRY].strip().lower()]
        if columns[_COL_LAT].strip():
            properties["latitude"] = [columns[_COL_LAT].strip()]
        if columns[_COL_LON].strip():
            properties["longitude"] = [columns[_COL_LON].strip()]
        entity = validate_or_raise(
            {
                "id": f"geonames-{geoname_id}",
                "schema": "Address",
                "properties": properties,
                "datasets": ["geonames"],
            }
        )
        set_anchor(entity, "geonames_id", geoname_id)
        return [stamp(entity, provenance)]
