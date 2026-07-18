"""News→event LLM extraction (ADR 0115, Slice B).

Turns curated-feed **Article** nodes into a semantic layer on the graph: for each recent article
the LLM gateway (LOCAL/Ollama by default) extracts the primary real-world event + the named actors,
and this module derives FtM ``Event`` + ``Person``/``Organization`` candidates that are enqueued
into the ER queue. The **existing resolver** then applies the catastrophic-merge guard and the
fail-closed provenance-on-write invariant — this module never writes the graph directly (it only
reads Article nodes and enqueues candidates), exactly like a connector.

Leads, not verdicts: extraction is a best-effort *hypothesis* (reliability ``C``); a hostile or
malformed model response is dropped, never trusted. The pass is default-OFF (``extraction_enabled``)
and hard-bounded per cycle (``extraction_max_articles_per_cycle``); the high-volume Bluesky firehose
is never selected (curated feeds only). Idempotency + cost-bounding ride on a processing WATERMARK:
every processed article (event OR non-event) is marked ``extraction_done`` on its node, so the
selection never re-scans it — this both prevents non-event articles from starving fresh events and
bounds steady-state LLM cost to the feed's arrival rate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.geo import COUNTRY_CENTROIDS, city_coords
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.runner.fulltext import load_body

logger = logging.getLogger(__name__)

# The connector_id label stamped on derived candidates (ErQueueItem.connector_id is a free-form
# attribution label, not an FK) + the provenance source id / dataset for derived entities.
_DERIVATION_ID = "derivation:geoevent"
_SUMMARY_MAX = 240
_NAME_MAX = 160
_MAX_ACTORS = 5
_2LETTER = re.compile(r"^[a-z]{2}$")
# An FtM-safe ISO-8601 date prefix (YYYY[-MM[-DD]]…) — the shape the feeds connector's
# ``_entry_date`` normalizer emits. Anything else falls back to the extraction timestamp.
_ISO_DATE_PREFIX = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?([T ].*)?$")

# The extraction contract. Kept deliberately small + strict so a weak local model can satisfy it;
# the parser is defensive regardless (a wrong shape is dropped, never trusted).
_SYSTEM_PROMPT = (
    "You are an OSINT analyst. From a news headline (and optional summary and article text), "
    "extract the single "
    "primary real-world EVENT it reports. Reply with ONLY a JSON object, no markdown, no prose. "
    "Keys: "
    '"is_event" (true only if it reports a discrete real-world event: attack, protest, disaster, '
    "deal, sanction, election, etc.; false for opinion/analysis/roundups), "
    '"event_type" (short lowercase category or null), '
    '"summary" (one plain sentence, <=200 chars, describing what happened), '
    '"country" (ISO 3166-1 alpha-2 code, lowercase, of the primary location, or null), '
    '"place" (city/region name or null), '
    '"actors" (array, max 5, of {"name": string, "kind": "person"|"organization"} for '
    "EXPLICITLY named people/organisations central to the event; [] if none named). "
    "Output the JSON object only."
)


@dataclass(slots=True)
class ExtractStats:
    """What one extraction cycle did (returned to the driver for the task_run record)."""

    scanned: int = 0
    extracted: int = 0
    events: int = 0
    actors: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, object]:
        # ``object`` (not ``int``) so it slots into the driver's ``_finalize(stats: dict[str,
        # object] | None)`` — dict value types are invariant under pyright strict.
        return {
            "scanned": self.scanned,
            "extracted": self.extracted,
            "events": self.events,
            "actors": self.actors,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def build_messages(
    title: str, summary: str | None, body: str | None = None
) -> list[dict[str, str]]:
    """The chat messages for one article (system contract + headline/summary/cached body).

    ``body`` is the full-text cache's already-truncated article text (ADR 0116) — hostile input
    like everything else here; it rides inside the user message and the reply goes through the
    same defensive :func:`parse_extraction` trust boundary.
    """
    user = f"Headline: {title}"
    if summary:
        user += f"\nSummary: {summary}"
    if body:
        user += f"\nArticle text: {body}"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def response_text(response: Any) -> str:
    """Defensively read the completion text from a litellm/OpenAI-shaped response."""
    try:
        choices = response.choices
        content = choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


def parse_extraction(content: str) -> dict[str, Any] | None:
    """Parse the model's reply into a validated extraction dict, or ``None`` to drop it.

    Defensive against hostile/malformed output: it slices the first ``{`` .. last ``}`` (tolerating
    markdown fences / chatter around the JSON), ``json.loads`` it, and returns ``None`` on any
    failure, on a non-dict, or when ``is_event`` is not truthy (a non-event article yields no
    Event). This is the trust boundary — external model output is never eval'd or executed.
    """
    if not content:
        return None
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    parsed = cast("dict[str, Any]", data)
    if not parsed.get("is_event"):
        return None
    return parsed


def _clean_actors(raw: Any) -> list[dict[str, str]]:
    """Coerce the actors array into ``[{name, schema}]`` (Person/Organization), dropping junk."""
    if not isinstance(raw, list):
        return []
    actors: list[dict[str, str]] = []
    for item in cast("list[Any]", raw)[:_MAX_ACTORS]:
        if not isinstance(item, Mapping):
            continue
        entry = cast("Mapping[str, Any]", item)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        kind = str(entry.get("kind", "")).strip().lower()
        schema = "Person" if kind == "person" else "Organization"
        actors.append({"name": name.strip()[:_NAME_MAX], "schema": schema})
    return actors


def _event_id(article_id: str) -> str:
    return f"evt-{hashlib.sha1(article_id.encode('utf-8')).hexdigest()}"


def _actor_id(schema: str, name: str, article_id: str) -> str:
    """A candidate id SCOPED to the source article.

    Deliberately NOT a bare name hash: a name-only id would MERGE two DISTINCT same-named people
    (two "John Smith"s) into one node by id-fiat, bypassing the catastrophic-merge guard. Scoping
    to the article makes each extracted mention its own candidate, so any cross-article merge is
    decided by the ER pipeline + the guard — never forced here. (Re-extracting the SAME article
    yields the same id, so it stays idempotent.)
    """
    prefix = "person" if schema == "Person" else "org"
    digest = hashlib.sha1(f"{article_id}:{name.lower()}".encode()).hexdigest()
    return f"{prefix}-{digest}"


def _address_id(article_id: str, place: str) -> str:
    """A derived Address candidate id, scoped to the article (same rationale as ``_actor_id``)."""
    return f"addr-{hashlib.sha1(f'{article_id}:{place.lower()}'.encode()).hexdigest()}"


def _valid_country(value: Any) -> str | None:
    """Return a plottable ISO alpha-2 code (lowercase) or ``None``.

    Restricted to the known-centroid set so (a) the FtM ``country`` type always validates and
    (b) the Event actually plots on the dashboard globe. An unknown/malformed value is dropped.
    """
    if not isinstance(value, str):
        return None
    code = value.strip().lower()
    return code if _2LETTER.match(code) and code in COUNTRY_CENTROIDS else None


def derive_entities(
    article: Mapping[str, Any],
    extraction: Mapping[str, Any],
    *,
    retrieved_at: str,
) -> list[FtmEntity]:
    """Build the stamped FtM entities for one article's extraction: ``[Event, *actors]``.

    Returns ``[]`` when there is nothing worth an Event (no usable summary AND no event_type). The
    Event carries the actor links (``involved``) + the source-article receipt (``proof``), and its
    geo is EITHER a precise pin (a linked FtM ``Address`` for a known city, Slice F) OR — as a
    fallback — the coarse ISO country literal (so ``/points`` plots it at the country centroid).
    Every entity is provenance-stamped (reliability ``C`` — a derived lead) so the fail-closed
    writer accepts it.
    """
    summary_raw = extraction.get("summary")
    summary = summary_raw.strip()[:_SUMMARY_MAX] if isinstance(summary_raw, str) else ""
    event_type_raw = extraction.get("event_type")
    event_type = event_type_raw.strip() if isinstance(event_type_raw, str) else ""
    name = summary or event_type
    if not name:
        return []

    article_id = str(article["id"])
    source_record = str(article["source_record"])
    country = _valid_country(extraction.get("country"))
    place_raw = extraction.get("place")
    place = place_raw.strip()[:_NAME_MAX] if isinstance(place_raw, str) else ""

    prov = Provenance(
        source_id=_DERIVATION_ID,
        retrieved_at=retrieved_at,
        reliability="C",
        source_record=source_record,
    )

    actors = _clean_actors(extraction.get("actors"))
    actor_ids: list[str] = []
    entities: list[FtmEntity] = []
    for actor in actors:
        actor_id = _actor_id(actor["schema"], actor["name"], article_id)
        # Only the name — NOT the event's country. An actor's nationality/jurisdiction is unknown
        # from the extraction; stamping it with the event's country would be wrong AND would seed
        # a bogus Splink blocking key. Country stays on the Event alone.
        entities.append(
            stamp(
                validate_or_raise(
                    {
                        "id": actor_id,
                        "schema": actor["schema"],
                        "properties": {"name": [actor["name"]]},
                        "datasets": ["events"],
                    }
                ),
                prov,
            )
        )
        actor_ids.append(actor_id)

    # Precise geo (Slice F): if the extracted place is a known major city, emit an FtM Address (it
    # carries real latitude/longitude that ftmg keeps + /points plots) and link the Event to it via
    # ``addressEntity``. The Address gives the Event a precise pin; the Event then does NOT carry a
    # ``country`` (so /points does not ALSO plot it at the centroid — no double dot). A place not in
    # the gazetteer falls back to the coarse country centroid on the Event itself.
    coords = city_coords(place)
    address_id: str | None = None
    if coords is not None:
        address_id = _address_id(article_id, place)
        addr_props: dict[str, list[str]] = {
            "full": [place],
            "latitude": [str(coords[0])],
            "longitude": [str(coords[1])],
        }
        if country:
            addr_props["country"] = [country]
        entities.append(
            stamp(
                validate_or_raise(
                    {
                        "id": address_id,
                        "schema": "Address",
                        "properties": addr_props,
                        "datasets": ["events"],
                    }
                ),
                prov,
            )
        )

    # NB: the model's ``event_type`` is deliberately NOT written to FtM ``topics`` — ``topics`` is a
    # typed RISK vocabulary (``sanction``/``crime``/…), and routing unvalidated model output there
    # would let a crafted headline stamp a risk label on the Event, flipping adjacent (INVOLVED)
    # real actors to sensitive/parked via the catastrophic-merge sensitivity guard. It lives only in
    # the Event's ``name`` (display), never the risk model.
    #
    # Event ``date`` = the article's PUBLISH date when the feed carried one (WP-2c) — an event
    # happened when it was reported, not when the extraction pass got to it. The extraction
    # timestamp is only the fallback for date-less feed entries; a non-ISO-shaped value also falls
    # back (never trust a malformed feed date into a validated FtM property).
    published = article.get("published")
    event_date = (
        published.strip()
        if isinstance(published, str) and _ISO_DATE_PREFIX.match(published.strip())
        else retrieved_at
    )
    event_props: dict[str, list[str]] = {"name": [name], "date": [event_date]}
    if summary:
        event_props["summary"] = [summary]
    if place:
        event_props["location"] = [place]
    if address_id is not None:
        event_props["addressEntity"] = [
            address_id
        ]  # precise pin (:Event)-[:ADDRESS_ENTITY]->(:Address)
    elif country:
        event_props["country"] = [country]  # coarse fallback: /points plots at the country centroid
    if actor_ids:
        event_props["involved"] = actor_ids
    # The receipt: (:Event)-[:PROOF]->(:Article). The Article node already exists (feeds ingest);
    # linking by its real id makes it a traversable neighbour in the graph panel.
    event_props["proof"] = [article_id]

    event = stamp(
        validate_or_raise(
            {
                "id": _event_id(article_id),
                "schema": "Event",
                "properties": event_props,
                "datasets": ["events"],
            }
        ),
        prov,
    )
    return [event, *entities]


def select_feed_articles(neo4j: Neo4jClient, *, limit: int) -> list[dict[str, Any]]:
    """Recent, NOT-YET-EXTRACTED curated-feed Article nodes (newest first).

    Excludes the Bluesky firehose (``'feeds' IN n.datasets``) AND any article already watermarked
    (``n.extraction_done IS NULL``) — the watermark is what stops non-event articles from refilling
    the newest window and starving fresh events.
    """
    query = (
        "MATCH (n:Entity) "
        "WHERE 'Article' IN labels(n) AND 'feeds' IN n.datasets AND n.extraction_done IS NULL "
        "RETURN n.id AS id, head(n.title) AS title, head(n.summary) AS summary, "
        "coalesce(head(n.publishedAt), head(n.date)) AS published, "
        "n.prov_source_record AS source_record "
        "ORDER BY coalesce(head(n.publishedAt), head(n.date), n.prov_retrieved_at) DESC "
        f"LIMIT {max(1, int(limit))}"
    )
    return [
        row for row in neo4j.execute_read(query) if row.get("title") and row.get("source_record")
    ]


def _mark_extracted(neo4j: Neo4jClient, article_id: str) -> None:
    """Watermark an Article node as extraction-processed so it is never re-scanned.

    A minimal operational property on an EXISTING node (not an entity write, no new node) — ftmg's
    additive ``SET n += props`` on any later re-emit leaves it untouched, so the watermark persists.
    Marking every processed article (event OR non-event) is what bounds steady-state LLM cost to the
    feed's arrival rate and prevents non-event starvation.
    """
    neo4j.execute_write(
        "MATCH (n:Entity {id: $article_id}) SET n.extraction_done = true", article_id=article_id
    )


def _enqueue(session: Session, entities: Sequence[FtmEntity], *, source_record: str) -> None:
    """Idempotently enqueue derived entities as ER-queue candidates (status='pending')."""
    for entity in entities:
        session.execute(
            pg_insert(ErQueueItem)
            .values(
                id=str(uuid.uuid4()),
                connector_id=_DERIVATION_ID,
                entity_id=entity.id,
                raw_entity=entity.to_dict(),
                source_record=source_record,
                status="pending",
            )
            .on_conflict_do_nothing(constraint="uq_er_queue_dedup")
        )
    session.commit()


# The gateway seam: anything exposing chat(messages, *, caller_tag=...) -> response.
ChatFn = Callable[..., Any]


def extract_cycle(
    *,
    neo4j: Neo4jClient,
    sessions: Callable[[], Session],
    gateway: Any,
    max_articles: int,
    retrieved_at: str,
    body_max_chars: int = 4000,
) -> ExtractStats:
    """Run one bounded extraction cycle. Per-article failures are isolated (one bad article never
    aborts the cycle). Returns what happened for the driver's task_run record.

    When the full-text pass (ADR 0116) has cached a body for an article, the prompt carries it
    (truncated to ``body_max_chars``); otherwise the headline/summary alone — best-effort, never a
    dependency."""
    stats = ExtractStats()
    # The selection already excludes watermarked articles, so the freshest `max_articles`
    # UNPROCESSED feed articles come back — no wider scan / in-loop skip needed.
    articles = select_feed_articles(neo4j, limit=max_articles)[:max_articles]
    for article in articles:
        stats.scanned += 1
        article_id = str(article["id"])
        try:
            body = load_body(sessions, article_id, max_chars=body_max_chars)
            messages = build_messages(str(article["title"]), article.get("summary"), body)
            response = gateway.chat(messages, caller_tag="extraction")
            extraction = parse_extraction(response_text(response))
            entities = (
                derive_entities(article, extraction, retrieved_at=retrieved_at)
                if extraction
                else []
            )
            if entities:
                with sessions() as session:
                    _enqueue(session, entities, source_record=str(article["source_record"]))
                stats.extracted += 1
                stats.events += 1
                # Count only actor entities — the derived set also holds the Event + (maybe) an
                # Address, which are not actors.
                stats.actors += sum(
                    1 for e in entities if e.schema.name in ("Person", "Organization")
                )
            else:
                stats.skipped += 1
            # Watermark AFTER a successful LLM call + enqueue (event or not) so it is never
            # re-scanned; an errored article is left UNmarked so it retries next cycle.
            _mark_extracted(neo4j, article_id)
        except Exception as exc:  # noqa: BLE001 — one article's failure must not abort the cycle
            logger.warning("extraction failed for article %s: %s", article_id, exc)
            stats.errors += 1
    return stats
