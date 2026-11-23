# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The protection posture, host connectivity, certificate, KMIP and alert
watcher rules: present by name in the standalone rulebooks and their
generated platform twins, comparing the fields the plugin's events carry
(tests/fixtures/protection.yml holds a sample of each event), with the
designed severities, throttles and order (critical before warning, the
recovery rule where the degraded rules cannot shadow it), and every variable
they read is a documented key of vars/example.yml."""

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
RULEBOOKS = "extensions/eda/rulebooks"
EVENT_FIELD = re.compile(r"event\.(purefa_[a-z_]+)\.([a-z_]+)")
VAR_REF = re.compile(r"\bvars\.([a-z_][a-z0-9_]*)")
JINJA_REF = re.compile(r"{{\s*([a-z_][a-z0-9_]*)\b")
NOTIFY_VARS = {"severity", "subject", "detail", "target_array", "alert_log"}
EVENT_KINDS = {
    "purefa_protection_finding",
    "purefa_host_connectivity",
    "purefa_certificate",
    "purefa_kmip",
    "purefa_config",
}

# rule name -> rulebook, event kind, severity, once_within, group_by fields
# (None, None: no throttle)
RULES = {
    "Volumes without protection - notify": (
        "replication.yml",
        "purefa_protection_finding",
        "warning",
        "24 hours",
        ["array"],
    ),
    "Volumes with local snapshots only - notify": (
        "replication.yml",
        "purefa_protection_finding",
        "warning",
        "24 hours",
        ["array"],
    ),
    "Protection groups without SafeMode - notify": (
        "replication.yml",
        "purefa_protection_finding",
        "warning",
        "24 hours",
        ["array"],
    ),
    "Protection group retention below the minimum - notify": (
        "replication.yml",
        "purefa_protection_finding",
        "warning",
        "24 hours",
        ["array"],
    ),
    "Protection group members removed - notify": (
        "replication.yml",
        "purefa_protection_finding",
        "warning",
        "1 hour",
        ["array"],
    ),
    "Host paths critical - notify": (
        "health.yml",
        "purefa_host_connectivity",
        "critical",
        "6 hours",
        ["array", "host"],
    ),
    "Host paths degraded - notify": (
        "health.yml",
        "purefa_host_connectivity",
        "warning",
        "6 hours",
        ["array", "host"],
    ),
    "Host paths recovered - notify": (
        "health.yml",
        "purefa_host_connectivity",
        "info",
        None,
        None,
    ),
    "Certificate about to expire or expired - notify": (
        "config_drift.yml",
        "purefa_certificate",
        "critical",
        "24 hours",
        ["array", "name"],
    ),
    "Certificate expiring - notify": (
        "config_drift.yml",
        "purefa_certificate",
        "warning",
        "24 hours",
        ["array", "name"],
    ),
    "KMIP server unreachable - notify": (
        "config_drift.yml",
        "purefa_kmip",
        "critical",
        "1 hour",
        ["array", "name"],
    ),
    "No enabled alert watcher - notify": (
        "config_drift.yml",
        "purefa_config",
        "warning",
        "24 hours",
        ["array"],
    ),
    "Alert watchers differ from the baseline - notify": (
        "config_drift.yml",
        "purefa_config",
        "warning",
        "24 hours",
        ["array"],
    ),
}

# the comparisons each condition makes (the contract fields of the design)
CONDITIONS = {
    "Volumes without protection - notify": ['finding == "unprotected"'],
    "Volumes with local snapshots only - notify": [
        "vars.protection_require_replication == true",
        'finding == "local_only"',
    ],
    "Protection groups without SafeMode - notify": [
        "vars.protection_require_safemode == true",
        'finding == "unlocked"',
    ],
    "Protection group retention below the minimum - notify": [
        'finding == "short_retention"'
    ],
    "Protection group members removed - notify": ['finding == "members_removed"'],
    "Host paths critical - notify": [
        "host is defined",
        'status == "critical"',
        "recovered == false",
    ],
    "Host paths degraded - notify": ["recovered == false", 'status != "healthy"'],
    "Host paths recovered - notify": ["recovered == true"],
    "Certificate about to expire or expired - notify": [
        "days_left is defined",
        "days_left <= vars.certificate_critical_days",
    ],
    "Certificate expiring - notify": ["days_left <= vars.certificate_warn_days"],
    "KMIP server unreachable - notify": ["ok == false"],
    "No enabled alert watcher - notify": [
        "vars.alert_watchers_required == true",
        "alert_watchers_enabled_count is defined",
        "alert_watchers_enabled_count == 0",
    ],
    "Alert watchers differ from the baseline - notify": [
        'vars.baseline_alert_watchers_csv != ""',
        "alert_watchers_csv is defined",
        "alert_watchers_csv != vars.baseline_alert_watchers_csv",
    ],
}

# silent inside a maintenance window; the other host rules and the config
# events (which carry no maintenance_active) are not gated
MAINTENANCE_GATED = {
    "Host paths degraded - notify",
    "Volumes without protection - notify",
    "Volumes with local snapshots only - notify",
    "Protection groups without SafeMode - notify",
    "Protection group retention below the minimum - notify",
    "Protection group members removed - notify",
    "Host paths critical - notify",
}

POSTURE_ORDER = [
    "Volumes without protection - notify",
    "Volumes with local snapshots only - notify",
    "Protection groups without SafeMode - notify",
    "Protection group retention below the minimum - notify",
    "Protection group members removed - notify",
]

# vars/example.yml keys the rules and the digest tick rule read, with the
# documented defaults
NEW_VARS = {
    "protection_interval": 600,
    "protection_require_safemode": False,
    "protection_require_replication": False,
    "protection_min_retention_days": 7,
    "certificate_warn_days": 30,
    "certificate_critical_days": 7,
    "alert_watchers_required": True,
    "baseline_alert_watchers_csv": "",
    "digest_interval_s": 900,
    "digest_window_minutes": 15,
}


def _doc(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def _rules(path):
    return {r["name"]: r for rs in _doc(path) for r in rs.get("rules", [])}


def _names(path):
    return [r["name"] for rs in _doc(path) for r in rs.get("rules", [])]


def _rule(name, platform=False):
    rulebook = RULES[name][0]
    prefix = "platform/" if platform else ""
    rules = _rules(f"{RULEBOOKS}/{prefix}{rulebook}")
    assert name in rules, f"{prefix}{rulebook} has no rule '{name}'"
    return rules[name]


@pytest.fixture(scope="module")
def example_vars():
    return _doc("vars/example.yml")


@pytest.fixture(scope="module")
def samples():
    """The fixture's events by kind (several host connectivity events: the
    degraded ones and the recovered one)."""
    out = {}
    for event in _doc("tests/fixtures/protection.yml")["fixture_events"]:
        ((kind, body),) = event.items()
        out.setdefault(kind, []).append(body)
    return out


def test_the_design_names_thirteen_rules():
    assert len(RULES) == 13
    assert set(CONDITIONS) == set(RULES)
    assert {RULES[n][1] for n in RULES} == EVENT_KINDS


@pytest.mark.parametrize("name", sorted(RULES))
def test_rule_notifies_the_same_way_in_both_forms(name):
    rulebook, kind, severity, _, _ = RULES[name]
    rule = _rule(name)
    run = rule["action"]["run_playbook"]
    assert run["name"] == "playbooks/notify.yml"
    extra = run["extra_vars"]
    assert set(extra) == NOTIFY_VARS
    assert extra["severity"] == severity
    assert extra["target_array"] == "{{ event.%s.array }}" % kind
    assert extra["alert_log"] == "{{ alert_log }}"
    # the subject names the array once, as the sibling rules do; the detail
    # names the items from the event
    assert extra["subject"].count(f"event.{kind}.array") == 1
    assert f"event.{kind}." in extra["detail"]
    platform = _rule(name, platform=True)
    job = platform["action"]["run_job_template"]
    assert job["name"] == "FlashArray - notify"
    assert job["job_args"]["extra_vars"] == {
        k: v for k, v in extra.items() if k != "alert_log"
    }
    assert platform["condition"] == rule["condition"]
    assert platform.get("throttle") == rule.get("throttle")


@pytest.mark.parametrize("name", sorted(RULES))
def test_throttle_is_as_designed(name):
    _, kind, _, once_within, group_by = RULES[name]
    rule = _rule(name)
    if once_within is None:
        assert "throttle" not in rule, f"{name} is not throttled by design"
        return
    assert rule["throttle"]["once_within"] == once_within
    assert rule["throttle"]["group_by_attributes"] == [
        f"event.{kind}.{field}" for field in group_by
    ]


@pytest.mark.parametrize("name", sorted(RULES))
def test_condition_compares_the_contract_fields(name, samples):
    _, kind, _, _, _ = RULES[name]
    rule = _rule(name)
    condition = " ".join(rule["condition"].split())
    for fragment in CONDITIONS[name]:
        assert fragment in condition, (name, fragment)
    if name in MAINTENANCE_GATED:
        assert f"event.{kind}.maintenance_active == false" in condition
    else:
        assert "maintenance_active" not in condition
    # every event field the rule reads, in the condition or the message, is
    # on the event kind the rule is about and present in the fixture sample
    extra = rule["action"]["run_playbook"]["extra_vars"]
    fields = set(
        EVENT_FIELD.findall(" ".join([condition, extra["subject"], extra["detail"]]))
    )
    assert fields, name
    assert {k for k, _ in fields} == {kind}
    carried = set().union(*(set(sample) for sample in samples[kind]))
    missing = sorted(field for _, field in fields if field not in carried)
    assert not missing, f"{name}: tests/fixtures/protection.yml lacks {missing}"


def test_critical_precedes_warning_and_recovery_cannot_be_shadowed():
    """First match wins: the critical rule sits before the warning one whose
    condition it also satisfies; the host recovery event carries
    recovered: true, which both degraded rules exclude. The watcher rules
    read purefa_config like the drift rules and follow them; the posture
    rules follow the protection group rules in the design's order."""
    for prefix in ("", "platform/"):
        health = _names(f"{RULEBOOKS}/{prefix}health.yml")
        critical, warning, recovered = (
            health.index(n)
            for n in (
                "Host paths critical - notify",
                "Host paths degraded - notify",
                "Host paths recovered - notify",
            )
        )
        assert critical < warning < recovered, prefix
        config = _names(f"{RULEBOOKS}/{prefix}config_drift.yml")
        assert config.index(
            "Certificate about to expire or expired - notify"
        ) < config.index("Certificate expiring - notify"), prefix
        assert (
            config.index("Admin authentication settings drift - notify only")
            < config.index("No enabled alert watcher - notify")
            < config.index("Alert watchers differ from the baseline - notify")
        ), prefix
        replication = _names(f"{RULEBOOKS}/{prefix}replication.yml")
        posture = [n for n in replication if n in POSTURE_ORDER]
        assert posture == POSTURE_ORDER, prefix
        assert replication.index(
            "Protection group snapshot schedule disabled with members - notify"
        ) < replication.index(POSTURE_ORDER[0]), prefix
    for name in ("Host paths critical - notify", "Host paths degraded - notify"):
        assert "recovered == false" in _rule(name)["condition"]
    assert "recovered == true" in _rule("Host paths recovered - notify")["condition"]


