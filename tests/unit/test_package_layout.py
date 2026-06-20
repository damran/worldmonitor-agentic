"""The Phase 0 package skeleton: every module from the roadmap layout imports."""

import importlib

import pytest

MODULES = [
    "worldmonitor",
    "worldmonitor.api",
    "worldmonitor.mcp",
    "worldmonitor.authz",
    "worldmonitor.ontology",
    "worldmonitor.plugins",
    "worldmonitor.plugins.connectors",
    "worldmonitor.plugins.enrichers",
    "worldmonitor.plugins.resolvers",
    "worldmonitor.plugins.rules",
    "worldmonitor.plugins.scorers",
    "worldmonitor.plugins.notifiers",
    "worldmonitor.runner",
    "worldmonitor.resolution",
    "worldmonitor.graph",
    "worldmonitor.provenance",
    "worldmonitor.improvement",
    "worldmonitor.llm",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None
