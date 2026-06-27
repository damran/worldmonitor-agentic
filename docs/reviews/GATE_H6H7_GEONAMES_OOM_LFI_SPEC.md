# Gate H-6/H-7 — GeoNames connector: bound memory (OOM) + close the local-path LFI

- **Gate:** H-6 + H-7 (ONE coherent gate — both HIGH production-readiness findings live in the SAME
  connector and the SAME method, `GeoNamesConnector.collect()`).
- **Branch:** `gate/h6h7-geonames-oom-lfi` (off `master` @ `325f0a1`; clean).
- **ADR:** `docs/decisions/0052-geonames-bounded-streaming-and-path-confinement.md` (PROPOSED).
- **Severity:** two HIGH findings.
  - **H-6 (OOM):** `collect()` reads the WHOLE dump into RAM (string + list) — `US`/`CN`/`allCountries`
    OOM-kill the now-`mem_limit`ed (B-4c) driver container.
  - **H-7 (LFI):** the `path` config field is an unconstrained string with no base-dir confinement and
    no size bound — an operator (or, once the self-service connector-config API ships, ANY caller) can
    point it at `/etc/passwd` or `/proc/self/environ` and exfiltrate `CONFIG_ENCRYPTION_KEY` /
    `NEO4J_PASSWORD` / `MINIO_SECRET_KEY` / DSNs into the readable landing zone.
- **Why ONE gate.** Both defects are in `collect()` (connector.py:71-89) and `_download()`
  (connector.py:118-125). The H-6 streaming rewrite and the H-7 confinement check both sit in the
  local-path / download branches of the same method; splitting them across gates would mean two
  back-to-back rewrites of the same ~20 lines. They are two slices of one connector hardening.
- **Independently built on the other line:** Workflow A hardens its GeoNames connector on A's terms; B
  re-derives here against B's `collect()` + `_download()` + `get_settings()`. Does NOT copy.
- **NOT in this gate (hard stops, §11):** the `wm:Place` ontology extension; any change to `map()`'s
  FtM Address output; a generic plugin-wide path-confinement framework; the self-service
  connector-config API itself; the driver `mem_limit` (already B-4c).

---

## 1. The gap (verified against B's code @ `325f0a1`)

`src/worldmonitor/plugins/connectors/geonames/connector.py` (~126 lines):

### H-6 — unbounded memory in `collect()` / `_download()`

```
71  def collect(self, config): ...
77      text = Path(str(local_path)).read_text("utf-8")          # WHOLE local file -> one string
79      text = self._download(str(config["country"]).upper())    # download path (below)
80      for line in text.splitlines():                            # WHOLE file -> a full list
...
118 def _download(country):
120     response = httpx.get(...)                                 # whole .zip into response.content (RAM)
124     with zipfile.ZipFile(io.BytesIO(response.content)) ...
125         return archive.read(f"{country}.txt").decode("utf-8") # whole DECOMPRESSED text into RAM
```

Peak RAM ≈ compressed zip (`response.content`) **+** the full decompressed string (`archive.read(...)`)
**+** the full `splitlines()` list — all live at once. For `VA` (~30 KB) this is invisible; for `US` /
`CN` / `allCountries` it is hundreds of MB to GB and OOM-kills the driver container (B-4c gave it a
`mem_limit`, which now turns an unbounded read into a hard kill + restart loop). The ingest path is
already windowed (`run_ingest` drains `collect()` in `ingest_commit_every` windows, ADR 0027), so a
**lazy** `collect()` bounds steady-state RAM to roughly one window of records.

### H-7 — local-path LFI (arbitrary file read)

`config.schema.json` constrains `country` to `^[A-Za-z]{2}$` but `path` is:

```json
"path": { "type": "string", "title": "Local dump path", "description": "Optional path ..." }
```

— no `pattern`, no base-dir confinement, no size bound. `collect()` does
`Path(str(local_path)).read_text("utf-8")` verbatim (connector.py:77). The connector then lands the raw
bytes in the landing zone (the GDPR-readable raw store). So `path: "/etc/passwd"`,
`path: "/proc/self/environ"`, or `path: "../../.env"` reads that file straight into the landing zone.
The blast radius widens the moment a self-service connector-config API exists: an authenticated caller
who can create a `ConnectorInstance` can read any file the driver process can read — including the
secrets the driver itself holds.

