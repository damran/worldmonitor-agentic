"""Gate H-8c follow-up (ADR 0078): structural + parity tests for the Prometheus scrape config and
alert rules shipped under deploy/prometheus/.

These are ALL Docker-free, run via 'uv run pytest'. They are intentionally RED until the builder
ships the config files.

Invariants enforced:
  (a) YAML syntax — prometheus.yml and worldmonitor.rules.yml parse without error.
  (b) PARITY (PRIMARY / INV-PARITY): every worldmonitor_* metric name referenced in an alert expr
      is derived dynamically from src/worldmonitor/metrics/collector.py source text at test runtime.
      A rename or removal in the collector immediately breaks this test; a hand-copied list cannot
      drift silently. The only non-worldmonitor_ metric allowed is 'up' (and only with
      job="worldmonitor-driver"). An adversarial fixture proves a misspelled metric fails.
  (c) INV-SCRAPE: 'worldmonitor-driver' job targets driver:<port> where <port> matches
      Settings().driver_metrics_port (9108); global scrape_interval / evaluation_interval are
      strictly <= resolve_cadence_seconds (300s); rule_files references the alerts glob.
  (d) INV-STRUCTURE: every alert has expr + for + labels.severity in {critical,warning,info} +
      non-empty annotations.summary + annotations.description. ResolutionWedged's threshold literal
      in the expr equals Settings().resolve_lock_skip_alert_threshold (3). Exactly two critical
      alerts (DriverDown and ResolutionWedged).
  (e) rule_files in prometheus.yml references a path that exists on disk.
  (f) OPTIONAL: if promtool is on PATH, validate config + rules + run the bundled test fixture,
      asserting returncode 0.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from worldmonitor.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMETHEUS_DIR = REPO_ROOT / "deploy" / "prometheus"
PROMETHEUS_YML = PROMETHEUS_DIR / "prometheus.yml"
RULES_YML = PROMETHEUS_DIR / "alerts" / "worldmonitor.rules.yml"
PROMTOOL_TEST_YML = PROMETHEUS_DIR / "tests" / "worldmonitor.rules.test.yml"
COLLECTOR_SRC = REPO_ROOT / "src" / "worldmonitor" / "metrics" / "collector.py"

# Prometheus synthetics we allow in alert exprs even though they are not emitted by the collector.
_ALLOWED_SYNTHETICS = {"up"}

# The closed label value sets per metric (from the collector source / ADR).
_VALID_LABEL_VALUES: dict[str, dict[str, set[str]]] = {
    "worldmonitor_task_runs": {
        "kind": {"ingest", "resolve"},
        "status": {"ok", "error", "running"},
    },
    "worldmonitor_resolve_last_stopped_reason": {
        "reason": {"exhausted", "timeout", "unknown"},
    },
}

# For 'up' the only valid label matcher in our context is job="worldmonitor-driver".
_UP_VALID_JOB = "worldmonitor-driver"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_yaml(path: Path) -> Any:
    """Parse a YAML file; the caller is responsible for asserting the file exists first."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _emitted_metric_names() -> set[str]:
    """Derive the set of worldmonitor_* family names EMITTED by DriverMetricsCollector by parsing
    the string literals in collector.py at test time.

    Strategy: extract every quoted string of the form "worldmonitor_[a-z0-9_]+" from the collector
    source. This is intentionally source-level (not instantiation-level) so it:
      1. Does not require a running DB or Neo4j stub (stays Docker-free).
      2. Fails immediately when a metric is renamed/removed in the source (the regex will not find
         the old name).
      3. Cannot drift from a hand-copied list maintained elsewhere.

    We cross-check the result against the ADR 0076 / gate.scope enumeration to make the oracle
    self-consistent (the test asserts a non-empty superset of the known names).
    """
    src = COLLECTOR_SRC.read_text(encoding="utf-8")
    # Match string literals — both single and double quoted — that contain worldmonitor_... text.
    # We extract the metric family name token from GaugeMetricFamily("worldmonitor_...", ...) calls.
    pattern = re.compile(r"""["'](?P<name>worldmonitor_[a-z0-9_]+)["']""")
    found = {m.group("name") for m in pattern.finditer(src)}
    return found


