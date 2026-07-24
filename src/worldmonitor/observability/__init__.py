"""Derived, read-only observability surfaces over run-metadata (Gate F-1, ADR 0123).

Unlike :mod:`worldmonitor.metrics` (the raw on-scrape Prometheus collector, ADR 0076), this
package holds *derived* projections consumed by more than one surface — the first is the 6-state
source-freshness machine (:mod:`worldmonitor.observability.freshness`), shared by the REST route
(``GET /sources/freshness``) and the driver's Prometheus gauge so the two can never drift.
"""

from __future__ import annotations
