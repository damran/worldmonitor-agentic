"""TelegramNotifier — the first :class:`Notifier`, delivering alerts via the Telegram Bot API.

Telegram's ``sendMessage`` endpoint accepts a GET with query params
(``https://api.telegram.org/bot<bot_token>/sendMessage?chat_id=...&text=...``), so a
:class:`Notification` is rendered to text and delivered over the SSRF guard
(:func:`worldmonitor.net.ssrf.guarded_stream`) exactly like a connector's fetch — never a bare
``httpx`` call to an attacker-influenced host.

Secret hygiene: the ``bot_token`` rides in the URL *path*. httpx's INFO request-URL logging is
suppressed at the egress chokepoint (``net/ssrf.py::_quiet_http_request_logging``, ADR 0065), and
this notifier additionally logs only the ``chat_id`` + outcome — NEVER the token or the URL.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import Mapping
from importlib import resources
from typing import Any

import httpx

from worldmonitor.net.ssrf import guarded_stream
from worldmonitor.plugins.base import Kind, Manifest, Notification, Notifier, Status

logger = logging.getLogger(__name__)

_SEND_MESSAGE_BASE = "https://api.telegram.org"
_HTTP_TIMEOUT = 30.0
# Telegram's sendMessage response is a small JSON envelope; cap the read so a hostile/oversized body
# is refused (fail-closed) rather than streamed unbounded into memory.
_MAX_RESPONSE_BYTES = 64 * 1024


class TelegramNotifier(Notifier):
    """Delivers a :class:`Notification` to a Telegram chat via the Bot sendMessage API."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """Store an optional injected ``transport`` (``httpx.MockTransport`` in tests).

        Production instantiation passes no transport (real HTTP via ``guarded_stream``); tests
        inject an ``httpx.MockTransport`` so no live network call is ever made.
        """
        self._transport = transport

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="telegram",
            name="Telegram",
            version="0.1.0",
            kind=Kind.NOTIFIER,
            mode=None,
            capability=None,
            description="Delivers alerts to a Telegram chat via the Bot sendMessage API.",
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def send(self, config: Mapping[str, Any], notification: Notification) -> None:
        """Deliver ``notification`` to the configured chat via Telegram ``sendMessage``.

        Validates the config, renders the notification to a single text body, and issues a GET to
        ``/bot<token>/sendMessage`` (chat_id + text [+ parse_mode] as query params) through the SSRF
        guard. ``raise_for_status`` surfaces a Telegram error (4xx/5xx) loudly; the small response
        is read under a byte cap. Logs only the ``chat_id`` — never the token or the URL.
        """
        self.validate_config(config)
        bot_token = config["bot_token"]
        chat_id = config["chat_id"]
        text = _render(notification)

        params: dict[str, str] = {"chat_id": chat_id, "text": text}
        parse_mode = config.get("parse_mode")
        if parse_mode:
            params["parse_mode"] = parse_mode
        url = f"{_SEND_MESSAGE_BASE}/bot{bot_token}/sendMessage?{urllib.parse.urlencode(params)}"

        with guarded_stream(
            "GET", url, timeout=_HTTP_TIMEOUT, transport=self._transport
        ) as response:
            response.raise_for_status()
            self._drain_bounded(response)

        # chat_id is not a secret; the token + the URL are never logged.
        logger.info("sent telegram notification to chat %s", chat_id)

    @staticmethod
    def _drain_bounded(response: httpx.Response) -> None:
        """Consume the streaming body under the byte cap (fail-closed on overflow)."""
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"telegram response body exceeded the {_MAX_RESPONSE_BYTES}-byte cap "
                    "(fail-closed)"
                )


def _render(notification: Notification) -> str:
    """Render a :class:`Notification` to Telegram message text (title + body, severity-prefixed).

    The default ``info`` severity renders bare title/body; any other severity (warning/critical) is
    prefixed (e.g. ``"[CRITICAL] ..."``) so the channel surfaces it.
    """
    prefix = "" if notification.severity == "info" else f"[{notification.severity.upper()}] "
    return f"{prefix}{notification.title}\n\n{notification.body}"
