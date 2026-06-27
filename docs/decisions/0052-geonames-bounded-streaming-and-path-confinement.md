# 0052 — GeoNames connector: bounded streaming + fail-closed local-path confinement

- **Status:** PROPOSED
- **Date:** 2026-06-27
- **Gate:** H-6 (OOM) + H-7 (local-path LFI) — `gate/h6h7-geonames-oom-lfi`
- **Spec:** `docs/reviews/GATE_H6H7_GEONAMES_OOM_LFI_SPEC.md`
- **Supersedes / relates:** ADR 0021 (raw lands before mapping), ADR 0027 (windowed bounded ingest),
  ADR 0051 (driver `mem_limit` — turns this OOM into a hard kill, motivating the fix). No prior ADR
  covered the GeoNames `collect()` memory profile or the `path` override security posture.

## Context

`GeoNamesConnector.collect()` (connector.py:71-89) and `_download()` (118-125) have two HIGH
production-readiness defects, both verified against `master` @ `325f0a1`:

- **H-6 (OOM).** `collect()` reads the entire dump into RAM: `Path(path).read_text("utf-8")` for the
  local override, or `_download()` which pulls the whole `.zip` into `response.content`, wraps it in
  `io.BytesIO`, and `archive.read(member).decode("utf-8")` to a full string — then `text.splitlines()`
  materializes a full list. Peak RAM ≈ compressed zip + full decompressed string + full list, all live
  at once. `US` / `CN` / `allCountries` are hundreds of MB to GB and OOM-kill the now-`mem_limit`ed
  (ADR 0051) driver container. The ingest path is already windowed (ADR 0027), so a lazy `collect()`
  bounds steady-state RAM to ~one window.
- **H-7 (LFI).** `config.schema.json` constrains `country` to `^[A-Za-z]{2}$` but `path` is an
  unconstrained string with no base-dir confinement and no size bound; `collect()` does
  `Path(str(local_path)).read_text("utf-8")` verbatim. `path: "/etc/passwd"` /
  `path: "/proc/self/environ"` / `path: "../../.env"` reads that file into the GDPR-readable landing
  zone. The blast radius widens the moment a self-service connector-config API lets a caller create a
  `ConnectorInstance`: that caller could read any file the driver process can read — including
  `CONFIG_ENCRYPTION_KEY` / `NEO4J_PASSWORD` / `MINIO_SECRET_KEY` / DSNs.

The local `path` override is load-bearing: the `tests/fixtures/geonames/VA.txt` fixture and offline dev
depend on it. So `path` must stay, but be made safe.

## Decision

### D1 — H-6: stream the download to a temp **file**; iterate the dump **lazily**.

`collect()` stays a generator but becomes lazy end-to-end:

- **Local path:** iterate the file handle line by line (`_iter_local_lines`), not
  `read_text().splitlines()`.
- **Download:** `_stream_to_tempfile(response)` writes `response.iter_bytes()` chunks to a
  `NamedTemporaryFile` (whole zip on **disk**, never in RAM); `_iter_zip_lines(zip_path, member)` opens
  the zip from the path and yields lines via `io.TextIOWrapper(archive.open(member))` (incremental
  decompression); `_download_lines(country)` wires `httpx.stream(...)` → temp file → lazy lines with a
  `try/finally` temp-file cleanup. `raise_for_status()`, `_HTTP_TIMEOUT`, `follow_redirects` preserved.

The bound is **RAM**, not I/O: a zip's central directory is at its end, so the full zip MUST be on disk
to be read — the win is that peak RAM is one chunk + one decompressed line, not the whole file. Lines are
`rstrip("\n")`-stripped so each emitted `RawRecord` is **byte-identical** to today's
`splitlines()`-based output (the FROZEN negative-space invariant).

### D2 — H-7: fail-closed, default-deny, realpath-confined + size-capped local `path`.

Two new settings:

- `geonames_allowed_path_dir: str = ""` — the allowlist base dir for `path`. **Empty default ⇒ `path`
  is rejected entirely (default-deny).** Production that never sets it is safe by construction; dev/test
  sets it to the fixtures dir.
- `geonames_max_path_bytes: int = Field(default=268_435_456, gt=0)` — 256 MiB defense-in-depth size cap.

