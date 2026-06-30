"""LiteLLM gateway for service-side LLM use (Phase-3 S2, ADR 0091).

Public surface: ``LLMGateway`` (the single egress choke point) and ``LLMMode``
(the three-mode confidential selector).  Everything else is implementation-private.
"""

from worldmonitor.llm.gateway import LLMGateway
from worldmonitor.llm.modes import LLMMode

__all__ = ["LLMGateway", "LLMMode"]
