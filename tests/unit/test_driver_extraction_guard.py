"""The driver's extraction pass is a no-op unless explicitly enabled + wired (ADR 0115, Slice B).

The LLM-cost switch: ``run_extraction`` must do NOTHING (no LLM call, no work) unless
``extraction_enabled`` is set AND a gateway is present. These pin the default-OFF guard so the pass
can never fire — or spend — by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from worldmonitor.runner.driver import IngestDriver
from worldmonitor.settings import Settings

_NOW = datetime(2026, 7, 12, tzinfo=UTC)


def _driver(*, enabled: bool, gateway: object | None) -> IngestDriver:
    return IngestDriver(
        sessions=MagicMock(),
        landing=MagicMock(),
        neo4j=MagicMock(),
        registry=MagicMock(),
        settings=Settings(_env_file=None, environment="test", extraction_enabled=enabled),  # type: ignore[call-arg]
        cipher=MagicMock(),
        heartbeat=MagicMock(),
        llm_gateway=gateway,  # type: ignore[arg-type]
    )


def test_extraction_default_off_in_code() -> None:
    """The code default must be OFF — an operator opts in, never the reverse."""
    assert Settings(_env_file=None, environment="test").extraction_enabled is False  # type: ignore[call-arg]


def test_run_extraction_is_noop_when_disabled() -> None:
    driver = _driver(enabled=False, gateway=MagicMock())
    assert driver.run_extraction(now=_NOW) == []
    driver._neo4j.execute_read.assert_not_called()  # no work, no LLM, no reads


def test_run_extraction_is_noop_without_a_gateway() -> None:
    driver = _driver(enabled=True, gateway=None)
    assert driver.run_extraction(now=_NOW) == []
    driver._neo4j.execute_read.assert_not_called()
