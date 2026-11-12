"""The alert lifecycle: the plugin stamps age, resolution time, the severity
below and a severity rise on alert events and emits a recovered event when a
hardware component leaves the issue set; the rulebooks notify at closing,
route again on a rise and clear the LED on recovery; the runbook map falls
through code -> component type -> default, and code 13 reapplies NTP."""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


class Resp:
    def __init__(self, items):
        self.items = items
        self.status_code = 200
        self.total_item_count = len(items)


class Client:
    def __init__(self, alerts, hardware=()):
        self.alerts = alerts
        self.hardware = list(hardware)

    def get_alerts(self, **kw):
        if "names" in kw:
            return Resp([a for a in self.alerts if a["name"] in kw["names"]])
        return Resp([a for a in self.alerts if a["state"] != "closed"])

    def get_maintenance_windows(self):
        return Resp([])

    def get_hardware(self, **kw):
        return Resp(self.hardware)

    def get_drives(self, **kw):
        return Resp([])

    def get_controllers(self, **kw):
        return Resp([{"name": "CT0", "mode": "primary", "status": "ready"}])

    def get_support(self, **kw):
        return Resp([{"remote_assist_active": False, "phonehome_enabled": True}])

    def get_arrays(self, **kw):
        return Resp([{"version": "6.10.6"}])


class Arr:
    def __init__(self, client):
        self.client = client
        self.name = "array-a"


def _alert(name, state="open", updated=1, severity="warning", **extra):
    return {
        "name": name,
        "state": state,
        "updated": updated,
        "code": 42,
        "severity": severity,
        **extra,
    }


def test_alert_events_carry_the_lifecycle_fields(events):
    now = events._now_ms()
    client = Client([_alert("1", created=now - 300_000)])
    arr, state = Arr(client), {}
    initial = events.poll_alerts(arr, state, {})[0]["purefa_alert"]
    assert initial["transition"] == "initial"
    assert 295 <= initial["age_s"] <= 305
    assert initial["resolved_after_s"] is None
    assert initial["severity_down"] == "info" and initial["severity_raised"] is False
    # the severity rises: an updated event that says so and carries the old one
    client.alerts = [_alert("1", updated=2, severity="critical", created=now - 300_000)]
    (updated,) = [e["purefa_alert"] for e in events.poll_alerts(arr, state, {})]
    assert updated["transition"] == "updated"
    assert (
        updated["previous_severity"] == "warning" and updated["severity_raised"] is True
    )
    assert updated["severity_down"] == "warning"
    # it resolves: closing carries how long it was open
    client.alerts = [
        _alert(
            "1",
            state="closing",
            updated=3,
            severity="critical",
            created=now - 300_000,
            closed=now,
        )
    ]
    (closing,) = [e["purefa_alert"] for e in events.poll_alerts(arr, state, {})]
    assert closing["transition"] == "closing"
    assert closing["resolved_after_s"] == 300
    assert closing["severity_raised"] is False  # same severity as last poll
    # a drop in severity is not a rise
    assert events._severity_raised("critical", "warning") is False
    assert events._severity_raised(None, "warning") is False


def test_hardware_recovered_is_emitted_once_when_the_issue_clears(events):
    client = Client(
        [],
        hardware=[
            {
                "name": "CT1",
                "type": "controller",
                "status": "critical",
                "identify_enabled": False,
            }
        ],
    )
    arr, state = Arr(client), {}
    first = events.poll_health(arr, state, {})
    assert [e for e in first if "purefa_hardware_recovered" in e] == []
    assert any("purefa_hardware" in e for e in first)
    # the component comes back healthy with the LED still lit by the rulebook
    client.hardware = [
        {"name": "CT1", "type": "controller", "status": "ok", "identify_enabled": True}
    ]
    second = events.poll_health(arr, state, {})
    (recovered,) = [
        e["purefa_hardware_recovered"]
        for e in second
        if "purefa_hardware_recovered" in e
    ]
    assert (
        recovered["component"] == "CT1" and recovered["previous_status"] == "critical"
    )
    assert recovered["status"] == "ok" and recovered["identify_enabled"] is True
    assert recovered["maintenance_active"] is False
    # once: the next poll says nothing more about it
    third = events.poll_health(arr, state, {})
    assert [e for e in third if "purefa_hardware_recovered" in e] == []