def test_the_posture_rules_have_their_own_ruleset_that_matches_every_rule():
    """One posture event per array carries every finding, so the posture
    ruleset lets each rule match it, and the protection poll fails into it
    alone, so it carries the poll-error rules too; the replication ruleset
    keeps the replication signal alone and first-match-wins."""
    for path in (
        f"{RULEBOOKS}/replication.yml",
        f"{RULEBOOKS}/platform/replication.yml",
    ):
        doc = _doc(path)
        replication = next(rs for rs in doc if rs["name"] == "FlashArray replication")
        (source,) = replication["sources"]
        args = source["sdodsley.flasharray_eda.purefa_events"]
        assert args["signals"] == ["replication"], path
        assert "protection_interval" not in args, path
        posture = next(
            rs for rs in doc if rs["name"] == "FlashArray protection posture"
        )
        assert "match_multiple_rules" not in posture, path
        (source,) = posture["sources"]
        args = source["sdodsley.flasharray_eda.purefa_events"]
        assert args["signals"] == ["protection"], path
        assert args["protection_interval"] == "{{ protection_interval }}", path
        assert (
            args["protection_min_retention_days"]
            == "{{ protection_min_retention_days }}"
        ), path
        names = [r["name"] for r in posture["rules"]]
        assert names == [
            "Poll failing for many polls - escalate",
            "Poll failed - notify once",
            "Volumes without protection - notify",
            "Volumes with local snapshots only - notify",
            "Protection groups without SafeMode - notify",
            "Protection group retention below the minimum - notify",
            "Protection group members removed - notify",
        ], path
    for rulebook, signal in (("health.yml", "health"), ("config_drift.yml", "config")):
        (ruleset,) = _doc(f"{RULEBOOKS}/{rulebook}")
        args = ruleset["sources"][0]["sdodsley.flasharray_eda.purefa_events"]
        assert args["signals"] == [signal], rulebook


