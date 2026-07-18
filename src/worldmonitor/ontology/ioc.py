"""Connector-independent IOC identity (executes ADR 0118's carried phase-2 precondition).

The S-2 checker's finding (recorded in ADR 0118): ``matchable: false`` is honored by FtM /
nomenklatura's native matcher but is inert in our Splink scoring path, so ``wm:Indicator``
identity is **id-only** — which is safe only if every Indicator-producing connector derives the
SAME id for the same IOC value. This module is that shared scheme; every Indicator connector
MUST derive its entity id here and never mint its own.

Executed EARLY (pre-first-deployment, 2026-07-19) so no live graph ever holds a
connector-prefixed Indicator id — the fix is a rename today and would be a data migration the
day after the operator's first deploy.

Normalization contract: ``strip()`` + ``casefold()`` — IPs/ports are case-free so this is a
no-op for Feodo, but future domain/URL/hash indicators must converge case-insensitively
(``EvIl.example`` ≡ ``evil.example``; hex digests compare lowercased). Anything stronger
(IDNA, defanging like ``hxxp``/``[.]``) is a per-connector mapping concern that happens BEFORE
this function; the scheme hashes exactly what it is given post-normalization.
"""

from __future__ import annotations

import hashlib

_PREFIX = "ioc-"


def indicator_id(value: str) -> str:
    """The canonical entity id for an IOC value: ``ioc-<sha1(normalized value)>``.

    Deterministic and connector-independent: the same normalized value from Feodo, ThreatFox,
    URLhaus, or any future sibling derives the identical id, so cross-connector records of one
    real-world indicator converge on one node by identity — never by fuzzy matching.
    """
    normalized = value.strip().casefold()
    return f"{_PREFIX}{hashlib.sha1(normalized.encode('utf-8')).hexdigest()}"