def _rules(path):
    doc = yaml.safe_load((ROOT / path).read_text())
    return [r for rs in doc for r in rs.get("rules", [])]


def test_alerts_rulebook_notifies_at_closing_routes_a_rise_and_logs_closed():
    rules = {r["name"]: r for r in _rules("extensions/eda/rulebooks/alerts.yml")}
    names = list(rules)
    resolved = rules["Alert resolved - notify"]
    assert 'transition == "closing"' in resolved["condition"]
    extra = resolved["action"]["run_playbook"]["extra_vars"]
    assert extra["severity"] == "{{ event.purefa_alert.severity_down }}"
    assert "resolved_after_s" in extra["detail"]
    raised = rules["Alert severity raised - route through the runbook map again"]
    assert "severity_raised == true" in raised["condition"]
    assert raised["action"]["run_playbook"]["name"] == "playbooks/alert_runbook.yml"
    assert raised["action"]["run_playbook"]["extra_vars"]["transition"] == "updated"
    closed = rules["Alert closed - log"]
    assert (
        "debug" in closed["action"]
    )  # a day late; the resolved notice went out at closing
    assert names.index("Alert resolved - notify") < names.index("Alert closed - log")
    opened = rules["Alert opened - route through the runbook map"]["action"][
        "run_playbook"
    ]["extra_vars"]
    for key in ("alert_runbooks_by_type", "component_type", "baseline_ntp_servers_csv"):
        assert key in opened


def test_health_rulebook_clears_the_led_a_recovered_component_still_shows():
    rules = {r["name"]: r for r in _rules("extensions/eda/rulebooks/health.yml")}
    clear = rules["Hardware recovered - clear the identify LED"]
    assert "identify_enabled == true" in clear["condition"]
    assert "maintenance_active == false" in clear["condition"]
    extra = clear["action"]["run_playbook"]["extra_vars"]
    assert clear["action"]["run_playbook"]["name"] == "playbooks/identify_led.yml"
    assert extra["led_state"] is False and extra["severity"] == "info"
    assert "Hardware recovered - log" in rules
    example = yaml.safe_load((ROOT / "vars" / "example.yml").read_text())
    assert example["alert_runbooks_by_type"] == {}
    assert example["alert_runbooks"]["13"]["route"] == "tasks"


def test_runbook_map_falls_through_and_code_13_reapplies_ntp():
    play = yaml.safe_load((ROOT / "playbooks" / "alert_runbook.yml").read_text())[0]
    pick = next(
        t for t in play["tasks"] if t["name"].startswith("Pick the runbook entry")
    )
    expr = pick["ansible.builtin.set_fact"]["runbook"]
    assert "alert_runbooks[alert_code | string]" in expr
    assert "alert_runbooks_by_type | default({})" in expr
    assert "alert_runbooks['default']" in expr
    tasks = yaml.safe_load((ROOT / "playbooks" / "runbooks" / "13.yml").read_text())
    ntp = next(t for t in tasks if "everpure.flasharray.purefa_ntp" in t)
    assert ntp["when"] == "baseline_ntp_servers_csv | default('') | length > 0"
    args = ntp["everpure.flasharray.purefa_ntp"]
    assert args["fa_url"] == "{{ hostvars[target_array].fa_url }}"
    assert args["api_token"] == "{{ hostvars[target_array].api_token }}"
    assert "baseline_ntp_servers_csv.split(',')" in args["ntp_servers"]
    assert args["state"] == "present"