def test_every_variable_the_new_rules_read_is_in_the_example_vars(example_vars):
    for name in RULES:
        rule = _rule(name)
        referenced = set(VAR_REF.findall(rule["condition"]))
        for text in rule["action"]["run_playbook"]["extra_vars"].values():
            referenced |= {n for n in JINJA_REF.findall(str(text)) if n != "event"}
        missing = sorted(referenced - set(example_vars))
        assert not missing, f"{name} reads vars not in vars/example.yml: {missing}"


def test_example_vars_carry_the_new_keys_with_their_defaults(example_vars):
    for key, value in NEW_VARS.items():
        assert key in example_vars, key
        assert example_vars[key] == value, key
        # a switch is a boolean, a threshold an int, the baseline a string
        assert type(example_vars[key]) is type(value), key
    assert (
        example_vars["certificate_critical_days"]
        < example_vars["certificate_warn_days"]
    )


def test_fixture_holds_a_recorded_shape_sample_of_each_new_event(samples):
    # the per-array summary stays in the fixture as a documented shape
    assert set(samples) == EVENT_KINDS | {"purefa_protection_posture"}
    for kind, bodies in samples.items():
        for body in bodies:
            assert body["array"] == "array-a", kind
    (posture,) = samples["purefa_protection_posture"]
    assert posture["unprotected_count"] == len(posture["unprotected"])
    assert posture["unprotected_csv"] == ",".join(sorted(posture["unprotected"]))
    assert posture["members_removed_count"] == len(posture["members_removed"])
    hosts = samples["purefa_host_connectivity"]
    assert {h["recovered"] for h in hosts} == {True, False}
    # hosts without volumes are not paged, so every sample has connections
    assert all(h["connection_count"] > 0 for h in hosts)
    assert {h["status"] for h in hosts if h["recovered"]} == {"healthy"}
    assert "healthy" not in {h["status"] for h in hosts if not h["recovered"]}
    for event in (posture, *hosts):
        assert event["maintenance_active"] is False
    certificates = samples["purefa_certificate"]
    assert all(c["days_left"] <= 30 for c in certificates)
    assert any(c["days_left"] <= 7 for c in certificates)
    (kmip,) = samples["purefa_kmip"]
    assert kmip["tested"] is True and kmip["ok"] is False and kmip["failures"]
    (config,) = samples["purefa_config"]
    watchers = config["alert_watchers"]
    assert config["alert_watcher_count"] == len(watchers)
    assert config["alert_watchers_csv"] == ",".join(sorted(w["name"] for w in watchers))
    assert config["alert_watchers_enabled_count"] == sum(
        1 for w in watchers if w["enabled"]
    )
    assert config["alert_watchers_enabled_count"] == len(
        config["alert_watchers_enabled"]
    )
