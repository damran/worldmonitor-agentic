"""The shared connector-independent IOC id scheme (S-2b — ADR 0118's executed precondition).

Indicator identity is id-only (``matchable: false`` is inert in the Splink path — the checker
finding ADR 0118 records), so EVERY Indicator connector must derive the same id for the same
IOC value. These tests pin the scheme itself; ``test_feodo_connector.py`` pins that feodo
actually uses it.
"""

from __future__ import annotations

import hashlib

from worldmonitor.ontology.ioc import indicator_id


def test_deterministic_and_prefixed() -> None:
    a = indicator_id("203.0.113.7:443")
    assert a == indicator_id("203.0.113.7:443")
    assert a.startswith("ioc-") and len(a) == 4 + 40  # prefix + sha1 hexdigest


def test_normalization_converges_case_and_whitespace() -> None:
    """Future domain/hash indicators must converge case-insensitively; whitespace never splits
    identity. (No-op for IPs — pinned so a later 'optimization' cannot silently drop it.)"""
    assert indicator_id("EvIl.example") == indicator_id("evil.example")
    assert indicator_id("  203.0.113.7:443  ") == indicator_id("203.0.113.7:443")
    assert indicator_id("ABCDEF0123") == indicator_id("abcdef0123")


def test_distinct_values_never_collide_and_rule_is_the_documented_sha1() -> None:
    assert indicator_id("203.0.113.7:443") != indicator_id("203.0.113.7:80")
    expected = "ioc-" + hashlib.sha1(b"203.0.113.7:443").hexdigest()
    assert indicator_id("203.0.113.7:443") == expected
