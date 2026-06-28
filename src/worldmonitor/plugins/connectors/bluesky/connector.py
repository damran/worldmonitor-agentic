"""BlueskyConnector — the Bluesky Jetstream firehose mapped to FtM ``Article`` (ADR 0070).

The platform's first real-time ``Mode.STREAM`` source: a ``PASSIVE`` / ``EXTERNAL_IMPORT`` connector
that consumes the **public** Bluesky Jetstream WebSocket firehose
(``wss://jetstream2.us-east.bsky.network/subscribe``; no API key) one bounded window at a time and
maps each ``app.bsky.feed.post`` create commit to an FtM-native ``Article`` (mirroring the Feed
connector). It is a one-way PULL into OUR landing zone + ER queue — it never pushes/routes our data
anywhere, never resolves, and never writes the graph.

Safety (the locked invariants):

* **Bounded** — ``collect()`` (inherited from :class:`StreamConnector`) self-bounds the window by
  ``window_seconds`` / ``max_events``; ``run_ingest``'s ``timeout`` / ``max_records`` remain a
  backstop, and the WS reader carries a per-event byte cap.
* **Hostile input** — each event is hostile bytes in ``RawRecord.data``; ``map()`` validates via FtM
  (``validate_or_raise``) and fail-soft-skips a non-post / malformed event (it never raises).
* **SSRF defense-in-depth** — the (operator-configured) Jetstream host is checked with
  :func:`assert_public_host` before the socket opens.
"""

# websockets ships type information but its async client surface is narrowed at the one call site
# below; the scoped ignores keep the connector strict-clean without relaxing the package gate.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping
from importlib import resources
from typing import Any

import websockets

from worldmonitor.net.ssrf import assert_public_host
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import InvalidEntity, validate_or_raise
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, RawRecord, Status
from worldmonitor.plugins.stream import EventSource, StreamConnector
from worldmonitor.provenance.model import Provenance, stamp

logger = logging.getLogger(__name__)

# The public Jetstream firehose (verified) — overridable via the ``endpoint`` config field.
_DEFAULT_ENDPOINT = "wss://jetstream2.us-east.bsky.network/subscribe"
_DEFAULT_WANTED_COLLECTIONS = ["app.bsky.feed.post"]
_POST_COLLECTION = "app.bsky.feed.post"
# Per-event byte sanity cap for the raw WS frame (a hostile, oversized event is dropped by the lib).
_MAX_EVENT_BYTES = 1 * 1024 * 1024