`country` is **safe** and needs no further hardening: `validate_config()` (jsonschema) runs at the top
of `collect()` and rejects anything not matching `^[A-Za-z]{2}$` **before** `_download()`; two ASCII
letters `.upper()`-cased cannot inject a scheme, host, or `..` into `{_BASE_URL}/{country}.zip`.
Confirmed — no change to `country` handling (§11).

---

## 2. The load-bearing testable invariants

Two positive invariants, each failing-test-first:

- **H-7 (confinement / fail-closed):** a `path` that resolves OUTSIDE the configured allowlist base dir
  — an absolute escape (`/etc/passwd`), a `..` traversal that escapes, and a **symlink** that points
  outside — is **REJECTED** (raises a clear, specific error) and yields **NO** records. A `path` INSIDE
  the allowlist still works (the `VA.txt` fixture, allowlist = the fixtures dir). An **over-size** file
  is rejected. A `path` supplied with **no allowlist configured** is rejected (default-deny → production
  with no allowlist set is safe by construction).
- **H-6 (bounded memory / streaming):** `collect()` is lazy — peak RAM stays bounded far below
  whole-file size regardless of input size; the download streams the zip to a temp **file** (never the
  whole zip / whole decompressed text into RAM); the zip member is iterated lazily.

The **negative-space invariant** (FROZEN, §9): the emitted `RawRecord`s and the resulting FtM `Address`
output are **byte-identical** before and after this gate. Streaming and confinement change *how*
`collect()` reads; they must not change *what* it emits.

### 2.1 How each invariant is proven (deterministic, non-flaky)

| Concern | How proven |
|---|---|
| H-7 confinement: outside-allowlist `path` (abs / `..` / symlink) rejected, yields nothing | **unit** — `tests/unit/test_geonames_security.py`, real temp dirs + a real symlink |
| H-7 default-deny: `path` with no allowlist configured is rejected | **unit** — same file |
| H-7 size cap: over-`geonames_max_path_bytes` file rejected | **unit** — same file |
| H-7 happy path still works inside the allowlist (VA.txt) | **unit** — existing `test_geonames_connector.py`, with allowlist wired (§9) |
| H-6 peak RAM bound on a large synthetic local file | **unit** — `tests/unit/test_geonames_streaming.py`, `tracemalloc` peak << file size |
| H-6 download streams (never touches `response.content`) | **unit** — fake httpx response whose `.content` access RAISES; streaming via `iter_bytes` still produces records |
| H-6 zip member iterated lazily | **unit** — the `_iter_zip_lines` seam returns a generator/iterator (not a list) and is correct on a small real zip |
| H-6 `collect()` is a generator that yields its first record | **unit** — `inspect.isgenerator(...)` + `next(...)` |

No wall-clock timing, no network, no live stack. The `tracemalloc` bound uses a large margin (below) so
interpreter noise cannot flip it.

---

## 3. Design — smallest correct footprint

`collect()` is the single seam. Both fixes factor it into testable helpers and keep `map()` untouched.

### 3.1 H-7 — fail-closed path confinement (the security boundary)

Two new settings in `src/worldmonitor/settings.py`:

- `geonames_allowed_path_dir: str = ""` — the allowlist base directory for the local `path` override.
  **Default empty ⇒ the `path` override is rejected entirely (default-deny).** Production that never sets
  it can never be tricked into a local read; dev/test sets it to the fixtures dir.
- `geonames_max_path_bytes: int = Field(default=268_435_456, gt=0)` — defense-in-depth size cap
  (256 MiB) on a local read; raise on exceedance.

A new helper on the connector — `_resolve_confined_path(local_path: str) -> Path` — runs **eagerly**
(before any record is yielded, so `list(collect(cfg))` raises and produces nothing):

1. `allowed = get_settings().geonames_allowed_path_dir`. If empty ⇒ **raise** (default-deny) with a
   clear message ("local `path` override requires `geonames_allowed_path_dir` to be configured").
2. `base = Path(allowed).resolve(strict=True)` — must exist (a misconfigured allowlist fails closed).
3. `real = Path(local_path).resolve(strict=True)` — `resolve()` defeats `..` AND symlinks;
   `strict=True` means a non-existent target raises (no silent empty read).
