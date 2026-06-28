"""Primary invariant tests (RED) for TelegramNotifier — ADR 0067.

These pin the contract for ``src/worldmonitor/plugins/notifiers/telegram/`` (the first ``Notifier``
instance), mirroring the OpenCorporates connector's hermetic-HTTP test patterns
(``tests/unit/test_opencorporates_connector.py``):

* MANIFEST: ``connector_id="telegram"``, ``kind=NOTIFIER``, ``mode=None``, ``capability=None``,
  ``status=IMPLEMENTED``.
* CONFIG SCHEMA: ``bot_token`` is a SECRET (``"secret": true``); ``required`` is exactly
  ``{"bot_token","chat_id"}``; ``additionalProperties: false``; optional ``parse_mode`` enum
  ``["MarkdownV2","HTML"]``. ``validate_config`` rejects a config missing bot_token or chat_id
  (and a bad parse_mode).
* ``send()``: over ``httpx.MockTransport`` (NO live HTTP) issues the Telegram
  ``https://api.telegram.org/bot<bot_token>/sendMessage`` request carrying ``chat_id`` + a ``text``
  param that renders both the notification title and body; raises on a Telegram error response
  (``raise_for_status`` -> ``httpx.HTTPStatusError``); fetches ONLY via ``net.ssrf.guarded_stream``
  (a private-resolving host is ``BlockedAddressError`` BEFORE any request leaves).
* SECRET: the ``bot_token`` (which rides in the URL path) is NEVER written to ANY logger — not the
  notifier's own tree AND not ``httpx`` (whose INFO request-URL line is suppressed at the egress
  chokepoint, ``net/ssrf.py``). Captured across ALL loggers, like the OpenCorporates token test.

RED today: ``worldmonitor.plugins.notifiers.telegram`` does not exist (nor ``Notification`` on
``base``), so the top-level import raises ``ModuleNotFoundError`` / ``ImportError`` and the whole
module errors at collection (the correct RED). GREEN once the builder lands TelegramNotifier.

No live network: ``httpx.MockTransport`` is injected through the notifier ``transport=`` ctor kwarg
(forwarded to ``guarded_stream``), and ``socket.getaddrinfo`` is monkeypatched to a chosen IP so the
SSRF host check runs with no real DNS — the pattern from ``tests/unit/test_ssrf_guard.py``.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable

import httpx
import jsonschema
import pytest

from worldmonitor.net.ssrf import BlockedAddressError
from worldmonitor.plugins.base import Kind, Notification, Status

# Top-level import of the not-yet-built notifier — ModuleNotFoundError today (correct RED).
from worldmonitor.plugins.notifiers.telegram import TelegramNotifier


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    """A ``getaddrinfo`` stand-in resolving EVERY host to ``ip`` (one IPv4 5-tuple), no real DNS."""

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


# --------------------------------------------------------------------------------------------------
# Manifest — a notifier, not a connector
# --------------------------------------------------------------------------------------------------


def test_manifest_is_notifier() -> None:
    """The Telegram plugin is a NOTIFIER with no connector Mode / Capability."""
    manifest = TelegramNotifier().manifest
    assert manifest.connector_id == "telegram"
    assert manifest.name == "Telegram"
    assert manifest.version == "0.1.0"
    assert manifest.kind is Kind.NOTIFIER
    assert manifest.mode is None
    assert manifest.capability is None
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# Config schema — secret bot_token + required chat_id + closed schema + parse_mode enum
# --------------------------------------------------------------------------------------------------


def test_config_schema_marks_bot_token_secret_and_requires_chat_id() -> None:
    """bot_token is a UI secret; bot_token + chat_id are required; the schema is closed + enum."""
    notifier = TelegramNotifier()
    schema = notifier.config_schema
    props = schema["properties"]

    # bot_token is a SECRET field (drives the UI password input + vault encryption at rest).
    assert props["bot_token"].get("secret") is True
    assert props["bot_token"]["type"] == "string"
    assert props["chat_id"]["type"] == "string"

    # Exactly bot_token + chat_id are required; the schema is closed; parse_mode is the enum option.
    assert set(schema["required"]) == {"bot_token", "chat_id"}
    assert schema["additionalProperties"] is False
    assert props["parse_mode"]["enum"] == ["MarkdownV2", "HTML"]

    # validate_config: a complete config passes (with or without the optional parse_mode).
    notifier.validate_config({"bot_token": "T", "chat_id": "42"})
    notifier.validate_config({"bot_token": "T", "chat_id": "42", "parse_mode": "HTML"})

    # Rejects a config missing either required secret/target, or a bad parse_mode.
    with pytest.raises(jsonschema.ValidationError):
        notifier.validate_config({"chat_id": "42"})  # bot_token missing
    with pytest.raises(jsonschema.ValidationError):
        notifier.validate_config({"bot_token": "T"})  # chat_id missing
    with pytest.raises(jsonschema.ValidationError):
        notifier.validate_config({"bot_token": "T", "chat_id": "42", "parse_mode": "PLAIN"})


# --------------------------------------------------------------------------------------------------
# send() — the Telegram sendMessage request shape (chat_id + rendered text)
# --------------------------------------------------------------------------------------------------


def test_send_issues_telegram_sendmessage_with_chat_id_and_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send() issues GET api.telegram.org/bot<token>/sendMessage with chat_id + a rendered text."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    notifier = TelegramNotifier(transport=httpx.MockTransport(_handler))
    notifier.send(
        {"bot_token": "BOTSECRET123", "chat_id": "42"},
        Notification(title="Alert", body="rule X fired"),
    )

    assert len(calls) == 1, "send() must issue exactly one sendMessage request"
    request = calls[0]
    assert request.method == "GET"
    # The Telegram URL shape: api.telegram.org / bot<token> / sendMessage.
    assert request.url.host == "api.telegram.org"
    assert "/bot" in request.url.path
    assert request.url.path.endswith("/sendMessage")
    # chat_id + the rendered message text ride as query params.
    assert request.url.params.get("chat_id") == "42"
    text = request.url.params.get("text")
    assert text is not None
    assert "Alert" in text  # title rendered
    assert "rule X fired" in text  # body rendered


def test_send_raises_on_telegram_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Telegram error response (4xx) makes send() raise via raise_for_status (no silent fail)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "bad chat"})

    notifier = TelegramNotifier(transport=httpx.MockTransport(_handler))
    with pytest.raises(httpx.HTTPStatusError):
        notifier.send({"bot_token": "T", "chat_id": "99"}, Notification(title="T", body="B"))


# --------------------------------------------------------------------------------------------------
# SSRF — every fetch goes through guarded_stream; a private-resolving host is blocked
# --------------------------------------------------------------------------------------------------


def test_send_uses_guarded_stream_and_blocks_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetch goes through net.ssrf.guarded_stream: a host resolving to a private address is
    refused BEFORE any request reaches the transport (a bare-httpx notifier would NOT block)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("10.0.0.1"))
    calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(transport=httpx.MockTransport(_handler))
    with pytest.raises(BlockedAddressError):
        notifier.send({"bot_token": "T", "chat_id": "42"}, Notification(title="T", body="B"))

    assert calls == [], "send() connected to a blocked host — the SSRF guard was bypassed"


# --------------------------------------------------------------------------------------------------
# SECRET — the bot_token (in the URL path) is never logged, by ANY logger
# --------------------------------------------------------------------------------------------------


def test_send_does_not_log_bot_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bot_token (which rides in the request URL path) is NEVER written to ANY logger.

    Mirrors the OpenCorporates token test: ``caplog.set_level(logging.INFO)`` (no ``logger=``)
    raises the ROOT logger + caplog handler to INFO and captures PROPAGATED records from EVERY
    logger — including ``httpx``, whose ``"HTTP Request: GET <url> ..."`` INFO line carries the
    token-bearing URL. The egress suppression (``net/ssrf.py::_quiet_http_request_logging``) plus
    the notifier's own redaction must hold: the token value appears in NO captured record.
    """
    token = "BOTSECRET123"
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    notifier = TelegramNotifier(transport=httpx.MockTransport(_handler))

    caplog.set_level(logging.INFO)
    notifier.send(
        {"bot_token": token, "chat_id": "42"},
        Notification(title="Alert", body="rule X fired"),
    )

    # Guard against a vacuous pass: send() must have actually issued the request.
    assert calls, "send() never issued a request — the no-leak assertion would be vacuous"

    # The secret must appear in NONE of the captured output, across ALL loggers — the aggregated
    # caplog.text AND each record's formatted message AND its raw %-args (so a token embedded in
    # httpx's request line, supplied via %-args, is caught).
    assert token not in caplog.text, (
        "bot_token leaked into the aggregated log text "
        "(the httpx request-URL INFO line carries the token-bearing path)"
    )
    leaked = [rec for rec in caplog.records if token in rec.getMessage() or token in str(rec.args)]
    assert not leaked, "bot_token leaked into logs via " + "; ".join(
        f"{r.name}[{r.levelname}]: {r.getMessage()}" for r in leaked
    )
