"""Gate F-6 slice 1 — the thin read-only ``wm`` CLI (``health`` / ``ready`` / ``entity``).

This is the FAILING oracle written before ``src/worldmonitor/cli.py`` exists (per
``docs/reviews/GATE_F6_WM_CLI_SPEC.md`` §4 and ADR 0127). It pins:

* the exit-code contract (0/1/2/3) per command, table in spec §3.3,
* the ``ready`` 503-body-on-stdout / ``checks.driver`` non-fatal pin (ADR 0059, spec §3.2),
* provenance pass-through verbatim on ``entity`` (AC-6, G1 non-violation),
* the security-load-bearing invariants: the bearer is env-only (no ``--token`` flag, AC-17),
  it never leaks into stdout/stderr (AC-10), and every command is GET-only (AC-18),
* config plumbing: ``WM_BASE_URL``/``--base-url``, ``WM_TOKEN``, ``WM_TIMEOUT``/``--timeout``.

Every test drives the public entry point ``worldmonitor.cli.main(argv)`` — never a private
helper — with an ``httpx.MockTransport`` injected so there is no live server and no network
(mirrors the connectors' ``transport=`` injection pattern, e.g.
``tests/unit/test_feodo_connector.py``). Per the spec's transport-injection design (§3.6),
``build_client(*, base_url, token, timeout, transport=None)`` is the injection seam; these
tests monkeypatch ``worldmonitor.cli.build_client`` with a thin wrapper that forwards to the
REAL ``build_client`` (so its header/base_url/timeout wiring is genuinely exercised) while
forcing the transport to the test's ``httpx.MockTransport`` in place of a live network
transport. This is a testability seam substitution, not a re-implementation of the logic
under test.

RED reason (today): ``worldmonitor.cli`` does not exist yet -> the module-level import below
raises ``ModuleNotFoundError``, which pytest reports as a collection error for every test in
this file. That is red for the right reason: the gate is "the module + its contract don't
exist yet," not a weaker assertion the builder could satisfy by accident.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from worldmonitor import cli

# A short, non-secret-scanner-shaped test token (kept well under 16 chars; see
# .claude/hooks/secret-scan.sh — it only fires on TOKEN[:=]<16+ char blob>, and this repo's
# tests never hold a real credential regardless).
TEST_TOKEN = "tkn-abc"

_HEALTH_BODY = {"status": "ok", "environment": "test"}
_READY_OK_BODY = {
    "ready": True,
    "checks": {"postgres": "ok", "neo4j": "ok", "minio": "ok", "driver": "ok"},
}
_ENTITY_BODY = {
    "id": "NRC-abc123",
    "schema": "Person",
    "properties": {"name": ["Jane Doe"]},
    "prov_source_id": "src:test",
    "prov_source_record": "s3://landing/test/a.json",
    "prov_retrieved_at": "2026-06-21T00:00:00Z",
    "prov_reliability": "A",
}


@pytest.fixture(autouse=True)
def _clean_wm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may depend on the ambient environment or a ``.env`` file."""
    for var in ("WM_BASE_URL", "WM_TOKEN", "WM_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)


def _patch_build_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[list[httpx.Request], dict[str, object]]:
    """Route ``main()``'s client construction through an injected ``MockTransport``.

    Returns ``(calls, captured)`` where ``calls`` records every ``httpx.Request`` the mock
    transport actually saw (proving GET-only / header / no-request-made assertions) and
    ``captured`` records the most recent ``build_client(base_url=, token=, timeout=)`` call
    plus the real ``httpx.Client`` it produced (proving argv/env plumbing reaches the real
    factory, not a re-implementation).
    """
    calls: list[httpx.Request] = []

    def _recording_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    mock_transport = httpx.MockTransport(_recording_handler)
    real_build_client = cli.build_client
    captured: dict[str, object] = {}

    def _fake_build_client(
        *,
        base_url: str,
        token: str | None,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> httpx.Client:
        del transport  # production code never passes one; the test always overrides it
        client = real_build_client(
            base_url=base_url, token=token, timeout=timeout, transport=mock_transport
        )
        captured["base_url"] = base_url
        captured["token"] = token
        captured["timeout"] = timeout
        captured["client"] = client
        return client

    monkeypatch.setattr(cli, "build_client", _fake_build_client)
    return calls, captured


def _json_response(status_code: int, body: dict[str, object]) -> httpx.Response:
    return httpx.Response(status_code, json=body)


def _raising_handler(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return _handler


# ---------------------------------------------------------------------------------------
# AC-1/AC-2 -- health
# ---------------------------------------------------------------------------------------


def test_health_success_exit0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-1: 200 -> exit 0, body on stdout."""
    calls, _ = _patch_build_client(monkeypatch, lambda request: _json_response(200, _HEALTH_BODY))

    code = cli.main(["health"])

    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == _HEALTH_BODY
    assert captured.out.endswith("\n")
    assert captured.err == ""
    assert len(calls) == 1


def test_health_no_token_required(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-2: WM_TOKEN unset -> health still succeeds (public endpoint)."""
    calls, _ = _patch_build_client(monkeypatch, lambda request: _json_response(200, _HEALTH_BODY))
    # WM_TOKEN is deliberately absent (see _clean_wm_env autouse fixture).

    code = cli.main(["health"])

    assert code == 0
    assert "authorization" not in {h.lower() for h in calls[0].headers}
    capsys.readouterr()


def test_health_non200_exit1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """health non-200 -> exit 1, message on stderr (spec §3.2)."""
    _patch_build_client(monkeypatch, lambda request: _json_response(500, {"detail": "boom"}))

    code = cli.main(["health"])

    assert code == 1
    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------------------
# AC-3/AC-4/AC-5 -- ready (ADR 0059 pin: report the status, never reinterpret ``checks``)
# ---------------------------------------------------------------------------------------


def test_ready_ready_exit0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-3: 200 -> exit 0, body (incl. checks) on stdout."""
    _patch_build_client(monkeypatch, lambda request: _json_response(200, _READY_OK_BODY))

    code = cli.main(["ready"])

    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == _READY_OK_BODY
    assert captured.err == ""


def test_ready_notready_503_exit1_body_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-4: 503 -> exit 1, but the body still lands on STDOUT verbatim (not stderr).

    This is the pin: the CLI reports the endpoint's structured readiness verdict either way
    (ADR 0059) -- a 503 is not folded into the generic "error to stderr" path.
    """
    body = {
        "ready": False,
        "checks": {"postgres": "down", "neo4j": "ok", "minio": "ok", "driver": "unknown"},
    }
    _patch_build_client(monkeypatch, lambda request: _json_response(503, body))

    code = cli.main(["ready"])

    assert code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == body
    assert captured.err == ""


def test_ready_503_exit1_regardless_of_checks_contents(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Extra pin (spec §3.2/ADR 0059): the exit code comes SOLELY from the HTTP status.

    Every individual check reads "ok" here, yet the endpoint still returned 503 overall
    (e.g. a store flapped between its own internal check and the response). The CLI must
    NOT recompute readiness from ``checks`` -- it must still exit 1 because the HTTP status
    was 503, never re-deriving success from the (misleadingly all-"ok") checks dict.
    """
    body = {
        "ready": False,
        "checks": {"postgres": "ok", "neo4j": "ok", "minio": "ok", "driver": "ok"},
    }
    _patch_build_client(monkeypatch, lambda request: _json_response(503, body))

    code = cli.main(["ready"])

    assert code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == body


def test_ready_driver_stale_still_exit0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-5: 200 with checks.driver == "stale" -> still exit 0; driver passed through."""
    body = {
        "ready": True,
        "checks": {"postgres": "ok", "neo4j": "ok", "minio": "ok", "driver": "stale"},
    }
    _patch_build_client(monkeypatch, lambda request: _json_response(200, body))

    code = cli.main(["ready"])

    assert code == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["checks"]["driver"] == "stale"
    assert parsed == body


def test_ready_other_status_exit1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ready with neither 200 nor 503 -> generic API-error path: exit 1, stderr message."""
    _patch_build_client(monkeypatch, lambda request: _json_response(500, {"detail": "unexpected"}))

    code = cli.main(["ready"])

    assert code == 1
    captured = capsys.readouterr()
    assert "unexpected" in captured.err


# ---------------------------------------------------------------------------------------
# AC-6..AC-10 -- entity (the auth-gated command)
# ---------------------------------------------------------------------------------------


def test_entity_success_preserves_provenance_exit0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-6: 200 -> exit 0; every prov_* key survives verbatim on stdout (G1 non-violation)."""
    monkeypatch.setenv("WM_TOKEN", TEST_TOKEN)
    _patch_build_client(monkeypatch, lambda request: _json_response(200, _ENTITY_BODY))

    code = cli.main(["entity", "NRC-abc123"])

    assert code == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["prov_source_id"] == "src:test"
    assert parsed["prov_source_record"] == "s3://landing/test/a.json"
    assert parsed["prov_retrieved_at"] == "2026-06-21T00:00:00Z"
    assert parsed["prov_reliability"] == "A"
    assert parsed == _ENTITY_BODY


def test_entity_sends_bearer_header(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-7: entity attaches Authorization: Bearer <WM_TOKEN>."""
    monkeypatch.setenv("WM_TOKEN", TEST_TOKEN)
    calls, _ = _patch_build_client(monkeypatch, lambda request: _json_response(200, _ENTITY_BODY))

    code = cli.main(["entity", "NRC-abc123"])

    assert code == 0
    assert len(calls) == 1
    assert calls[0].headers["authorization"] == f"Bearer {TEST_TOKEN}"
    capsys.readouterr()


def test_entity_missing_token_exit2_no_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-8: WM_TOKEN unset -> exit 2, stderr message, and NO request is made."""
    calls, _ = _patch_build_client(monkeypatch, lambda request: _json_response(200, _ENTITY_BODY))
    # WM_TOKEN deliberately absent.

    code = cli.main(["entity", "NRC-abc123"])

    assert code == 2
    captured = capsys.readouterr()
    assert "WM_TOKEN is required for 'entity'" in captured.err
    assert captured.out == ""
    assert calls == []


def test_entity_404_exit1_detail_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-9: 404 -> exit 1, detail on stderr (no dedicated not-found exit code)."""
    monkeypatch.setenv("WM_TOKEN", TEST_TOKEN)
    _patch_build_client(
        monkeypatch, lambda request: _json_response(404, {"detail": "Entity not found"})
    )

    code = cli.main(["entity", "missing-id"])

    assert code == 1
    captured = capsys.readouterr()
    assert "Entity not found" in captured.err
    assert captured.out == ""
    assert "Traceback" not in captured.err


def test_entity_401_never_leaks_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-10: 401 -> exit 1; the token string appears in NEITHER stdout NOR stderr.

    Security pin -- do NOT soften. Even though the CLI genuinely sent
    ``Authorization: Bearer <TEST_TOKEN>`` on the wire (asserted separately), it must never
    echo that value back to the operator's terminal/logs on an auth failure.
    """
    monkeypatch.setenv("WM_TOKEN", TEST_TOKEN)
    calls, _ = _patch_build_client(
        monkeypatch, lambda request: _json_response(401, {"detail": "Not authenticated"})
    )

    code = cli.main(["entity", "NRC-abc123"])

    assert code == 1
    # Sanity: the request genuinely carried the real bearer (proves this is a meaningful
    # negative-space check, not a vacuous one -- the token really was in play).
    assert calls[0].headers["authorization"] == f"Bearer {TEST_TOKEN}"
    captured = capsys.readouterr()
    assert TEST_TOKEN not in captured.out
    assert TEST_TOKEN not in captured.err
    assert f"Bearer {TEST_TOKEN}" not in captured.out
    assert f"Bearer {TEST_TOKEN}" not in captured.err


def test_entity_5xx_exit1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """entity 5xx (not just 4xx) also folds into exit 1 with detail surfaced."""
    monkeypatch.setenv("WM_TOKEN", TEST_TOKEN)
    _patch_build_client(
        monkeypatch, lambda request: _json_response(500, {"detail": "internal error"})
    )

    code = cli.main(["entity", "NRC-abc123"])

    assert code == 1
    captured = capsys.readouterr()
    assert "internal error" in captured.err


# ---------------------------------------------------------------------------------------
# AC-11/AC-12 -- transport failures
# ---------------------------------------------------------------------------------------


def test_connection_error_exit3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-11: connection failure -> exit 3, no traceback."""
    _patch_build_client(monkeypatch, _raising_handler(httpx.ConnectError("connection refused")))

    code = cli.main(["health"])

    assert code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert captured.err != ""


def test_timeout_exit3(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """AC-12: timeout -> exit 3."""
    monkeypatch.setenv("WM_TOKEN", TEST_TOKEN)
    _patch_build_client(monkeypatch, _raising_handler(httpx.TimeoutException("timed out")))

    code = cli.main(["entity", "NRC-abc123"])

    assert code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------------------
# AC-13/AC-14 -- usage errors
# ---------------------------------------------------------------------------------------


def test_unknown_command_exit2(capsys: pytest.CaptureFixture[str]) -> None:
    """AC-13: unknown subcommand -> exit 2. ``main()`` RETURNS 2, never raises SystemExit
    (the pinned signature is ``main(argv=None) -> int``; the console-script wrapper relies on
    ``sys.exit(main())``, which only works if ``main`` truly returns)."""
    code = cli.main(["bogus-command"])

    assert code == 2
    capsys.readouterr()


def test_no_subcommand_exit2(capsys: pytest.CaptureFixture[str]) -> None:
    """AC-14: no subcommand at all -> exit 2 (usage)."""
    code = cli.main([])

    assert code == 2
    capsys.readouterr()


def test_help_flag_exit0(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit-code totality: --help exits 0 and prints usage (not an error)."""
    code = cli.main(["--help"])

    assert code == 0
    captured = capsys.readouterr()
    assert "health" in captured.out
    assert "ready" in captured.out
    assert "entity" in captured.out


# ---------------------------------------------------------------------------------------
# AC-15/AC-16 -- config plumbing (env + flag overrides)
# ---------------------------------------------------------------------------------------


def test_base_url_flag_overrides_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-15: --base-url overrides WM_BASE_URL for one invocation."""
    monkeypatch.setenv("WM_BASE_URL", "http://env-should-be-overridden:1111")
    calls, captured_kwargs = _patch_build_client(
        monkeypatch, lambda request: _json_response(200, _HEALTH_BODY)
    )

    code = cli.main(["health", "--base-url", "http://example.test:9000"])

    assert code == 0
    assert captured_kwargs["base_url"] == "http://example.test:9000"
    assert str(calls[0].url) == "http://example.test:9000/health"
    capsys.readouterr()


def test_base_url_env_var_used_when_no_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """WM_BASE_URL (env, no flag) is honored."""
    monkeypatch.setenv("WM_BASE_URL", "http://from-env.test:7000")
    calls, _ = _patch_build_client(monkeypatch, lambda request: _json_response(200, _HEALTH_BODY))

    code = cli.main(["health"])

    assert code == 0
    assert str(calls[0].url) == "http://from-env.test:7000/health"
    capsys.readouterr()


def test_default_base_url_when_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither WM_BASE_URL nor --base-url set -> defaults to http://localhost:8000 (V6/spec 3.1)."""
    calls, captured_kwargs = _patch_build_client(
        monkeypatch, lambda request: _json_response(200, _HEALTH_BODY)
    )

    code = cli.main(["health"])

    assert code == 0
    assert captured_kwargs["base_url"] == "http://localhost:8000"
    assert str(calls[0].url) == "http://localhost:8000/health"
    capsys.readouterr()


def test_timeout_flag_applied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-16: --timeout is parsed and reaches the client factory / the real httpx.Client."""
    _, captured_kwargs = _patch_build_client(
        monkeypatch, lambda request: _json_response(200, _HEALTH_BODY)
    )

    code = cli.main(["health", "--timeout", "5"])

    assert code == 0
    assert captured_kwargs["timeout"] == pytest.approx(5.0)
    client = captured_kwargs["client"]
    assert isinstance(client, httpx.Client)
    assert client.timeout == httpx.Timeout(5.0)
    capsys.readouterr()


def test_timeout_env_var_applied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """WM_TIMEOUT (env, no flag) reaches the client factory."""
    monkeypatch.setenv("WM_TIMEOUT", "2.5")
    _, captured_kwargs = _patch_build_client(
        monkeypatch, lambda request: _json_response(200, _HEALTH_BODY)
    )

    code = cli.main(["health"])

    assert code == 0
    assert captured_kwargs["timeout"] == pytest.approx(2.5)
    capsys.readouterr()


def test_timeout_default_is_10_when_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither WM_TIMEOUT nor --timeout set -> defaults to 10 seconds (spec §3.1)."""
    _, captured_kwargs = _patch_build_client(
        monkeypatch, lambda request: _json_response(200, _HEALTH_BODY)
    )

    code = cli.main(["health"])

    assert code == 0
    assert captured_kwargs["timeout"] == pytest.approx(10.0)
    capsys.readouterr()


# ---------------------------------------------------------------------------------------
# AC-17 -- no --token flag exists (security pin, do NOT soften)
# ---------------------------------------------------------------------------------------


def test_token_flag_rejected_env_only(capsys: pytest.CaptureFixture[str]) -> None:
    """AC-17: --token is not a recognized flag anywhere -- the bearer is env-only.

    A token on argv would leak into shell history / `ps`; this is a security pin, not a
    style choice. ``main()`` must return 2 (usage error), never raise SystemExit past its own
    boundary, and must NOT make any request with the argv-supplied value.
    """
    code = cli.main(["entity", "NRC-abc123", "--token", "should-be-rejected"])

    assert code == 2
    captured = capsys.readouterr()
    assert "should-be-rejected" not in captured.out
    assert "should-be-rejected" not in captured.err


def test_token_flag_rejected_on_health_too(capsys: pytest.CaptureFixture[str]) -> None:
    """--token is unrecognized on every subcommand, not just entity."""
    code = cli.main(["health", "--token", "x"])

    assert code == 2
    capsys.readouterr()


# ---------------------------------------------------------------------------------------
# AC-18 -- GET-only across every command (append-only / read-only-by-construction pin)
# ---------------------------------------------------------------------------------------


def test_all_commands_are_get_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-18: health/ready/entity each issue a GET and nothing else.

    The handler itself asserts the verb (fail loudly, not just "assert True" after the
    fact) -- a non-GET request raises inside the transport before a response is even formed.
    """
    monkeypatch.setenv("WM_TOKEN", TEST_TOKEN)

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", f"non-GET request: {request.method} {request.url}"
        if request.url.path == "/health":
            return _json_response(200, _HEALTH_BODY)
        if request.url.path == "/ready":
            return _json_response(200, _READY_OK_BODY)
        if request.url.path.startswith("/entities/"):
            return _json_response(200, _ENTITY_BODY)
        raise AssertionError(f"unexpected path: {request.url.path}")

    calls, _ = _patch_build_client(monkeypatch, _handler)

    assert cli.main(["health"]) == 0
    assert cli.main(["ready"]) == 0
    assert cli.main(["entity", "NRC-abc123"]) == 0

    assert len(calls) == 3
    assert all(c.method == "GET" for c in calls)
    capsys.readouterr()


# ---------------------------------------------------------------------------------------
# Checker LOW -- entity id path-traversal cannot escape the /entities/ route
# ---------------------------------------------------------------------------------------


def test_entity_path_traversal_id_stays_under_entities_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An id shaped like ``../ready`` must never route-escape ``/entities/`` to ``/ready``.

    Without percent-encoding the id segment, httpx RFC-3986-normalizes
    ``/entities/../ready`` down to ``/ready`` client-side *before* the request ever leaves the
    process -- so ``wm entity '../ready'`` would silently GET the ready endpoint and exit 0 on
    a non-entity body. The fix (``quote(args.entity_id, safe="")``) turns the literal ``/`` in
    the id into ``%2F`` so there is no path separator left for httpx to collapse; the request
    that actually goes out on the wire targets ``/entities/..%2Fready`` -- a single (bogus)
    segment under the entities route, never ``/ready`` itself.

    This pins the fix at the wire level (``str(request.url)``), not just the decoded display
    form of ``.path`` (which httpx re-decodes ``%2F`` back to ``/`` for readability and would
    misleadingly still show ``..`` -- that decoded view is irrelevant to what was actually
    requested).
    """
    monkeypatch.setenv("WM_TOKEN", TEST_TOKEN)

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/entities/"), (
            f"path escaped the /entities/ route: {request.url.path}"
        )
        if str(request.url) == "http://localhost:8000/entities/NRC-abc123":
            return _json_response(200, _ENTITY_BODY)
        return _json_response(404, {"detail": "Entity not found"})

    calls, _ = _patch_build_client(monkeypatch, _handler)

    code = cli.main(["entity", "../ready"])

    assert code == 1
    assert len(calls) == 1
    # The definitive wire-level pin: the actual request target, never simply "/ready".
    assert str(calls[0].url) == "http://localhost:8000/entities/..%2Fready"
    assert calls[0].url.path != "/ready"
    assert calls[0].url.path.startswith("/entities/")
    captured = capsys.readouterr()
    assert captured.out == ""