4. Require `real.is_relative_to(base)` (Python 3.12). Otherwise ⇒ **raise** (fail-closed).
5. `if real.stat().st_size > get_settings().geonames_max_path_bytes:` ⇒ **raise**.
6. return `real`.

The raised error is a single specific type — define `GeoNamesPathError(ValueError)` (module-level in
`connector.py`) so tests can assert on it precisely and callers get an unambiguous signal. (A bare
`PermissionError`/`ValueError` is acceptable but the named subclass is preferred — decide in the ADR.)

> **Why confinement is the boundary, not the schema.** `config.schema.json` is ALSO the self-service
> UI-form driver, so a schema `pattern` is advisory, not a security control — a caller hitting the
> connector directly bypasses it. The RUNTIME `realpath`-inside-allowlist check is the boundary. `path`
> stays in the schema (it is load-bearing for the `VA.txt` fixture + offline dev) but its description is
> updated to "dev/offline only; runtime-confined to `geonames_allowed_path_dir`." Schema change is
> **doc-only** (description text); the `type`/`additionalProperties` are unchanged.

> **Why the size cap does not fully cover `/proc`.** `/proc/self/environ` reports `st_size == 0`, so the
> size cap alone would not stop it — the **confinement** (step 4) does, because `/proc/...` is outside
> any sane allowlist. The size cap is secondary (defense-in-depth against a huge in-allowlist file and a
> secondary OOM vector); confinement is primary.

### 3.2 H-6 — bounded streaming (the memory boundary)

`collect()` becomes (still) a generator, but lazy end-to-end:

- **Local path:** after `_resolve_confined_path()` returns `real`, iterate the file handle line by line
  (`with real.open("r", encoding="utf-8") as fh: for line in fh:`) instead of `read_text().splitlines()`.
  Factor as `_iter_local_lines(path: Path) -> Iterator[str]`.
- **Download:** replace `_download` with a streaming generator:
  - `_stream_to_tempfile(response) -> Path` — iterate `response.iter_bytes()` writing chunks to a
    `NamedTemporaryFile(delete=False)`; the whole zip lands on **disk**, never in RAM. (The zip central
    directory is at the end of the file, so the full zip MUST be on disk to be opened — the bound is
    *RAM*, not I/O. This is the correct invariant: peak RAM is one chunk + one decompressed line, not the
    whole file.)
  - `_iter_zip_lines(zip_path: Path, member: str) -> Iterator[str]` — open the zip from the path and
    `io.TextIOWrapper(archive.open(member), encoding="utf-8")`, yielding lines lazily (decompresses
    incrementally).
  - `_download_lines(country: str) -> Iterator[str]` — `httpx.stream("GET", url, ...)` →
    `_stream_to_tempfile` → `_iter_zip_lines`; a `try/finally` deletes the temp file when the generator
    is exhausted or closed. (`raise_for_status()` preserved; `_HTTP_TIMEOUT`, `follow_redirects` kept.)

**Byte-identity (load-bearing).** `read_text().splitlines()` strips the line terminator. Iterating a
text file handle / `TextIOWrapper` yields lines WITH the trailing `\n` (universal-newline mode
translates `\r\n` → `\n`). The streamed line MUST be `line.rstrip("\n")` (or equivalent) so each
`RawRecord(data=line.encode("utf-8"), key=line.split("\t",1)[0], ...)` is **byte-identical** to today's
output. The blank-line skip (`if not line.strip(): continue`) is preserved. The FROZEN happy-path
`map()` test (§9) guards this.

`collect()` after the rewrite, in shape:

```
def collect(self, config):
    self.validate_config(config)
    retrieved_at = datetime.now(UTC).isoformat()
    local_path = config.get("path")
    if local_path:
        lines = self._iter_local_lines(self._resolve_confined_path(str(local_path)))   # H-7 eager
    else:
        lines = self._download_lines(str(config["country"]).upper())
    for line in lines:                                                                  # H-6 lazy
        line = line.rstrip("\n")
        if not line.strip():
            continue
        geoname_id = line.split("\t", 1)[0]
        yield RawRecord(key=geoname_id, data=line.encode("utf-8"),
                        retrieved_at=retrieved_at, content_type="text/tab-separated-values")
```