class BlueskyConnector(StreamConnector):
    """Imports Bluesky posts (FtM Article, metadata only) from the public Jetstream firehose."""

    def __init__(self, *, event_source: EventSource | None = None) -> None:
        """Store the injectable transport seam + the per-run config the real consumer reads.

        Tests inject ``event_source`` (canned events, no live WS); production leaves it ``None`` so
        :meth:`_default_event_source` opens the real socket. The locked seam is
        ``(cursor, window_seconds, max_events)`` (no ``config``), so ``collect`` stashes the config
        here for the real consumer to read the endpoint / wanted collections.
        """
        super().__init__(event_source=event_source)
        self._collect_config: dict[str, Any] = {}

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="bluesky",
            name="Bluesky",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.STREAM,
            capability=Capability.PASSIVE,
            description="Bluesky Jetstream firehose; imports each post as an FtM Article.",
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Capture the run config (for the real consumer) then delegate to the bounded base.

        The locked ``event_source`` seam carries no config, so the endpoint / wanted-collections the
        production consumer needs are stashed here before the window runs. The base ``collect`` does
        the validation, resume-cursor read, and self-bounding (unchanged for the injected seam).
        """
        self._collect_config = dict(config)
        return super().collect(config)

    def _extract_event(self, event: Mapping[str, Any]) -> tuple[str, str, bytes]:
        """Extract ``(key, cursor, data)`` from a raw Jetstream event.

        ``key`` is ``did/rkey`` (the post's stable id), ``cursor`` is ``str(time_us)`` (the
        time-based Jetstream cursor the next window resumes from), and ``data`` is the verbatim
        event JSON. A non-commit / commit-less event yields an empty ``rkey`` — ``map`` skips it.
        """
        did = str(event.get("did", ""))
        commit = event.get("commit")
        rkey = str(commit.get("rkey", "")) if isinstance(commit, Mapping) else ""
        cursor = str(event.get("time_us", ""))
        data = json.dumps(dict(event)).encode("utf-8")
        return (f"{did}/{rkey}", cursor, data)

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Map one ``app.bsky.feed.post`` create commit to an FtM Article with provenance.

        Only a post-create commit maps; a delete / non-post collection / malformed event maps to
        ``[]`` (fail-soft — this method NEVER raises). The id is ``bluesky-{did}-{rkey}`` so a
        re-ingest enqueues idempotently. Hostile bytes are validated through FtM.
        """
        try:
            event = json.loads(record.data)
        except (ValueError, TypeError):
            return []
        if not isinstance(event, dict):
            return []
        commit = event.get("commit")
        if (
            event.get("kind") != "commit"
            or not isinstance(commit, dict)
            or commit.get("collection") != _POST_COLLECTION
            or commit.get("operation") != "create"
        ):
            return []
        post = commit.get("record")
        if not isinstance(post, dict) or post.get("$type") != _POST_COLLECTION:
            return []

        did = event.get("did")
        rkey = commit.get("rkey")
        if not isinstance(did, str) or not did or not isinstance(rkey, str) or not rkey:
            return []

        properties: dict[str, list[str]] = {
            "author": [did],
            "sourceUrl": [f"at://{did}/{_POST_COLLECTION}/{rkey}"],
        }
        text = post.get("text")
        if isinstance(text, str) and text:
            properties["title"] = [text]
            properties["bodyText"] = [text]
        created = post.get("createdAt")
        if isinstance(created, str) and created:
            properties["publishedAt"] = [created]

        try:
            entity = validate_or_raise(
                {
                    "id": f"bluesky-{did}-{rkey}",
                    "schema": "Article",
                    "properties": properties,
                    "datasets": ["bluesky"],
                }
            )
        except InvalidEntity as exc:
            logger.warning("bluesky: dropping malformed post %s/%s: %s", did, rkey, exc)
            return []
        return [stamp(entity, provenance)]

    # -- the real transport (NOT exercised by tests — seam is injected) ------- #
    def _default_event_source(
        self, cursor: str | None, window_seconds: int, max_events: int
    ) -> Iterator[dict[str, Any]]:
        """Consume the real Jetstream WS firehose for one bounded window (production path).

        Builds the ``wss`` URL from the configured endpoint + ``wantedCollections`` + the resume
        ``cursor`` (when set), asserts the host is public (defense-in-depth), and drains the socket
        via ``asyncio.run`` for at most ``window_seconds`` or ``max_events`` events — buffered into
        a list (bounded by ``max_events``) so the call always RETURNS within ~``window_seconds``,
        never blocking forever.
        """
        config = self._collect_config
        endpoint = str(config.get("endpoint") or _DEFAULT_ENDPOINT)
        wanted = config.get("wanted_collections") or _DEFAULT_WANTED_COLLECTIONS
        params: list[tuple[str, str]] = [("wantedCollections", str(c)) for c in wanted]
        if cursor:
            params.append(("cursor", str(cursor)))
        url = f"{endpoint}?{urllib.parse.urlencode(params)}"

        host = urllib.parse.urlsplit(endpoint).hostname or ""
        assert_public_host(host)

        events = asyncio.run(self._consume_window(url, window_seconds, max_events))
        return iter(events)

    async def _consume_window(
        self, url: str, window_seconds: int, max_events: int
    ) -> list[dict[str, Any]]:
        """Drain the WS for one wall-clock-bounded window; return the buffered events.

        A per-``recv`` timeout (the remaining window budget) plus a wall-clock deadline guarantee
        the coroutine returns within ~``window_seconds`` even on a quiet stream; ``max_size`` caps a
        single hostile frame. Un-parseable / non-object frames are skipped.
        """
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + window_seconds
        async with websockets.connect(url, max_size=_MAX_EVENT_BYTES) as socket:
            while len(events) < max_events:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    message = await asyncio.wait_for(socket.recv(), timeout=remaining)
                except TimeoutError:
                    break
                except websockets.ConnectionClosed:
                    break
                try:
                    event = json.loads(message)
                except (ValueError, TypeError):
                    continue
                if isinstance(event, dict):
                    events.append(event)
        return events