`_resolve_confined_path(local_path)` (raising `GeoNamesPathError(ValueError)`) runs before any record is
yielded: reject if no allowlist is configured; `base = Path(allowed).resolve(strict=True)`;
`real = Path(local_path).resolve(strict=True)` (defeats `..` AND symlinks; non-existent target raises);
require `real.is_relative_to(base)` else reject; reject if `real.stat().st_size > max_bytes`; return
`real`. The **runtime** realpath-inside-allowlist check is the security boundary — NOT the JSON schema,
which doubles as the self-service UI-form driver and is bypassable by a direct caller. `path` stays in
the schema with a doc-only description change ("dev/offline only; runtime-confined").

### D3 — `country` is unchanged (already safe).

`validate_config()` (jsonschema, `^[A-Za-z]{2}$`) runs at the top of `collect()` before `_download()`;
two ASCII letters `.upper()`-cased cannot inject a scheme/host/`..` into `{_BASE_URL}/{country}.zip`. No
change.

## Alternatives considered (rejected)

**H-7 path handling:**

- **(A) Delete `path` from the production schema (dev-only build flag).** Rejected: the `VA.txt`
  fixture and offline dev depend on `path`; gating it behind a separate dev build would fork the code
  path that tests exercise from the one production runs (tests would cover a path production cannot
  reach). Default-deny via an unset allowlist achieves the same production safety without a code fork —
  production simply never sets `geonames_allowed_path_dir`.
- **(B) Env-flag-only (`GEONAMES_ALLOW_LOCAL_PATH=true`) with no base-dir confinement.** Rejected: a
  boolean flag re-opens full LFI the instant it is set (e.g. by a well-meaning operator who wants ONE
  fixture). The allowlist *directory* is itself the confinement — there is no "on but unconfined" state.
- **(C) `chroot` / container-only filesystem isolation.** Rejected: heavier, OS/deploy-specific, and
  orthogonal — it does not stop an in-container `/proc/self/environ` read, and the driver already needs
  to read its own config. Application-level realpath confinement is the right layer and is unit-testable.
- **(D) A `pattern` on the schema `path` field only.** Rejected: the schema is the UI-form driver, not a
  security boundary; a direct caller bypasses it. (We still update the schema *description*, but the
  control is the runtime check.)

**H-6 streaming:**

- **(E) Keep `response.content` but `del` it after extracting.** Rejected: peak still includes the whole
  zip + whole decompressed string simultaneously — the OOM is at peak, not at steady state.
- **(F) Stream the zip member straight from the network without a temp file.** Rejected: impossible for
  a zip (central directory is at the end; you cannot decompress a member before the whole archive is
  available). Streaming to a temp file on disk is the correct RAM-bound.
- **(G) `tracemalloc`-free laziness proof via timing.** Rejected for the tests: wall-clock is flaky. The
  spec's deterministic proofs are a `tracemalloc` peak bound with a large margin + a fake response whose
  `.content` access raises + an iterator-type check on the zip seam.

**Heartbeat-style storage / DB:** N/A — this gate adds no table and no migration.

## Consequences

- Production with `GEONAMES_ALLOWED_PATH_DIR` unset cannot be tricked into a local file read
  (default-deny). Dev/test sets it to the fixtures dir.
- `collect()` peak RAM is bounded regardless of country size; the windowed ingest (ADR 0027) keeps
  steady-state memory to ~one window. The driver `mem_limit` (ADR 0051) now bounds a *contained* read
  rather than masking an unbounded one.
- The existing happy-path test (`test_geonames_connector.py`) gains allowlist wiring (env +
  `get_settings.cache_clear()`); its `map()`/anchor/output assertions are unchanged.
- `map()`, the `geonames_id` anchor, the provenance stamp, and the emitted-record bytes are unchanged —
  G1 / append-only / canonical-via-guard hold **vacuously**; no graph/store mutation; no migration.

## Person-affecting / sign-off

**NOT person-affecting.** GeoNames is a reference gazetteer (places); the connector merges nothing,
scores nothing, mutates no ER threshold, touches no real-person record. No per-run human sign-off.
`human_fork: false`. This is security + memory hardening with a clear audit fix menu — PROPOSED, no
human STOP. Promote to ACCEPTED when both slices land green.
