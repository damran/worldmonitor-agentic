"""H-6 — GeoNames bounded-memory / streaming tests (Gate H-6/H-7, ADR 0052).

Today ``GeoNamesConnector.collect()`` reads the WHOLE dump into RAM (``read_text()`` +
``splitlines()`` for a local path; ``response.content`` + ``archive.read(member).decode()`` for
the download), so a large country (US / CN / allCountries) OOM-kills the now-``mem_limit``ed
driver container.

These tests pin the lazy / streaming contract the builder must satisfy (ADR 0052 D1):
  * ``collect()`` is a generator;
  * peak RAM stays far below whole-file size on a large synthetic dump (``tracemalloc`` bound);
  * the download STREAMS the zip (never touches ``response.content``) — proven with a fake httpx
    response whose ``.content`` access raises while ``iter_bytes()`` feeds the data;
  * the zip member is iterated LAZILY (``_iter_zip_lines`` returns an iterator, not a list);
  * the streaming seams exist.

Deterministic: no network, no wall-clock timing. The ~16 MiB / <2 MiB bound is an ~16x margin so
interpreter noise cannot flip it (spec §5).
"""

from __future__ import annotations

import inspect
import io
import os
import tracemalloc
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from worldmonitor.plugins.connectors.geonames.connector import GeoNamesConnector
from worldmonitor.settings import get_settings

# One realistic GeoNames TSV row (19 tab-separated columns, see the dump readme).
_SAMPLE_ROW = (
    "1234567\tPlace Name\tPlace Name\talt1,alt2,alt3\t41.90225\t12.4533\t"
    "P\tPPL\tVA\t\t00\t\t\t\t921\t\t62\tEurope/Vatican\t2026-02-25\n"
)


@pytest.fixture(autouse=True)
def _isolate_settings_cache() -> pytest.FixtureRequest:  # type: ignore[misc]
    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