(`_resolve_confined_path` runs eagerly enough that `list(collect(cfg))` raises on a bad `path` and
yields nothing — acceptable that the raise surfaces on the first `next()` since the loop has no prior
yield. If a strictly-eager raise is wanted, split `collect` into an eager prelude that returns an inner
generator; either is acceptable — decide at build time.)

### 3.3 `settings.py` knobs (12-factor, safe defaults)

- `geonames_allowed_path_dir: str = ""` — default-deny (see 3.1).
- `geonames_max_path_bytes: int = Field(default=268_435_456, gt=0)` — 256 MiB cap.

`.env.example` documents both with the security rationale (production: leave `GEONAMES_ALLOWED_PATH_DIR`
unset).

### 3.4 Settings access + testability

The connector reads `get_settings()` lazily inside `collect()` / `_resolve_confined_path()`. The
`GeoNamesConnector()` constructor stays **no-arg** (the plugin registry instantiates connectors with no
args; the existing test does `GeoNamesConnector()`). Tests configure the allowlist via
`monkeypatch.setenv("GEONAMES_ALLOWED_PATH_DIR", ...)` + `get_settings.cache_clear()` (a small fixture);
the test-author owns that fixture.

---

## 4. Acceptance criteria (crisp APPROVE bar)

1. **H-7** `tests/unit/test_geonames_security.py` proves: `path` outside the allowlist via (a) absolute
   `/etc/passwd`, (b) a `..` traversal escaping the allowlist, (c) a symlink inside the allowlist
   pointing outside — each RAISES the specific error and `list(collect(...))` yields **no** records;
   `path` with `geonames_allowed_path_dir` unset RAISES (default-deny); an over-`geonames_max_path_bytes`
   file inside the allowlist RAISES; a valid file INSIDE the allowlist yields records.
2. **H-6** `tests/unit/test_geonames_streaming.py` proves: a large synthetic local dump consumed
   record-by-record (records NOT retained) keeps `tracemalloc` peak well below file size (see §5 for the
   bound); the download path with a fake httpx response whose `.content` access RAISES still produces
   records (proving streaming, not `.content`); `_iter_zip_lines` returns an iterator (not a list) and is
   correct on a small real zip; `collect()` is a generator (`inspect.isgenerator`).
3. The emitted `RawRecord`s + the FtM `Address` output are byte-identical:
   `tests/unit/test_geonames_connector.py::test_maps_known_place_to_geonames_id` stays green (with the
   allowlist wired) — its `map()`/anchor/`country == ["va"]` assertions are **unchanged**.
4. `country` handling is unchanged; `validate_config` still rejects a non-`^[A-Za-z]{2}$` country before
   any download.
5. No migration; no DB model change; no `map()` change; the FROZEN suites (§9) stay green.

---

## 5. Failing-test-first (RED → GREEN)

- **H-7 RED:** today `collect({"country":"VA","path":"/etc/passwd"})` reads `/etc/passwd` and yields
  records (no rejection). `tests/unit/test_geonames_security.py` asserting a raise + no records →
  **RED** today → **GREEN** once `_resolve_confined_path` + the settings exist. The default-deny case
  (no allowlist) is RED for the same reason (today it just reads the file).
- **H-6 RED:** `tests/unit/test_geonames_streaming.py`:
  - The `tracemalloc` peak test on a large synthetic file → **RED** today (current `read_text()` +
    `splitlines()` peak ≈ ≥ 2× file size) → **GREEN** after streaming.
  - The `.content`-raises fake-response test → **RED** today (current `_download` reads
    `response.content`, so the fake raises) → **GREEN** after streaming via `iter_bytes`.
  - `from ... import _iter_zip_lines` / `_stream_to_tempfile` / `_download_lines` → **ImportError**
    today → **GREEN** once the seams exist.

**`tracemalloc` bound (specified precisely so the test-author has no judgement call):** generate a
synthetic local dump of **~16 MiB** of valid TSV lines inside the allowlist. Start `tracemalloc`,
iterate `collect()` discarding every record except a running count (retain nothing), `peak =
tracemalloc.get_traced_memory()[1]`, stop. **Assert `peak < 2 MiB`.** Streaming peaks at roughly one
line + buffers (kilobytes); the old code peaks ≥ ~32 MiB (full string + full list) — an ~16× margin, so
interpreter noise cannot flip the result. (If the harness wants more headroom, scale both: 64 MiB file
/ 4 MiB bound keeps the same ratio.)