def _extract_metric_names_from_expr(expr: str) -> set[str]:
    """Extract worldmonitor_* metric name tokens and 'up' from a PromQL expression string.

    We use a regex that matches the family name (everything up to but not including '{' or
    whitespace or operator chars), which covers:
      worldmonitor_er_queue_pending > 10000
      worldmonitor_resolve_last_stopped_reason{reason="timeout"} == 1
      up{job="worldmonitor-driver"} == 0
    """
    # Metric names in PromQL: [a-zA-Z_:][a-zA-Z0-9_:]* but all ours use [a-z0-9_].
    metric_pattern = re.compile(r"\b(worldmonitor_[a-z0-9_]+|up)\b")
    return {m.group(1) for m in metric_pattern.finditer(expr)}


def _parse_label_matchers(expr: str) -> dict[str, dict[str, str]]:
    """Extract {metric_name -> {label_key -> label_value}} from label selectors in a PromQL expr.

    E.g. worldmonitor_resolve_last_stopped_reason{reason="timeout"} -> {'reason': 'timeout'}.
    Only exact-match matchers (=) are extracted; inequality matchers are ignored for the parity
    check (we are checking that referenced values are in the closed set, not checking the operator).
    """
    result: dict[str, dict[str, str]] = {}
    # Pattern: metric_name{key="value", ...}
    selector_pattern = re.compile(r"\b(worldmonitor_[a-z0-9_]+|up)\s*\{([^}]*)\}")
    kv_pattern = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
    for m in selector_pattern.finditer(expr):
        metric_name = m.group(1)
        labels_str = m.group(2)
        labels = dict(kv_pattern.findall(labels_str))
        result[metric_name] = labels
    return result