@pytest.fixture
def configure(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Wire the H-7 allowlist so the local-path tests can read their in-allowlist synthetic dump."""

    def _apply(*, allowed_dir: str | os.PathLike[str], max_bytes: int | None = None) -> None:
        monkeypatch.setenv("GEONAMES_ALLOWED_PATH_DIR", str(allowed_dir))
        if max_bytes is None:
            monkeypatch.delenv("GEONAMES_MAX_PATH_BYTES", raising=False)
        else:
            monkeypatch.setenv("GEONAMES_MAX_PATH_BYTES", str(max_bytes))
        get_settings.cache_clear()

    return _apply


# --------------------------------------------------------------------------------------------------
# collect() is a lazy generator.
# --------------------------------------------------------------------------------------------------


def test_collect_returns_a_generator(tmp_path: Path, configure: Callable[..., None]) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    dump = allowed / "VA.txt"
    dump.write_text(_SAMPLE_ROW, encoding="utf-8")
    configure(allowed_dir=allowed)

    gen = GeoNamesConnector().collect({"country": "VA", "path": str(dump)})
    assert inspect.isgenerator(gen)
    first = next(gen)
    assert first.key == "1234567"
    gen.close()


# --------------------------------------------------------------------------------------------------
# Bounded peak RAM on a large local dump — the OOM regression guard.
# --------------------------------------------------------------------------------------------------


def test_collect_peak_memory_bounded_on_large_local_dump(
    tmp_path: Path, configure: Callable[..., None]
) -> None:
    """Iterating a ~16 MiB dump record-by-record keeps tracemalloc peak < 2 MiB (spec §5).

    Today's ``read_text()`` + ``splitlines()`` peaks at >= ~32 MiB (full string + full list live at
    once) — RED. Streaming peaks at ~one line — GREEN.
    """
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    dump = allowed / "VA.txt"
    target = 16 * 1024 * 1024
    with dump.open("w", encoding="utf-8") as handle:
        written = 0
        while written < target:
            handle.write(_SAMPLE_ROW)
            written += len(_SAMPLE_ROW)
    file_size = dump.stat().st_size
    assert file_size >= target
    configure(
        allowed_dir=allowed, max_bytes=file_size + 1
    )  # within the size cap, exercise streaming

    connector = GeoNamesConnector()
    tracemalloc.start()
    try:
        count = 0
        for _record in connector.collect({"country": "VA", "path": str(dump)}):
            count += 1  # retain NOTHING — only a running count
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert count > 100_000, "fixture must actually be large (the bound is meaningless otherwise)"
    assert peak < 2 * 1024 * 1024, (
        f"collect() peaked at {peak / 1024 / 1024:.1f} MiB on a {file_size / 1024 / 1024:.1f} MiB "
        "dump — it materialized the whole file instead of streaming"
    )


# --------------------------------------------------------------------------------------------------
# The download streams (never touches response.content).
# --------------------------------------------------------------------------------------------------


class _StreamingFakeResponse:
    """An httpx-response stand-in whose ``.content`` access RAISES; only ``iter_bytes`` works.

    Doubles as the value returned by both ``httpx.get`` (today's code path) and
    ``httpx.stream(...)`` (the streaming context-manager the builder must use).
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.iter_bytes_called = False

    def raise_for_status(self) -> None:
        return None

    @property
    def content(self) -> bytes:  # noqa: D401 - intentionally hostile
        raise AssertionError(
            "download must STREAM via iter_bytes() to a temp file — never read response.content"
        )

    def iter_bytes(self, chunk_size: int = 65536) -> Iterator[bytes]:
        self.iter_bytes_called = True
        for offset in range(0, len(self._payload), chunk_size):
            yield self._payload[offset : offset + chunk_size]

    def __enter__(self) -> _StreamingFakeResponse:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def _build_zip(member: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, text)
    return buffer.getvalue()


def test_download_streams_and_never_reads_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """The download path produces records via iter_bytes; touching ``.content`` would raise.

    Today ``_download`` does ``io.BytesIO(response.content)`` — the fake's ``.content``
    raises → RED. After streaming, ``iter_bytes`` feeds a temp file → GREEN.
    """
    tsv = "111\tAlpha\tAlpha\t\t1.0\t2.0\tP\tPPL\tVA\n222\tBeta\tBeta\t\t3.0\t4.0\tP\tPPL\tVA\n"
    fake = _StreamingFakeResponse(_build_zip("VA.txt", tsv))

    # Patch BOTH the streaming API (post-build) and the old blocking GET (so today the fake's
    # .content-on-get raises rather than hitting the network — RED for the RIGHT reason).
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: fake)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: fake)
    # The download path now routes through the SSRF guard (ADR 0057), which resolves the host via
    # socket.getaddrinfo before connecting — stub it to a public IP so this stays a hermetic unit
    # test (no live DNS on download.geonames.org). DNS-stub only; no assertion change.
    monkeypatch.setattr(
        "worldmonitor.net.ssrf.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    records = list(GeoNamesConnector().collect({"country": "VA"}))

    assert [record.key for record in records] == ["111", "222"]
    assert records[0].data == b"111\tAlpha\tAlpha\t\t1.0\t2.0\tP\tPPL\tVA"
    assert records[1].data == b"222\tBeta\tBeta\t\t3.0\t4.0\tP\tPPL\tVA"
    assert fake.iter_bytes_called, (
        "the download must consume the response via iter_bytes (streaming)"
    )


# --------------------------------------------------------------------------------------------------
# The zip member is iterated lazily; the seams exist.
# --------------------------------------------------------------------------------------------------


def test_streaming_seams_exist() -> None:
    """The lazy seams named by ADR 0052 D1 are present and callable."""
    from worldmonitor.plugins.connectors.geonames.connector import (
        _download_lines,
        _iter_zip_lines,
        _stream_to_tempfile,
    )

    assert callable(_iter_zip_lines)
    assert callable(_stream_to_tempfile)
    assert callable(_download_lines)


def test_iter_zip_lines_is_a_lazy_iterator(tmp_path: Path) -> None:
    """``_iter_zip_lines`` returns an iterator (NOT a list) and yields the member's lines."""
    from worldmonitor.plugins.connectors.geonames.connector import _iter_zip_lines

    zip_path = tmp_path / "VA.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("VA.txt", "111\tAlpha\n222\tBeta\n")

    result = _iter_zip_lines(zip_path, "VA.txt")
    assert isinstance(result, Iterator)
    assert not isinstance(result, list), "the member must be iterated lazily, not materialized"
    assert [line.rstrip("\n") for line in result] == ["111\tAlpha", "222\tBeta"]