---

## 6. Migration conclusion

**NONE.** This gate touches one connector, two settings fields, one schema description, two test files,
and `.env.example`. No table/column/constraint, no `db/models.py` edit, no alembic revision.
`tests/integration/test_migrations.py` (alembic head == create_all, ADR 0030) is **not triggered and
MUST stay green** (FROZEN).

---

## 7. Person-affecting / sign-off assessment

**Connector + security hardening — NOT person-affecting.** GeoNames is a reference **gazetteer**
(places, anchored on `geonames_id`); the connector merges nothing, scores nothing, mutates no ER
threshold, and touches no real-person record. The fix is purely about bounding memory and closing an
arbitrary-file-read. No per-run human sign-off. `human_fork: false` — determinable from CLAUDE.md + the
code; no OPEN architectural question (security hardening with a clear audit fix menu). ADR 0052 is
PROPOSED, no human STOP.

---

## 8. Locked invariants the gate must hold + APPROVE/DENY

- **G1 — provenance on every node AND edge.** PRESERVED **VACUOUSLY** — `collect()` emits raw records
  only; `map()` (which stamps provenance via `stamp(entity, provenance)`) is **unchanged**. **DENY** if
  `map()`, the provenance stamp, or the anchor (`set_anchor(... "geonames_id" ...)`) is touched, or any
  G1 test regresses.
- **Append-only / canonical-canonical only via the guard.** PRESERVED **VACUOUSLY** — no resolver, no
  merge, no threshold, no graph write, no ledger touch. **DENY** if any merge/threshold/guard/writer
  code changes.
- **The gate's OWN positive invariants (load-bearing).** (a) An outside-allowlist / no-allowlist /
  over-size `path` is rejected and yields nothing; an in-allowlist path works. (b) `collect()` is lazy —
  bounded peak RAM, streamed download, no `response.content`. **DENY** if `path` confinement can be
  bypassed (abs / `..` / symlink / default-deny), or if `collect()` re-materializes the whole file, or
  if the download reverts to `response.content`.
- **Negative-space invariant (FROZEN).** Emitted `RawRecord`s + FtM `Address` output byte-identical.
  **DENY** if the happy-path `map()` output changes.

**APPROVE** iff: H-7 rejects all four bypass classes (abs, `..`, symlink, no-allowlist) + the size cap,
and the in-allowlist VA fixture still works; H-6 keeps `tracemalloc` peak under the §5 bound, streams the
download (no `.content`), and iterates the zip member lazily; the FROZEN suites + the byte-identical
`map()` output stay green; no migration / no `map()` change. **DENY** on any confinement bypass, any
re-materialization / `.content` regression, any FROZEN regression, or any change to `map()` /
provenance / anchor.

---

## 9. FROZEN (keep-green) + the one editable-but-sensitive test

A removed assert / added skip|xfail / loosened tolerance in a FROZEN suite is a judge **DENY**
(D-FROZEN). This gate changes only `collect()` internals + settings; it changes no map/merge/score path.

- **`tests/unit/test_geonames_connector.py` — EDITABLE (allowlist wiring ONLY) AND keep-green
  sensitive.** This file's happy path calls `collect({"country":"VA","path":_FIXTURE})` with NO
  allowlist set; under default-deny it would now raise. The builder makes the **minimal** edit: set
  `GEONAMES_ALLOWED_PATH_DIR` to the fixtures dir (env + `get_settings.cache_clear()`) so the path is
  in-allowlist. **The `map()` / anchor / `vatican.get("country") == ["va"]` / "State of the Vatican
  City" / `>= 100` assertions MUST stay byte-identical** — they are the negative-space guard. Touching
  them beyond the allowlist wiring is a DENY. `test_manifest` is untouched.
- `tests/integration/test_migrations.py` — alembic head == create_all (ADR 0030); no migration added.
- The map/anchor suites broadly (FtM Address shape, `geonames_id` anchor, `validate_or_raise`).
- The resolution + sign-off suites (`cluster_and_merge`, `ResolverJudgement`, `SignOff`, H-1/H-2).
- Gate C value-level provenance; the graph-writer suite (G1 projection); the sensitivity-guard suite
  (Gate E / ADR 0047). All PRESERVED VACUOUSLY (this gate touches none of them).

