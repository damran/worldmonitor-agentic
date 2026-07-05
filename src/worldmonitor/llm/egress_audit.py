"""Durable, append-only LLM-egress audit writer (Gate F2 / ADR 0105).

Sibling to :mod:`worldmonitor.llm.egress_log` (the stdlib-logging audit, L1 / INV-S2-EGRESS,
FROZEN — byte-unchanged). This module builds and persists the durable Postgres counterpart:
a content fingerprint + an optional caller-declared entity manifest on the pre-call "attempt"
row, and token usage on the post-call "completed" row, correlated by a shared ``call_id``.

Module-level functions ONLY (not methods on ``LLMGateway``) — this keeps
``test_gateway_has_no_public_egress_bypass_other_than_chat`` green; the gateway calls these
functions from inside ``chat()``.

Append-only invariants (mirrors ``resolution/statements.py`` / ADR 0099):
* :func:`write_row` only ever issues ``session.add`` + ``commit`` — never an UPDATE, never a
  DELETE, never ``session.delete``.
* No column of either built row ever holds message content or the api key — the fingerprint
  (a fixed-length sha256 hex digest) is the durable, non-leaking stand-in (ADR 0091 §3,
  extended).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from worldmonitor.db.models import LlmEgressRecord
from worldmonitor.llm.egress_log import EgressRecord


def fingerprint_messages(messages: list[dict[str, Any]]) -> str:
    """A sha256 hex digest over the canonicalized outbound ``messages`` — TOTAL: never raises.

    Uses the repo's existing ``sort_keys=True`` canonicalization idiom (``provenance/model.py``,
    ``backup.py``) so the digest is key-order-insensitive and content-sensitive. Totality over
    hostile payloads (adversarial-verify fix round, ADR 0105):

    * ``default=str`` stringifies a non-JSON-serializable leaf VALUE;
    * ``"surrogatepass"`` encodes lone UTF-16 surrogates deterministically instead of raising
      ``UnicodeEncodeError`` — lone surrogates are wire-reachable via stdlib ``json.loads``
      escape handling (``"\\ud800"``) and pydantic ``content: str`` passes them through;
    * any remaining serialization failure (non-str dict keys / mixed-key ``sort_keys``
      ``TypeError``, circular-reference ``ValueError``, a leaf whose ``__str__`` raises) falls
      back to a coarse, deterministic type-level sentinel — content identity is unattainable
      for such payloads anyway, and an audit fingerprint must never break the choke point.

    Determinism domain, stated honestly: byte-deterministic across processes for JSON-shaped
    payloads (the ``/v1`` wire case). For non-serializable in-process payloads, ``default=str``
    reprs may embed memory addresses, so those digests identify the call, not the content.

    NEVER stores ``messages`` itself — only this fixed-length 64-char hex digest.
    """
    try:
        canonical = json.dumps(
            messages,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
    except Exception:  # totality: hostile keys / cycles / raising reprs (see docstring)
        try:
            size = str(len(messages))
        except Exception:
            size = "?"
        canonical = f"unserializable:{type(messages).__qualname__}:{size}"
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


def build_attempt_row(
    call_id: str,
    record: EgressRecord,
    fingerprint: str,
    entity_ids: list[str] | None,
) -> LlmEgressRecord:
    """Build the pre-call "attempt" row: fingerprint + declared manifest set, tokens NULL.

    ``entity_ids`` is recorded honestly: ``None`` (not declared / the ``/v1`` wire-caller
    default) stays ``None`` on the row — never faked as an empty list (SF-2).
    """
    return LlmEgressRecord(
        id=str(uuid.uuid4()),
        call_id=call_id,
        phase="attempt",
        mode=record.mode.value,
        confidentiality=record.confidentiality,
        target_host=record.target_host,
        data_left_perimeter=record.data_left_perimeter,
        model=record.model,
        caller_tag=record.caller_tag,
        content_fingerprint=fingerprint,
        entity_manifest=entity_ids,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
    )


def build_completed_row(call_id: str, record: EgressRecord) -> LlmEgressRecord:
    """Build the post-call "completed" row: token usage set, fingerprint/manifest NULL.

    Token extraction is defensive (``getattr``-style, mirrors
    ``egress_log._extract_usage_tokens``): a ``usage`` object missing an attribute yields
    ``None`` for that column rather than raising.
    """
    usage = record.usage
    return LlmEgressRecord(
        id=str(uuid.uuid4()),
        call_id=call_id,
        phase="completed",
        mode=record.mode.value,
        confidentiality=record.confidentiality,
        target_host=record.target_host,
        data_left_perimeter=record.data_left_perimeter,
        model=record.model,
        caller_tag=record.caller_tag,
        content_fingerprint=None,
        entity_manifest=None,
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )


def write_row(session_factory: Callable[[], Session], row: LlmEgressRecord) -> None:
    """Persist ``row`` in its own short transaction: add, commit, close.

    Unlike ``resolution/statements.py`` (where the caller commits as part of a larger
    transaction), this writer owns its own commit — the pre-call "attempt" row must commit
    BEFORE the provider call (INV-DURABLE-COMPLETE), so it cannot ride along with any other
    unit of work.

    INSERT-only: never UPDATE, never DELETE, never ``session.delete``. Propagates any commit
    failure (the caller — the gateway — decides fail-closed-vs-best-effort per mode); the
    session is always closed via ``finally``, even when commit raises.
    """
    session = session_factory()
    try:
        session.add(row)
        session.commit()
    finally:
        session.close()