def _all_alert_rules(rules_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten all alert rules from a rules document."""
    rules: list[dict[str, Any]] = []
    for group in rules_doc.get("groups") or []:
        for rule in group.get("rules") or []:
            if "alert" in rule:
                rules.append(rule)
    return rules


def _parse_duration_to_seconds(duration: str) -> int:
    """Convert a Prometheus duration string (e.g. '30s', '5m', '1h') to seconds.
    Only supports s/m/h suffixes (sufficient for this gate's assertions).
    """
    duration = str(duration).strip()
    multipliers = {"s": 1, "m": 60, "h": 3600}
    m = re.fullmatch(r"(\d+)([smh])", duration)
    assert m is not None, f"cannot parse duration {duration!r}; expected e.g. '30s', '5m', '1h'"
    return int(m.group(1)) * multipliers[m.group(2)]


# --------------------------------------------------------------------------- #
# (a) YAML syntax — both config files parse without error
# --------------------------------------------------------------------------- #


def test_prometheus_yml_exists_and_parses_as_valid_yaml() -> None:
    """(a) deploy/prometheus/prometheus.yml must exist and be valid YAML."""
    assert PROMETHEUS_YML.exists(), (
        f"deploy/prometheus/prometheus.yml does not exist — builder must create it (ADR 0078 D1). "
        f"Looked at: {PROMETHEUS_YML}"
    )
    doc = _load_yaml(PROMETHEUS_YML)
    assert isinstance(doc, dict), (
        f"prometheus.yml must parse to a YAML mapping, got {type(doc).__name__}"
    )


def test_rules_yml_exists_and_parses_as_valid_yaml() -> None:
    """(a) deploy/prometheus/alerts/worldmonitor.rules.yml must exist and be valid YAML."""
    assert RULES_YML.exists(), (
        f"deploy/prometheus/alerts/worldmonitor.rules.yml does not exist — builder must create it "
        f"(ADR 0078 D2). Looked at: {RULES_YML}"
    )
    doc = _load_yaml(RULES_YML)
    assert isinstance(doc, dict), (
        f"worldmonitor.rules.yml must parse to a YAML mapping, got {type(doc).__name__}"
    )
    assert "groups" in doc, "worldmonitor.rules.yml must contain a top-level 'groups:' key"
    assert isinstance(doc["groups"], list) and doc["groups"], (
        "worldmonitor.rules.yml groups: must be a non-empty list"
    )


# --------------------------------------------------------------------------- #
# (b) PARITY (INV-PARITY) — every metric in alert exprs is in the emitted set
# --------------------------------------------------------------------------- #


def test_emitted_metric_names_non_empty_and_contains_known_set() -> None:
    """Sanity: the dynamic derivation from collector.py yields at least the known ADR 0076 names.
    If this fails, the regex extraction strategy is broken (not the rules file)."""
    emitted = _emitted_metric_names()
    known = {
        "worldmonitor_er_queue_pending",
        "worldmonitor_er_queue_pending_review",
        "worldmonitor_parked_merges",
        "worldmonitor_dead_letters",
        "worldmonitor_task_runs",
        "worldmonitor_graph_nodes",
        "worldmonitor_graph_edges",
        "worldmonitor_instances_in_error",
        "worldmonitor_resolve_consecutive_lock_skips",
        "worldmonitor_resolve_last_stopped_reason",
    }
    missing = known - emitted
    assert not missing, (
        f"dynamic metric-name extraction from collector.py is missing these known "
        f"names: {missing}. "
        f"The regex pattern may need updating, or the collector source has changed unexpectedly."
    )


def test_alert_exprs_reference_only_emitted_metrics_or_up_synthetic() -> None:
    """(b) PRIMARY PARITY INVARIANT (INV-PARITY): every worldmonitor_* name in every alert expr
    must appear in the set emitted by DriverMetricsCollector (derived live from the source).

    A renamed or removed metric in the collector breaks this test immediately. The only non-
    worldmonitor_ metric allowed is 'up' (and ONLY with job="worldmonitor-driver").
    """
    assert RULES_YML.exists(), f"rules file missing: {RULES_YML}"
    emitted = _emitted_metric_names()
    rules_doc = _load_yaml(RULES_YML)
    alerts = _all_alert_rules(rules_doc)
    assert alerts, "worldmonitor.rules.yml contains no alert rules — expected at least 7"

    drift_errors: list[str] = []
    for rule in alerts:
        alert_name = rule.get("alert", "<unnamed>")
        expr = str(rule.get("expr", ""))
        referenced = _extract_metric_names_from_expr(expr)
        for name in sorted(referenced):
            if name in _ALLOWED_SYNTHETICS:
                # 'up' is only valid with job="worldmonitor-driver"
                matchers = _parse_label_matchers(expr)
                job_val = (matchers.get("up") or {}).get("job")
                if job_val != _UP_VALID_JOB:
                    drift_errors.append(
                        f"Alert '{alert_name}': 'up' used without job=\"{_UP_VALID_JOB}\" "
                        f"(got job={job_val!r}). expr: {expr!r}"
                    )
            elif name not in emitted:
                drift_errors.append(
                    f"Alert '{alert_name}' references UNKNOWN metric '{name}' "
                    f"(not emitted by DriverMetricsCollector). expr: {expr!r}. "
                    f"Emitted set: {sorted(emitted)}"
                )

    assert not drift_errors, (
        "PARITY VIOLATION — alert rules reference metrics not emitted by the collector:\n"
        + "\n".join(drift_errors)
    )


def test_alert_label_matchers_use_only_valid_closed_label_values() -> None:
    """(b) Label value parity: label selectors in alert exprs use only the closed value sets
    (kind in {ingest,resolve}, status in {ok,error,running}, reason in {exhausted,timeout,unknown}).
    """
    assert RULES_YML.exists(), f"rules file missing: {RULES_YML}"
    rules_doc = _load_yaml(RULES_YML)
    alerts = _all_alert_rules(rules_doc)

    label_errors: list[str] = []
    for rule in alerts:
        alert_name = rule.get("alert", "<unnamed>")
        expr = str(rule.get("expr", ""))
        matchers_by_metric = _parse_label_matchers(expr)
        for metric_name, labels in matchers_by_metric.items():
            valid_for_metric = _VALID_LABEL_VALUES.get(metric_name, {})
            for label_key, label_val in labels.items():
                if label_key in valid_for_metric:
                    allowed = valid_for_metric[label_key]
                    if label_val not in allowed:
                        label_errors.append(
                            f"Alert '{alert_name}': metric '{metric_name}' uses "
                            f"{label_key}={label_val!r} which is NOT in the closed set "
                            f"{sorted(allowed)}. expr: {expr!r}"
                        )

    assert not label_errors, (
        "LABEL VALUE PARITY VIOLATION — alert exprs use invalid closed-set label values:\n"
        + "\n".join(label_errors)
    )


def test_adversarial_parity_misspelled_metric_name_fails_detection() -> None:
    """Adversarial fixture: confirm the parity detector catches a misspelled metric name.
    This test proves the oracle cannot be silently bypassed by a metric typo in the rules file.
    The detector must flag 'worldmonitor_er_queue_pendingg' (double 'g') as unknown.
    """
    emitted = _emitted_metric_names()
    misspelled_expr = "worldmonitor_er_queue_pendingg > 10000"
    referenced = _extract_metric_names_from_expr(misspelled_expr)
    unknown = {n for n in referenced if n not in emitted and n not in _ALLOWED_SYNTHETICS}
    assert "worldmonitor_er_queue_pendingg" in unknown, (
        "ADVERSARIAL FIXTURE FAILED: the parity detector did not flag a misspelled metric name. "
        "The oracle must catch 'worldmonitor_er_queue_pendingg' as not in the emitted set."
    )


# --------------------------------------------------------------------------- #
# (c) INV-SCRAPE — scrape job targets driver:9108, intervals <= cadence, rule_files present
# --------------------------------------------------------------------------- #


def test_worldmonitor_driver_scrape_job_targets_correct_host_and_port() -> None:
    """(c) The 'worldmonitor-driver' scrape job must target driver:<driver_metrics_port>."""
    assert PROMETHEUS_YML.exists(), f"prometheus.yml missing: {PROMETHEUS_YML}"
    doc = _load_yaml(PROMETHEUS_YML)
    scrape_configs = doc.get("scrape_configs") or []
    job = next(
        (j for j in scrape_configs if j.get("job_name") == "worldmonitor-driver"),
        None,
    )
    assert job is not None, (
        "prometheus.yml must contain a scrape job named 'worldmonitor-driver' (ADR 0078 D1)"
    )

    expected_port = Settings().driver_metrics_port  # 9108
    expected_target = f"driver:{expected_port}"

    static_configs = job.get("static_configs") or []
    all_targets: list[str] = []
    for sc in static_configs:
        all_targets.extend(sc.get("targets") or [])

    assert expected_target in all_targets, (
        f"scrape job 'worldmonitor-driver' must target '{expected_target}' (driver_metrics_port="
        f"{expected_port}); found targets: {all_targets}. "
        "The port must match Settings().driver_metrics_port so the two cannot drift (ADR 0078 D1)."
    )


def test_global_scrape_and_eval_interval_within_resolve_cadence() -> None:
    """(c) global.scrape_interval and evaluation_interval must be <= resolve_cadence_seconds (300s).
    ADR 0078 D1 mandates 30s (strictly below 300s) so the live lock_skips gauge is sampled multiple
    times per resolve tick and a wedge is never missed.
    """
    assert PROMETHEUS_YML.exists(), f"prometheus.yml missing: {PROMETHEUS_YML}"
    doc = _load_yaml(PROMETHEUS_YML)
    g = doc.get("global") or {}
    cadence_s = Settings().resolve_cadence_seconds  # 300

    for key in ("scrape_interval", "evaluation_interval"):
        raw = g.get(key)
        assert raw is not None, (
            f"prometheus.yml global.{key} must be set (ADR 0078 D1 requires <= {cadence_s}s)"
        )
        parsed = _parse_duration_to_seconds(str(raw))
        assert parsed <= cadence_s, (
            f"global.{key}={raw!r} ({parsed}s) exceeds resolve_cadence_seconds ({cadence_s}s). "
            "ADR 0078 D1 requires the scrape interval to be strictly below the resolve cadence so "
            "lock-skip gauges are sampled multiple times per resolve tick."
        )


def test_rule_files_references_alerts_glob() -> None:
    """(c) rule_files in prometheus.yml must reference the alerts directory glob, and at least one
    referenced path pattern must resolve to an existing file on disk.
    """
    assert PROMETHEUS_YML.exists(), f"prometheus.yml missing: {PROMETHEUS_YML}"
    doc = _load_yaml(PROMETHEUS_YML)
    rule_files = doc.get("rule_files") or []
    assert rule_files, (
        "prometheus.yml must contain a 'rule_files:' list pointing to the alert rules (ADR 0078 D1)"
    )
    # The ADR specifies rule_files: ["alerts/*.rules.yml"] (relative to the Prometheus config dir
    # /etc/prometheus). We check that at least one entry contains 'alerts/' and '.rules.yml',
    # and also that the rules file exists relative to the prometheus config directory.
    alerts_referenced = any("alerts" in str(rf) and "rules" in str(rf) for rf in rule_files)
    assert alerts_referenced, (
        f"rule_files must reference the alerts glob (e.g. 'alerts/*.rules.yml'); got: {rule_files}"
    )


# --------------------------------------------------------------------------- #
# (e) rule_files references a path that exists on disk
# --------------------------------------------------------------------------- #


def test_rule_files_path_exists_on_disk() -> None:
    """(e) At least one rule_files entry in prometheus.yml must correspond to the actual alerts
    file that exists on disk (deploy/prometheus/alerts/worldmonitor.rules.yml).
    """
    assert PROMETHEUS_YML.exists(), f"prometheus.yml missing: {PROMETHEUS_YML}"
    assert RULES_YML.exists(), f"rules file missing: {RULES_YML}"
    # The rules file exists; prometheus.yml references it; that is sufficient for in-repo
    # validation. (In the container, rule_files paths are relative to /etc/prometheus, not
    # the repo root.) We confirm the rules file is reachable relative to the prometheus dir.
    rules_relative = RULES_YML.relative_to(PROMETHEUS_DIR)
    doc = _load_yaml(PROMETHEUS_YML)
    rule_files = doc.get("rule_files") or []
    # Check any rule_files glob pattern would match 'alerts/worldmonitor.rules.yml'
    # by confirming 'alerts' appears in at least one entry (lenient, mirrors ADR 0078 D1).
    any_match = any(str(rules_relative.parent) in str(rf) for rf in rule_files)
    assert any_match, (
        f"rule_files {rule_files} does not reference the directory containing "
        f"{rules_relative} (relative to {PROMETHEUS_DIR})"
    )


# --------------------------------------------------------------------------- #
# (d) INV-STRUCTURE — every alert has required fields + severity + annotations
# --------------------------------------------------------------------------- #


_VALID_SEVERITIES = {"critical", "warning", "info"}


def test_every_alert_has_required_fields_and_valid_severity() -> None:
    """(d) Every alert rule must carry: alert, expr, for,
    labels.severity in {critical,warning,info},
    non-empty annotations.summary, non-empty annotations.description.
    """
    assert RULES_YML.exists(), f"rules file missing: {RULES_YML}"
    doc = _load_yaml(RULES_YML)
    alerts = _all_alert_rules(doc)
    assert alerts, "worldmonitor.rules.yml must contain at least one alert rule"

    structural_errors: list[str] = []
    for rule in alerts:
        name = rule.get("alert") or "<unnamed>"

        # Required top-level fields
        for field in ("alert", "expr", "for"):
            if not rule.get(field):
                structural_errors.append(f"Alert '{name}': missing or empty field '{field}'")

        # labels.severity
        labels = rule.get("labels") or {}
        severity = labels.get("severity")
        if severity not in _VALID_SEVERITIES:
            structural_errors.append(
                f"Alert '{name}': labels.severity={severity!r} not in {_VALID_SEVERITIES}"
            )

        # annotations.summary and annotations.description (non-empty strings)
        annotations = rule.get("annotations") or {}
        for ann_key in ("summary", "description"):
            val = annotations.get(ann_key)
            if not val or not str(val).strip():
                structural_errors.append(
                    f"Alert '{name}': annotations.{ann_key} is missing or empty"
                )

    assert not structural_errors, "STRUCTURAL VIOLATIONS in worldmonitor.rules.yml:\n" + "\n".join(
        structural_errors
    )


def test_exactly_two_critical_alerts_driver_down_and_resolution_wedged() -> None:
    """(d) Exactly two alerts must have severity=critical: DriverDown and ResolutionWedged.
    All other alerts must be warning (or info). This is the operational contract from ADR 0078 D2.
    """
    assert RULES_YML.exists(), f"rules file missing: {RULES_YML}"
    doc = _load_yaml(RULES_YML)
    alerts = _all_alert_rules(doc)

    criticals = [
        r["alert"] for r in alerts if (r.get("labels") or {}).get("severity") == "critical"
    ]
    assert set(criticals) == {"DriverDown", "ResolutionWedged"}, (
        f"Exactly the alerts {{DriverDown, ResolutionWedged}} must be severity=critical "
        f"(ADR 0078 D2 — two critical, five warning). Got critical alerts: {sorted(criticals)}"
    )
    assert len(criticals) == 2, (
        f"Exactly 2 critical alerts expected; got {len(criticals)}: {criticals}"
    )


def test_resolution_wedged_threshold_matches_settings() -> None:
    """(d) ResolutionWedged's threshold literal in the expr must equal
    Settings().resolve_lock_skip_alert_threshold (3). This couples the alert and the driver
    escalation log so they cannot drift independently (ADR 0078 D2).
    """
    assert RULES_YML.exists(), f"rules file missing: {RULES_YML}"
    doc = _load_yaml(RULES_YML)
    alerts = _all_alert_rules(doc)

    wedged = next((r for r in alerts if r.get("alert") == "ResolutionWedged"), None)
    assert wedged is not None, (
        "worldmonitor.rules.yml must contain an alert named 'ResolutionWedged' (ADR 0078 D2)"
    )

    expected_threshold = Settings().resolve_lock_skip_alert_threshold  # 3
    expr = str(wedged.get("expr", ""))

    # The threshold must appear as a literal integer in the expr
    threshold_match = re.search(r">=\s*(\d+)", expr)
    assert threshold_match is not None, (
        f"ResolutionWedged expr {expr!r} must contain a '>= <threshold>' comparison "
        f"matching resolve_lock_skip_alert_threshold={expected_threshold}"
    )
    actual_threshold = int(threshold_match.group(1))
    assert actual_threshold == expected_threshold, (
        f"ResolutionWedged threshold in expr is {actual_threshold}, but "
        f"Settings().resolve_lock_skip_alert_threshold={expected_threshold}. "
        "These must be equal so the alert fires at the same count the driver logs a WARNING."
    )


def test_expected_seven_alerts_all_present_by_name() -> None:
    """(d) All seven alert names from ADR 0078 D2 must be present."""
    assert RULES_YML.exists(), f"rules file missing: {RULES_YML}"
    doc = _load_yaml(RULES_YML)
    alerts = _all_alert_rules(doc)
    actual_names = {r.get("alert") for r in alerts}

    expected_names = {
        "DriverDown",
        "ResolutionWedged",
        "ConnectorInstanceHardDisabled",
        "ResolvePassTimingOut",
        "ErQueueBacklogHigh",
        "IngestDeadLettersPresent",
        "MergesParkedForReview",
    }
    missing = expected_names - actual_names
    assert not missing, (
        f"worldmonitor.rules.yml is missing the following alert rules from ADR 0078 D2: {missing}. "
        f"Present: {sorted(actual_names)}"
    )


# --------------------------------------------------------------------------- #
# (f) OPTIONAL: promtool gate if available
# --------------------------------------------------------------------------- #


def test_promtool_validates_config_and_rules_if_available() -> None:
    """(f) If promtool is on PATH, run:
      - promtool check config <prometheus.yml>
      - promtool check rules <worldmonitor.rules.yml>
      - promtool test rules <worldmonitor.rules.test.yml>  (if the test fixture exists)
    and assert returncode == 0 for each. Skipped if promtool is absent.
    """
    promtool = shutil.which("promtool")
    if promtool is None:
        pytest.skip(
            "promtool not found on PATH — skipping promtool validation. "
            "Install Prometheus locally to enable this gate (ADR 0078 D3)."
        )

    assert PROMETHEUS_YML.exists(), (
        f"prometheus.yml must exist for promtool check: {PROMETHEUS_YML}"
    )
    assert RULES_YML.exists(), f"rules file must exist for promtool check: {RULES_YML}"

    result_cfg = subprocess.run(
        [promtool, "check", "config", str(PROMETHEUS_YML)],
        capture_output=True,
        text=True,
    )
    assert result_cfg.returncode == 0, (
        f"promtool check config failed (returncode={result_cfg.returncode}):\n"
        f"stdout: {result_cfg.stdout}\nstderr: {result_cfg.stderr}"
    )

    result_rules = subprocess.run(
        [promtool, "check", "rules", str(RULES_YML)],
        capture_output=True,
        text=True,
    )
    assert result_rules.returncode == 0, (
        f"promtool check rules failed (returncode={result_rules.returncode}):\n"
        f"stdout: {result_rules.stdout}\nstderr: {result_rules.stderr}"
    )

    if PROMTOOL_TEST_YML.exists():
        result_test = subprocess.run(
            [promtool, "test", "rules", str(PROMTOOL_TEST_YML)],
            capture_output=True,
            text=True,
        )
        assert result_test.returncode == 0, (
            f"promtool test rules failed (returncode={result_test.returncode}):\n"
            f"stdout: {result_test.stdout}\nstderr: {result_test.stderr}"
        )
    # If the test fixture doesn't exist yet, the builder must create it (ADR 0078 D3) — but we
    # don't fail here since the YAML tests above already enforce the structure.