---

## 10. Out of scope (hard stops)

- **NO** change to `map()`, the FtM `Address` output, the `geonames_id` anchor, or the provenance stamp.
- **NO** `wm:Place` ontology extension (a noted future refinement, not this gate).
- **NO** generic plugin-wide path-confinement framework — confine **GeoNames** only (other connectors
  with local-path overrides are separate findings).
- **NO** self-service connector-config API work (this gate hardens the connector that API would expose;
  it does not build the API).
- **NO** removal of `path` from the schema (it is load-bearing for the fixture + offline dev); the
  schema change is doc-only.
- **NO** change to `country` handling (confirmed safe via `^[A-Za-z]{2}$` + jsonschema-before-download).
- **NO** driver `mem_limit` / compose / supervision work (B-4c done) — this gate fixes the *cause* of
  the OOM, not the container bound.
- **NO** migration; **NO** `db/models.py` edit; **NO** new CI job.

---

## 11. Slice plan

Two independent, individually-mergeable slices, each failing-test-first, both no-Docker (pure unit).
Both touch `connector.py`'s `collect()` and `settings.py`, so they are **merge-ordered** (Slice 1 then
Slice 2); each is independently green and reviewable. New tests go in **two distinct new files** so the
slices do not conflict on a test file.

- **Slice 1 — H-7 path confinement (the security boundary).**
  - `src/worldmonitor/settings.py` — `geonames_allowed_path_dir`, `geonames_max_path_bytes`.
  - `src/worldmonitor/plugins/connectors/geonames/connector.py` — `_resolve_confined_path()` +
    `GeoNamesPathError`; wire it into `collect()`'s local-path branch (still using the existing
    `read_text` read for now — streaming is Slice 2).
  - `src/worldmonitor/plugins/connectors/geonames/config.schema.json` — doc-only `path` description.
  - `.env.example` — the two new vars + rationale.
  - `tests/unit/test_geonames_security.py` (NEW) — the H-7 RED cases.
  - `tests/unit/test_geonames_connector.py` — minimal allowlist wiring (§9).
  - **RED:** today `path:/etc/passwd` yields records / no-allowlist reads the file. Mergeable alone.

- **Slice 2 — H-6 bounded streaming (the memory boundary).** Builds on Slice 1's confined `collect()`.
  - `src/worldmonitor/plugins/connectors/geonames/connector.py` — `_iter_local_lines`,
    `_stream_to_tempfile`, `_iter_zip_lines`, `_download_lines`; replace `read_text().splitlines()` with
    lazy iteration; replace `_download`'s `response.content` with streaming; preserve byte-identity
    (`rstrip("\n")`, blank-skip).
  - `tests/unit/test_geonames_streaming.py` (NEW) — the `tracemalloc` bound, the `.content`-raises fake
    response, the lazy-iterator + `inspect.isgenerator` checks, the small-zip fixture (built in the test
    or committed under `tests/fixtures/geonames/`).
  - **RED:** `tracemalloc` peak over bound / `.content` accessed / seam ImportError. Mergeable after
    Slice 1.

`human_fork: false`. No OPEN architectural question — security + memory hardening with a clear audit fix
menu; the one genuine design choice (default-deny allowlist + runtime realpath confinement + size cap;
stream-to-tempfile + lazy zip member) is decided in ADR 0052 with rejected alternatives recorded. Not a
product fork → PROPOSED, no human STOP.

---

## 12. Verdict

**Build Slice 1 → Slice 2.** The gate closes both HIGH findings in one connector with the smallest
correct footprint: a fail-closed, default-deny, realpath-confined + size-capped local `path` (H-7) and a
fully lazy `collect()` that streams the download to disk and iterates the zip member line-by-line (H-6),
bounding peak RAM to ~one window. `map()`, provenance, the anchor, and the emitted-record bytes are
unchanged (the negative-space FROZEN guard). No migration, no store mutation, no merge/score/guard
touch — the locked invariants hold vacuously and the new positive invariants (no LFI, bounded memory)
are the APPROVE bar. Not a product fork; ADR 0052 is PROPOSED, no human STOP.
