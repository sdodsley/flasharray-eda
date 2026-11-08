# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
purefa_events.py - ansible-rulebook event source for Everpure FlashArray.

Polls one or more FlashArrays over REST (py-pure-client) and emits plain
fact events.  Thresholds, baselines and policy live in the rulebook, not here.
One instance can run several signals, each on its own interval.

Verified against Purity//FA 6.10.6 (REST 2.46) with py-pure-client 1.94.1.

Arguments
---------
arrays            list of arrays (required). Each entry:
                    name          event `array` field and inventory host name
                    fa_url        https://<management address>, or
                    fa_url_env    name of an environment variable holding it
                    api_token     API token, or
                    api_token_env name of an environment variable holding it
                  A JSON string (or a YAML string) that decodes to that list
                  is accepted too, so a platform credential can inject the
                  list as one variable; it is decoded before validation and
                  never echoed into an error or a log.
audit_batch       audits: records fetched per page (default 200). Up to 10
                  pages are read when more than that arrived between polls;
                  beyond that the oldest are skipped and a purefa_poll_error
                  (signal audits, "audit_batch overflow") says so.
signals           list or comma-separated string, default: all of
                  alerts | audits | health | telemetry | replication | config
interval          default seconds between polls (default 60)
<signal>_interval per-signal override, e.g. audits_interval: 15
verify_ssl        verify the array certificate (default false)
request_timeout   seconds to wait for each REST response (default 30; the
                  connect timeout is fixed at 10). A hung connection then
                  fails that poll with purefa_poll_error instead of holding
                  the array's lock for ever. Keep it well under the shortest
                  signal interval: the signals on one array are serialised
                  and each signal polls its arrays in turn, so one cycle can
                  take arrays x (10 + request_timeout) seconds at worst.
automation_users  list or comma-separated string of user names whose audit
                  records count as automation (default: empty). Audit events
                  carry `by_automation`.
audit_lookback    seconds of audit history to replay at start-up (default 0)
include_initial_alerts
                  emit alerts that are already open at start-up with
                  transition "initial" (default true)
hardware_ignore_status
                  hardware statuses that are not issues
                  (default: ok, not_installed, unused)
drive_ignore_status
                  drive statuses that are not issues (default: healthy,
                  unused, empty, evacuated)
latency_threshold_usec
                  telemetry: a volume at or above this read or write latency
                  increments its high_latency_polls counter (default 2000)
min_iops          telemetry: ignore volumes doing fewer IOPS (default 1)
top_volumes       telemetry: how many of the slowest volumes to fetch per
                  array per poll (default 10, server-side sorted). Volumes
                  carrying the eda/qos_cap tag are always reported, whatever
                  their rank or IOPS, so the lift rule keeps seeing them.
top_growers       telemetry: how many fastest-growing volumes to report
                  (default 5)
growth_volumes    telemetry: growth is tracked across this many of the
                  largest live volumes by physical space (default 500); a
                  small volume outside that set never appears in top_growers
baseline          telemetry: also fetch the same half hour one week ago
                  from the array's own history (default true)
drr_drop_threshold_pct
                  telemetry: data reduction this much below a week ago sets
                  drr_drop (default 10); drr_drop_pct is accepted as an
                  alias.
snapshot_growth_threshold_pct
                  telemetry: snapshot space this much above a week ago sets
                  snapshot_surge (default 25); snapshot_growth_pct is
                  accepted as an alias.
write_surge_ratio telemetry: write bandwidth this many times last week's
                  sets write_surge (default 3)
rpo_seconds       replication: informational; copied onto replica-link
                  events so rules can compare lag with it (default 0 = none)
stuck_transfer_polls
                  replication: an in-progress transfer whose progress and
                  bytes transferred have not moved for this many polls sets
                  `stuck` (default 3)

Events (one dict per event, exactly one top-level key, always with `array`;
alert, audit, hardware and replication events also carry
`maintenance_active`, true while the array has an open maintenance window)
-------------------------------------------------------------------------
purefa_alert            transition: initial|opened|updated|closing|closed;
                        alert_id, code, severity, state, component_type,
                        component_name, summary, description, issue, actual,
                        expected, action, knowledge_base_url, flagged,
                        created, updated, category
purefa_audit            audit_id, time, user, user_interface, origin, command,
                        subcommand, arguments, by_automation, system_user
purefa_health           hardware_issues[{component,type,status}],
                        hardware_issue_count,
                        drive_issues[{drive,status,type}], drive_issue_count,
                        controllers[{name,mode,status,mode_since}],
                        primary_controller, maintenance_active,
                        maintenance_windows[{name,created,expires}],
                        remote_assist_active, phonehome_enabled,
                        open_alert_count, purity_version
purefa_hardware         one per hardware component whose status is an issue:
                        component, type, status, details, slot, serial,
                        identify_enabled
purefa_controller_failover
                        previous_primary, primary,
                        controllers[{name,mode,status}]
purefa_capacity         time, capacity_bytes, used_bytes, used_pct,
                        data_reduction, total_reduction, snapshot_bytes,
                        growth_bytes_per_day, days_to_full,
                        days_to_full_known,
                        top_growers[{volume,delta_bytes,total_physical}],
                        pending_eradication_volumes,
                        pending_eradication_snapshots, drr_week_ago,
                        drr_drop_pct, drr_drop_poll_pct, snapshot_growth_pct,
                        snapshot_growth_poll_pct, drr_drop, snapshot_surge,
                        others[{array,used_pct}] (the other polled arrays,
                        least used first)
purefa_array_performance
                        time, usec_per_read_op, usec_per_write_op,
                        reads_per_sec, writes_per_sec, read_bytes_per_sec,
                        write_bytes_per_sec, queue_depth; when the baseline
                        fetch succeeds also baseline_usec_per_read_op,
                        baseline_usec_per_write_op, baseline_reads_per_sec,
                        baseline_writes_per_sec, read_latency_ratio,
                        write_latency_ratio, write_bw_ratio; always
                        baseline_known, write_surge
purefa_volume_performance
                        volume, usec_per_read_op, usec_per_write_op,
                        reads_per_sec, writes_per_sec, iops_share_pct,
                        high_latency_polls, normal_polls, qos_iops_limit,
                        qos_bw_limit, eda_capped (volume carries the
                        eda/qos_cap tag set by the QoS playbook),
                        eda_cap_since, array_usec_per_read_op,
                        array_usec_per_write_op
purefa_array_connection remote, status, type, replication_transport,
                        paths_total, paths_connected,
                        failed_paths[{local,remote,status}], throttle,
                        remote_version
purefa_replica_link     local_pod, remote_pod, remote_array, status,
                        direction, paused, lag_ms, lag_s, recovery_point,
                        recovery_point_age_s, rpo_seconds, rpo_breached,
                        rpo_breach_polls, paused_for_s
purefa_stretched_pod    pod, members[{array,status,mediator_status,
                        pre_elected}], online_arrays[], offline_arrays[],
                        all_online, mediator_online, recovered (first
                        all-online poll after a degraded one)
purefa_pg_replication   pgroup, targets[], schedule_enabled, frequency_ms,
                        last_completed, since_last_completed_s, overdue,
                        in_progress{name,progress,data_transferred},
                        stuck_polls, stuck
purefa_config           purity_version, ntp_servers(_csv), dns_domain,
                        dns_nameservers(_csv), ldap_enabled, ldap_uris(_csv),
                        ldap_base_dn, ldap_bind_user, manual_eradication,
                        eradication_delay_ms, eradication_disabled_delay_ms,
                        lockout_duration_ms, max_login_attempts,
                        min_password_length, sso_enabled,
                        syslog_servers(_csv), smtp_relay_host,
                        alert_watchers_enabled(_csv), phonehome_enabled
purefa_poll_error       signal, error (one per failed poll; the audits
                        signal also raises it on audit_batch overflow)
"""

DOCUMENTATION = r"""
---
name: purefa_events
short_description: Poll Everpure FlashArrays over REST and emit fact events.
description:
  - Polls one or more FlashArrays with py-pure-client and emits plain fact
    events (C(purefa_alert), C(purefa_audit), C(purefa_health),
    C(purefa_capacity), C(purefa_volume_performance), C(purefa_replica_link),
    C(purefa_pg_replication), C(purefa_config) and others). Thresholds and
    policy live in the rulebook. One instance runs several signals, each on
    its own interval. The module docstring lists every event and its fields.
options:
  arrays:
    description:
      - Arrays to poll. Each entry has C(name) (the event's array field and
        inventory host name), C(fa_url) or C(fa_url_env), and C(api_token) or
        C(api_token_env); the C(_env) forms name environment variables.
      - A JSON string is accepted so a platform credential can inject the
        list as one extra var (a YAML string decodes the same way); the
        string is decoded before validation and is never written to a log.
    type: list
    elements: dict
    required: true
  signals:
    description: Signals to run (list or comma-separated string).
    type: list
    elements: str
    choices: [alerts, audits, health, telemetry, replication, config]
    default: [alerts, audits, health, telemetry, replication, config]
  interval:
    description: Default seconds between polls.
    type: int
    default: 60
  alerts_interval:
    description: Poll interval for the alerts signal; overrides I(interval).
    type: int
  audits_interval:
    description: Poll interval for the audits signal; overrides I(interval).
    type: int
  health_interval:
    description: Poll interval for the health signal; overrides I(interval).
    type: int
  telemetry_interval:
    description: Poll interval for the telemetry signal; overrides I(interval).
    type: int
  replication_interval:
    description: Poll interval for the replication signal; overrides I(interval).
    type: int
  config_interval:
    description: Poll interval for the config signal; overrides I(interval).
    type: int
  verify_ssl:
    description: Verify the array certificate.
    type: bool
    default: false
  request_timeout:
    description:
      - Seconds to wait for each REST response; the connect timeout is fixed
        at 10. Keep it well under the shortest signal interval.
    type: int
    default: 30
  automation_users:
    description: User names whose audit records count as automation (list or comma-separated string).
    type: list
    elements: str
    default: []
  audit_batch:
    description:
      - Audit records fetched per page; up to 10 pages are read per poll,
        then a purefa_poll_error reports the overflow.
    type: int
    default: 200
  audit_lookback:
    description: Seconds of audit history to replay at start-up.
    type: int
    default: 0
  include_initial_alerts:
    description: Emit alerts already open at start-up with transition initial.
    type: bool
    default: true
  hardware_ignore_status:
    description: Hardware statuses that are not issues.
    type: list
    elements: str
    default: [ok, not_installed, unused]
  drive_ignore_status:
    description: Drive statuses that are not issues.
    type: list
    elements: str
    default: [healthy, unused, empty, evacuated]
  latency_threshold_usec:
    description: A volume at or above this read or write latency counts a high-latency poll.
    type: int
    default: 2000
  min_iops:
    description: Ignore volumes doing fewer IOPS (capped volumes are always reported).
    type: float
    default: 1
  top_volumes:
    description: Slowest volumes fetched per array per poll, server-side sorted.
    type: int
    default: 10
  top_growers:
    description: Fastest-growing volumes reported on the capacity event.
    type: int
    default: 5
  growth_volumes:
    description: Growth is tracked across this many of the largest live volumes.
    type: int
    default: 500
  baseline:
    description: Also fetch the same half hour one week ago from the array's history.
    type: bool
    default: true
  drr_drop_threshold_pct:
    description: Data reduction this much below a week ago sets drr_drop.
    type: float
    default: 10
  snapshot_growth_threshold_pct:
    description: Snapshot space this much above a week ago sets snapshot_surge.
    type: float
    default: 25
  write_surge_ratio:
    description: Write bandwidth this many times last week's sets write_surge.
    type: float
    default: 3
  rpo_seconds:
    description: RPO copied onto replica-link events; 0 means none.
    type: int
    default: 0
  stuck_transfer_polls:
    description: An in-progress transfer unchanged for this many polls sets stuck.
    type: int
    default: 3
"""

EXAMPLES = r"""
- name: FlashArray alerts and health
  hosts: all
  sources:
    - sdodsley.flasharray_eda.purefa_events:
        arrays: "{{ purefa_arrays }}"
        signals: [alerts, health]
        alerts_interval: 60
        health_interval: 300
        automation_users: "{{ automation_users }}"
  rules:
    - name: Critical alert opened
      condition: >-
        event.purefa_alert.transition == "opened" and
        event.purefa_alert.severity == "critical"
      action:
        debug:
          msg: "{{ event.purefa_alert.array }}: {{ event.purefa_alert.summary }}"
"""

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import urllib3
from pypureclient import flasharray

try:
    import yaml
except ImportError:  # ansible-rulebook ships PyYAML; a bare interpreter may not
    yaml = None

logger = logging.getLogger(__name__)

SIGNALS = ("alerts", "audits", "health", "telemetry", "replication", "config")
DAY_MS = 86_400_000
HALF_HOUR_MS = 1_800_000
CONNECT_TIMEOUT = 10  # seconds to open the connection; the read timeout is an argument
AUDIT_MAX_PAGES = (
    10  # pages of audit_batch rows read per poll before reporting overflow
)
PG_TRANSFER_ROWS = 500  # transfer rows fetched per protection group (bounds a backlog)
USER_AGENT = "flasharray-eda/purefa_events"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _as_list(value):
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)


def _decode_arrays(value, plugin):
    """The arrays argument as the structure the plugin documents.

    A platform credential injects the list (or mapping) as one string
    variable, so a JSON string, or a YAML string, is decoded here before the
    argument is validated. The text is never echoed into an error message or
    a log record: it carries API tokens.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if not isinstance(decoded, (list, dict)) and yaml is not None:
        try:
            decoded = yaml.safe_load(text)
        except yaml.YAMLError:
            decoded = None
    if isinstance(decoded, (list, dict)):
        return decoded
    raise ValueError(
        f"{plugin}: arrays is a string that does not decode as JSON (or YAML) "
        "into the documented structure; the text is not shown because it may "
        "hold API tokens"
    )


def _truthy(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _csv(values):
    return ",".join(sorted(str(v) for v in (values or [])))


def _items(resp, what):
    """Return the response items as dicts, or raise with the array's message."""
    if resp is None or not hasattr(resp, "items"):
        errs = getattr(resp, "errors", None) or []
        msg = "; ".join(getattr(e, "message", str(e)) for e in errs) or str(resp)
        raise RuntimeError(f"{what}: {msg}")
    out = []
    for item in resp.items:
        out.append(item.to_dict() if hasattr(item, "to_dict") else dict(item))
    return out


def _total(resp):
    return getattr(resp, "total_item_count", None)


def _first(items, default=None):
    return items[0] if items else (default if default is not None else {})


def _name(ref):
    if isinstance(ref, dict):
        return ref.get("name")
    return ref


def _avg(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 1) if vals else None


def _ratio(now, base):
    if isinstance(now, (int, float)) and isinstance(base, (int, float)) and base > 0:
        return round(now / base, 2)
    return None


def _now_ms():
    return int(time.time() * 1000)


def _poll_error(signal, array, error):
    return {
        "purefa_poll_error": {"signal": signal, "array": array, "error": str(error)}
    }


def _maintenance(arr, state, max_age=20.0):
    """Active maintenance windows on the array, cached briefly across signals.

    The alert, audit, health and replication signals stamp
    `maintenance_active` on their events so a rule can suppress remediation
    while someone has declared work on the array, without cross-signal
    facts in the rulebook; the telemetry and config events carry no flag.
    """
    cache = state.setdefault("maintenance", {}).setdefault(
        arr.name, {"at": 0, "windows": []}
    )
    if time.monotonic() - cache["at"] > max_age:
        cache["windows"] = _items(
            arr.client.get_maintenance_windows(), "maintenance windows"
        )
        cache["at"] = time.monotonic()
    return cache["windows"]


# --------------------------------------------------------------------------
# array handle: one SDK client per array, rebuilt after a failure
# --------------------------------------------------------------------------
class Array:
    def __init__(self, spec, verify_ssl, request_timeout=30):
        self.name = spec["name"]
        url = spec.get("fa_url") or spec.get("url")
        if not url and spec.get("fa_url_env"):
            url = os.environ.get(spec["fa_url_env"])
        if not url:
            raise ValueError(f"array {self.name}: fa_url or fa_url_env is required")
        self.host = url.replace("https://", "").replace("http://", "").strip("/")
        token = spec.get("api_token")
        if not token and spec.get("api_token_env"):
            token = os.environ.get(spec["api_token_env"])
        if not token:
            raise ValueError(
                f"array {self.name}: api_token or api_token_env is required"
            )
        self._token = token
        self._verify = verify_ssl
        self._timeout = (CONNECT_TIMEOUT, float(request_timeout))
        self._client = None
        self.lock = asyncio.Lock()

    @property
    def client(self):
        if self._client is None:
            # the timeout also bounds the version probe Client() makes, so an
            # unreachable array fails fast here instead of hanging the worker
            self._client = flasharray.Client(
                target=self.host,
                api_token=self._token,
                verify_ssl=self._verify,
                timeout=self._timeout,
                user_agent=USER_AGENT,
            )
        return self._client

    def reset(self):
        self._client = None


# --------------------------------------------------------------------------
# signal pollers: synchronous, run in a worker thread; return a list of events
# --------------------------------------------------------------------------
def poll_alerts(arr, state, args):
    c = arr.client
    st = state.setdefault("alerts", {}).setdefault(arr.name, {"seen": None})
    active = _items(c.get_alerts(filter="state!='closed'"), "alerts")
    seen = st["seen"]
    first = seen is None
    current = {}
    events = []

    maintenance_active = bool(_maintenance(arr, state))

    def event(a, transition):
        return {
            "purefa_alert": {
                "array": arr.name,
                "transition": transition,
                "maintenance_active": maintenance_active,
                "alert_id": a.get("name"),
                "code": a.get("code"),
                "severity": a.get("severity"),
                "state": a.get("state"),
                "component_type": a.get("component_type"),
                "component_name": a.get("component_name"),
                "summary": a.get("summary"),
                "description": a.get("description"),
                "issue": a.get("issue"),
                "actual": a.get("actual"),
                "expected": a.get("expected"),
                "action": a.get("action"),
                "knowledge_base_url": a.get("knowledge_base_url"),
                "flagged": a.get("flagged"),
                "category": a.get("category"),
                "created": a.get("created"),
                "updated": a.get("updated"),
            }
        }

    for a in active:
        aid = a.get("name")
        key = (a.get("state"), a.get("updated"))
        current[aid] = key
        if first:
            if _truthy(args.get("include_initial_alerts"), True):
                events.append(event(a, "initial"))
        elif aid not in seen:
            events.append(event(a, "opened"))
        elif seen[aid] != key:
            events.append(
                event(a, "closing" if a.get("state") == "closing" else "updated")
            )
    if not first:
        for aid in set(seen) - set(current):
            final = _first(
                _items(c.get_alerts(names=[aid]), "alert"),
                {"name": aid, "state": "closed"},
            )
            events.append(event(final, "closed"))
    st["seen"] = current
    return events


def poll_audits(arr, state, args):
    c = arr.client
    st = state.setdefault("audits", {}).setdefault(
        arr.name, {"cursor": None, "ids": set()}
    )
    automation = set(_as_list(args.get("automation_users")))
    # /audits does not accept a filter on `time` (REST 2.46: "Unknown
    # attribute"), so fetch the newest records page by page and cut locally
    # at the cursor.
    batch = int(args.get("audit_batch", 200))
    if st["cursor"] is None:
        latest = _first(_items(c.get_audits(limit=1, sort=["time-"]), "audits"))
        lookback = float(args.get("audit_lookback", 0) or 0) * 1000
        st["cursor"] = int((latest.get("time") or _now_ms()) - lookback)
        if not lookback:
            st["ids"] = {latest.get("name")}
    rows, page, overflow = [], 0, False
    while True:
        chunk = _items(
            c.get_audits(limit=batch, offset=page * batch, sort=["time-"]), "audits"
        )
        fresh = [r for r in chunk if (r.get("time") or 0) >= st["cursor"]]
        rows.extend(fresh)
        page += 1
        if len(fresh) < len(chunk) or len(chunk) < batch:
            break  # crossed the cursor, or reached the end of the log
        if page >= AUDIT_MAX_PAGES:
            overflow = True  # the oldest new records stay unread
            break
    # a record written between two page fetches shifts the pages by one row;
    # keep each audit id once
    rows = list({r.get("name"): r for r in rows}.values())
    # oldest first; audit ids are increasing integers, so they break timestamp ties
    rows.sort(
        key=lambda r: (
            r.get("time") or 0,
            int(r["name"]) if str(r.get("name", "")).isdigit() else 0,
        )
    )
    events = []
    newest = st["cursor"]
    ids_at_newest = set(st["ids"])
    maintenance_active = bool(_maintenance(arr, state)) if rows else False
    for r in rows:
        if r.get("name") in st["ids"] and r.get("time") == st["cursor"]:
            continue
        user = r.get("user")
        events.append(
            {
                "purefa_audit": {
                    "array": arr.name,
                    "maintenance_active": maintenance_active,
                    "audit_id": r.get("name"),
                    "time": r.get("time"),
                    "user": user,
                    "user_interface": r.get("user_interface"),
                    "origin": _name(r.get("origin")),
                    "command": r.get("command"),
                    "subcommand": r.get("subcommand"),
                    "arguments": r.get("arguments"),
                    "by_automation": user in automation,
                    "system_user": user == "purity" or bool(r.get("system_event")),
                }
            }
        )
        t = r.get("time") or newest
        if t > newest:
            newest, ids_at_newest = t, {r.get("name")}
        elif t == newest:
            ids_at_newest.add(r.get("name"))
    st["cursor"], st["ids"] = newest, ids_at_newest
    if overflow:
        msg = (
            f"audit_batch overflow: more than {AUDIT_MAX_PAGES * batch} audit records "
            "since the last poll, the oldest were skipped; raise audit_batch"
        )
        logger.warning("purefa_events[audits]: %s: %s", arr.name, msg)
        events.append(_poll_error("audits", arr.name, msg))
    return events


def poll_health(arr, state, args):
    c = arr.client
    st = state.setdefault("health", {}).setdefault(arr.name, {"primary": None})
    hw_ignore = set(
        _as_list(args.get("hardware_ignore_status"))
        or ["ok", "not_installed", "unused"]
    )
    drive_ignore = set(
        _as_list(args.get("drive_ignore_status"))
        or ["healthy", "unused", "empty", "evacuated"]
    )

    hardware = _items(c.get_hardware(), "hardware")
    issues = [h for h in hardware if (h.get("status") or "unknown") not in hw_ignore]
    drives = _items(c.get_drives(), "drives")
    drive_issues = [
        d for d in drives if (d.get("status") or "unknown") not in drive_ignore
    ]
    controllers = _items(c.get_controllers(), "controllers")
    primary = next(
        (ct.get("name") for ct in controllers if ct.get("mode") == "primary"), None
    )
    windows = _maintenance(arr, state, max_age=0)
    support = _first(_items(c.get_support(), "support"))
    arrays = _first(_items(c.get_arrays(), "arrays"))
    open_alerts = c.get_alerts(filter="state='open'", limit=1, total_item_count=True)
    open_count = _total(open_alerts) if hasattr(open_alerts, "items") else None

    events = [
        {
            "purefa_health": {
                "array": arr.name,
                "purity_version": arrays.get("version"),
                "hardware_issues": [
                    {
                        "component": h.get("name"),
                        "type": h.get("type"),
                        "status": h.get("status"),
                    }
                    for h in issues
                ],
                "hardware_issue_count": len(issues),
                "drive_issues": [
                    {
                        "drive": d.get("name"),
                        "status": d.get("status"),
                        "type": d.get("type"),
                    }
                    for d in drive_issues
                ],
                "drive_issue_count": len(drive_issues),
                "controllers": [
                    {
                        "name": ct.get("name"),
                        "mode": ct.get("mode"),
                        "status": ct.get("status"),
                        "mode_since": ct.get("mode_since"),
                    }
                    for ct in controllers
                ],
                "primary_controller": primary,
                "maintenance_active": bool(windows),
                "maintenance_windows": [
                    {
                        "name": w.get("name"),
                        "created": w.get("created"),
                        "expires": w.get("expires"),
                    }
                    for w in windows
                ],
                "remote_assist_active": support.get("remote_assist_active"),
                "phonehome_enabled": support.get("phonehome_enabled"),
                "open_alert_count": open_count,
            }
        }
    ]
    for h in issues:
        events.append(
            {
                "purefa_hardware": {
                    "array": arr.name,
                    "component": h.get("name"),
                    "type": h.get("type"),
                    "status": h.get("status"),
                    "details": h.get("details"),
                    "slot": h.get("slot"),
                    "serial": h.get("serial"),
                    "identify_enabled": h.get("identify_enabled"),
                    "maintenance_active": bool(windows),
                }
            }
        )
    if st["primary"] and primary and st["primary"] != primary:
        events.append(
            {
                "purefa_controller_failover": {
                    "array": arr.name,
                    "previous_primary": st["primary"],
                    "primary": primary,
                    "controllers": [
                        {
                            "name": ct.get("name"),
                            "mode": ct.get("mode"),
                            "status": ct.get("status"),
                        }
                        for ct in controllers
                    ],
                }
            }
        )
    st["primary"] = primary
    return events


def _capacity_row(arr, state, args):
    c = arr.client
    st = state.setdefault("telemetry", {}).setdefault(arr.name, {})
    now = _now_ms()
    space = _first(_items(c.get_arrays_space(), "arrays/space"))
    sp = space.get("space") or {}
    capacity = space.get("capacity") or 0
    used = sp.get("total_physical") or 0

    growth = days_to_full = None
    hist = []
    try:
        hist = _items(
            c.get_arrays_space(
                resolution=DAY_MS, start_time=now - 7 * DAY_MS, end_time=now
            ),
            "space history",
        )
        pts = [
            (h.get("time"), (h.get("space") or {}).get("total_physical"))
            for h in hist
            if h.get("time")
            and (h.get("space") or {}).get("total_physical") is not None
        ]
        if len(pts) >= 2:
            pts.sort()
            span_days = (pts[-1][0] - pts[0][0]) / DAY_MS
            if span_days > 0:
                growth = int((pts[-1][1] - pts[0][1]) / span_days)
                if growth > 0 and capacity:
                    days_to_full = round((capacity - used) / growth, 1)
    except Exception as exc:  # noqa: BLE001 - history is optional
        logger.debug("purefa_events[%s]: space history unavailable: %s", arr.name, exc)

    # anomaly inputs: data reduction and snapshot space against the 7-day daily
    # history (its first point) and against the previous poll
    drr_now = sp.get("data_reduction")
    snap_now = sp.get("snapshots") or 0
    first = next(
        (h for h in sorted(hist, key=lambda h: h.get("time") or 0) if h.get("space")),
        None,
    )
    drr_week = (first.get("space") or {}).get("data_reduction") if first else None
    snap_week = (first.get("space") or {}).get("snapshots") if first else None
    drr_drop_pct = (
        round(100.0 * (drr_week - drr_now) / drr_week, 1)
        if drr_week and drr_now
        else None
    )
    snap_growth_pct = (
        round(100.0 * (snap_now - snap_week) / snap_week, 1) if snap_week else None
    )
    prev_space = st.get("prev_space") or {}
    drr_drop_poll_pct = (
        round(100.0 * (prev_space["drr"] - drr_now) / prev_space["drr"], 2)
        if prev_space.get("drr") and drr_now
        else None
    )
    snap_growth_poll_pct = (
        round(100.0 * (snap_now - prev_space["snap"]) / prev_space["snap"], 2)
        if prev_space.get("snap")
        else None
    )
    st["prev_space"] = {"drr": drr_now, "snap": snap_now}
    # drr_drop_pct and snapshot_growth_pct are accepted as aliases; the event
    # fields of the same names carry the measured values, hence the distinct
    # *_threshold_pct argument names
    drr_drop_threshold = float(
        args.get("drr_drop_threshold_pct", args.get("drr_drop_pct", 10))
    )
    snap_growth_threshold = float(
        args.get("snapshot_growth_threshold_pct", args.get("snapshot_growth_pct", 25))
    )

    top_n = int(args.get("top_growers", 5))
    # growth is tracked across the growth_volumes largest live volumes
    vols = _items(
        c.get_volumes_space(
            limit=int(args.get("growth_volumes", 500)),
            sort=["space.total_physical-"],
            destroyed=False,
        ),
        "volumes/space",
    )
    sizes = {
        v.get("name"): (v.get("space") or {}).get("total_physical") or 0 for v in vols
    }
    prev = st.get("vol_sizes") or {}
    growers = sorted(
        (
            {"volume": n, "delta_bytes": s - prev[n], "total_physical": s}
            for n, s in sizes.items()
            if n in prev and s - prev[n] > 0
        ),
        key=lambda g: g["delta_bytes"],
        reverse=True,
    )[:top_n]
    st["vol_sizes"] = sizes

    def _pending(fn):
        r = fn(destroyed=True, limit=1, total_item_count=True)
        return _total(r) if hasattr(r, "items") else None

    return {
        "array": arr.name,
        "capacity_bytes": capacity,
        "used_bytes": used,
        "used_pct": round(100.0 * used / capacity, 2) if capacity else None,
        "data_reduction": sp.get("data_reduction"),
        "total_reduction": sp.get("total_reduction"),
        "snapshot_bytes": sp.get("snapshots"),
        "drr_week_ago": drr_week,
        "drr_drop_pct": drr_drop_pct,
        "drr_drop_poll_pct": drr_drop_poll_pct,
        "snapshot_growth_pct": snap_growth_pct,
        "snapshot_growth_poll_pct": snap_growth_poll_pct,
        # anomaly booleans (thresholds: the *_threshold_pct arguments)
        "drr_drop": bool(
            drr_drop_pct is not None and drr_drop_pct >= drr_drop_threshold
        ),
        "snapshot_surge": bool(
            snap_growth_pct is not None and snap_growth_pct >= snap_growth_threshold
        ),
        "growth_bytes_per_day": growth,
        "days_to_full": days_to_full,
        "days_to_full_known": days_to_full is not None,
        "top_growers": growers,
        "pending_eradication_volumes": _pending(c.get_volumes),
        "pending_eradication_snapshots": _pending(c.get_volume_snapshots),
        "time": space.get("time"),
    }


def _perf_fields(perf, prefix=""):
    return {
        prefix + k: perf.get(k)
        for k in (
            "usec_per_read_op",
            "usec_per_write_op",
            "reads_per_sec",
            "writes_per_sec",
            "read_bytes_per_sec",
            "write_bytes_per_sec",
            "queue_depth",
        )
    }


def _volumes_performance(c, names):
    """Current performance rows for `names`. One name that no longer exists
    (a cap tag left on a destroyed volume) fails the whole bulk call, so fall
    back to one call per name and drop only that one."""
    if not names:
        return []
    try:
        return _items(c.get_volumes_performance(names=names), "volumes/performance")
    except Exception:  # noqa: BLE001 - retry one by one
        out = []
        for n in names:
            try:
                out.extend(
                    _items(c.get_volumes_performance(names=[n]), "volumes/performance")
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "purefa_events: volume %s performance unavailable: %s", n, exc
                )
        return out


def _performance_events(arr, state, args):
    c = arr.client
    st = state.setdefault("telemetry", {}).setdefault(arr.name, {})
    now = _now_ms()
    perf = _first(_items(c.get_arrays_performance(), "arrays/performance"))
    ev = {"array": arr.name, "time": perf.get("time"), **_perf_fields(perf)}
    if _truthy(args.get("baseline"), True):
        try:
            week_ago = now - 7 * DAY_MS
            hist = _items(
                c.get_arrays_performance(
                    resolution=HALF_HOUR_MS,
                    start_time=week_ago - HALF_HOUR_MS,
                    end_time=week_ago + HALF_HOUR_MS,
                ),
                "performance baseline",
            )
            for k in (
                "usec_per_read_op",
                "usec_per_write_op",
                "reads_per_sec",
                "writes_per_sec",
            ):
                ev["baseline_" + k] = _avg(h.get(k) for h in hist)
            ev["read_latency_ratio"] = _ratio(
                ev["usec_per_read_op"], ev.get("baseline_usec_per_read_op")
            )
            ev["write_latency_ratio"] = _ratio(
                ev["usec_per_write_op"], ev.get("baseline_usec_per_write_op")
            )
            ev["baseline_known"] = ev["write_latency_ratio"] is not None
            ev["write_bw_ratio"] = _ratio(
                ev["write_bytes_per_sec"],
                _avg(h.get("write_bytes_per_sec") for h in hist),
            )
            ev["write_surge"] = bool(
                ev["write_bw_ratio"] is not None
                and ev["write_bw_ratio"] >= float(args.get("write_surge_ratio", 3))
            )
        except Exception as exc:  # noqa: BLE001 - baseline is optional
            logger.debug("purefa_events[%s]: baseline unavailable: %s", arr.name, exc)
    ev.setdefault("baseline_known", False)
    ev.setdefault("write_surge", False)
    events = [{"purefa_array_performance": ev}]

    top = int(args.get("top_volumes", 10))
    threshold = float(args.get("latency_threshold_usec", 2000))
    min_iops = float(args.get("min_iops", 1))
    counters = st.setdefault("high_latency_polls", {})
    normal = st.setdefault("normal_polls", {})
    merged = {}
    for sort in ("usec_per_write_op-", "usec_per_read_op-"):
        for v in _items(
            c.get_volumes_performance(limit=top, sort=[sort], destroyed=False),
            "volumes/performance",
        ):
            merged[v.get("name")] = v
    seen = set()
    active = {
        n: vp
        for n, vp in merged.items()
        if (vp.get("reads_per_sec") or 0) + (vp.get("writes_per_sec") or 0) >= min_iops
    }
    # Volumes carrying the eda/qos_cap tag are always reported, whatever their
    # rank or IOPS: the lift rule needs normal_polls to keep counting after the
    # cap (or idleness) has taken the volume out of the slowest-N set. The tag
    # is the array-held state the hysteresis rule reads back.
    # A failed tag read fails the poll (purefa_poll_error, client reset): a
    # silent empty map would re-cap volumes that are already capped.
    capped = {}
    for t in _items(c.get_volumes_tags(namespaces=["eda"]), "volume tags"):
        if t.get("key") == "qos_cap":
            capped[_name(t.get("resource"))] = t.get("value")
    for vp in _volumes_performance(c, [n for n in capped if n not in active]):
        active[vp.get("name")] = vp
    # QoS limits for the volumes about to be reported
    qos = {}
    if active:
        for v in _items(c.get_volumes(names=list(active)), "volumes"):
            q = v.get("qos") or {}
            qos[v.get("name")] = {
                "iops": q.get("iops_limit"),
                "bw": q.get("bandwidth_limit"),
            }
    array_iops = (perf.get("reads_per_sec") or 0) + (perf.get("writes_per_sec") or 0)
    for name, vp in active.items():
        iops = (vp.get("reads_per_sec") or 0) + (vp.get("writes_per_sec") or 0)
        seen.add(name)
        lat = max(vp.get("usec_per_read_op") or 0, vp.get("usec_per_write_op") or 0)
        counters[name] = counters.get(name, 0) + 1 if lat >= threshold else 0
        normal[name] = normal.get(name, 0) + 1 if lat < threshold else 0
        events.append(
            {
                "purefa_volume_performance": {
                    "array": arr.name,
                    "volume": name,
                    "usec_per_read_op": vp.get("usec_per_read_op"),
                    "usec_per_write_op": vp.get("usec_per_write_op"),
                    "reads_per_sec": vp.get("reads_per_sec"),
                    "writes_per_sec": vp.get("writes_per_sec"),
                    # the array and volume samples are not taken at the same instant; clamp
                    "iops_share_pct": (
                        min(100.0, round(100.0 * iops / array_iops, 1))
                        if array_iops
                        else None
                    ),
                    "high_latency_polls": counters[name],
                    "normal_polls": normal[name],
                    "qos_iops_limit": qos.get(name, {}).get("iops"),
                    "qos_bw_limit": qos.get(name, {}).get("bw"),
                    "eda_capped": name in capped,
                    "eda_cap_since": capped.get(name),
                    "array_usec_per_read_op": perf.get("usec_per_read_op"),
                    "array_usec_per_write_op": perf.get("usec_per_write_op"),
                }
            }
        )
    for name in [n for n in counters if n not in seen]:
        counters.pop(name, None)
        normal.pop(name, None)
    return events


def poll_replication(arr, state, args):
    c = arr.client
    st = state.setdefault("replication", {}).setdefault(
        arr.name,
        {"progress": {}, "rpo_polls": {}, "paused_since": {}, "degraded": set()},
    )
    now = _now_ms()
    rpo = int(args.get("rpo_seconds", 0) or 0)
    events = []
    maintenance_active = bool(_maintenance(arr, state))

    paths = _items(c.get_array_connections_path(), "array-connections/path")
    for conn in _items(c.get_array_connections(), "array-connections"):
        mine = [p for p in paths if p.get("name") == conn.get("name")]
        failed = [
            {
                "local": f"{p.get('local_port')} {p.get('local_address')}",
                "remote": f"{p.get('remote_port')} {p.get('remote_address')}",
                "status": p.get("status"),
            }
            for p in mine
            if p.get("status") != "connected"
        ]
        events.append(
            {
                "purefa_array_connection": {
                    "array": arr.name,
                    "maintenance_active": maintenance_active,
                    "remote": conn.get("name"),
                    "status": conn.get("status"),
                    "type": conn.get("type"),
                    "replication_transport": conn.get("replication_transport"),
                    "paths_total": len(mine),
                    "paths_connected": len(mine) - len(failed),
                    "failed_paths": failed,
                    "throttle": conn.get("throttle") or {},
                    "remote_version": conn.get("version"),
                }
            }
        )

    for link in _items(c.get_pod_replica_links(), "pod-replica-links"):
        rp = link.get("recovery_point")
        key = f"{_name(link.get('local_pod'))}->{_name(link.get('remote_pod'))}"
        breached = bool(rpo > 0 and (link.get("lag") or 0) > rpo * 1000)
        st["rpo_polls"][key] = st["rpo_polls"].get(key, 0) + 1 if breached else 0
        if link.get("paused"):
            st["paused_since"].setdefault(key, now)
        else:
            st["paused_since"].pop(key, None)
        events.append(
            {
                "purefa_replica_link": {
                    "array": arr.name,
                    "maintenance_active": maintenance_active,
                    "local_pod": _name(link.get("local_pod")),
                    "remote_pod": _name(link.get("remote_pod")),
                    "remote_array": ",".join(
                        _name(r) for r in (link.get("remotes") or [])
                    ),
                    "status": link.get("status"),
                    "direction": link.get("direction"),
                    "paused": link.get("paused"),
                    "lag_ms": link.get("lag"),
                    "lag_s": round((link.get("lag") or 0) / 1000, 1),
                    "recovery_point": rp,
                    "recovery_point_age_s": round((now - rp) / 1000) if rp else None,
                    "rpo_seconds": rpo,
                    # rulebook conditions cannot do arithmetic, so the comparison is made here
                    "rpo_breached": breached,
                    "rpo_breach_polls": st["rpo_polls"][key],
                    "paused_for_s": (
                        round((now - st["paused_since"][key]) / 1000)
                        if key in st["paused_since"]
                        else 0
                    ),
                }
            }
        )

    for pod in _items(c.get_pods(filter="destroyed='false' and array_count>1"), "pods"):
        members = [
            {
                "array": m.get("name"),
                "status": m.get("status"),
                "mediator_status": m.get("mediator_status"),
                "pre_elected": m.get("pre_elected"),
            }
            for m in (pod.get("arrays") or [])
        ]
        all_online = all(m["status"] == "online" for m in members)
        mediator_online = all(m["mediator_status"] == "online" for m in members)
        pname = pod.get("name")
        recovered = all_online and pname in st["degraded"]
        if all_online:
            st["degraded"].discard(pname)
        else:
            st["degraded"].add(pname)
        events.append(
            {
                "purefa_stretched_pod": {
                    "array": arr.name,
                    "maintenance_active": maintenance_active,
                    "pod": pname,
                    "members": members,
                    "online_arrays": [
                        m["array"] for m in members if m["status"] == "online"
                    ],
                    "offline_arrays": [
                        m["array"] for m in members if m["status"] != "online"
                    ],
                    "all_online": all_online,
                    "mediator_online": mediator_online,
                    "recovered": recovered,
                }
            }
        )

    stuck_after = int(args.get("stuck_transfer_polls", 3))
    pgs = _items(
        c.get_protection_groups(
            filter="target_count>0 and destroyed='false' and is_local='true'"
        ),
        "protection-groups",
    )
    targets_by_pg = {}
    if pgs:
        for t in _items(
            c.get_protection_groups_targets(group_names=[p.get("name") for p in pgs]),
            "pg targets",
        ):
            targets_by_pg.setdefault(_name(t.get("group")), []).append(
                _name(t.get("member"))
            )
    for pg in pgs:
        name = pg.get("name")
        # Newest first. Purity sorts a null `completed` ahead of the values
        # under a descending sort (checked on REST 2.46 and 2.54), so the
        # queued and in-progress transfers lead and the newest completed one
        # follows; `started` is null even on completed rows there, so it is
        # no use as the key. Without a sort a page is the oldest retained
        # transfers in name order, which made overdue fire for ever and
        # stuck never. The limit only bounds a pathological backlog.
        transfers = _items(
            c.get_protection_group_snapshots_transfer(
                filter=f"name='{name}.*'",
                sort=["completed-"],
                destroyed=False,
                limit=PG_TRANSFER_ROWS,
            ),
            "pg transfers",
        )
        completed = [t.get("completed") for t in transfers if t.get("completed")]
        last = max(completed) if completed else None
        running = [
            t
            for t in transfers
            if not t.get("completed") and t.get("progress") is not None
        ]
        running.sort(key=lambda t: t.get("started") or 0, reverse=True)
        cur = running[0] if running else None
        key = f"{name}"
        if cur:
            # progress or bytes moving both reset the stuck counter
            mark = (cur.get("name"), cur.get("progress"), cur.get("data_transferred"))
            prevp = st["progress"].get(key)
            polls = prevp[1] + 1 if prevp and prevp[0] == mark else 0
            st["progress"][key] = (mark, polls)
        else:
            polls = 0
            st["progress"].pop(key, None)
        sched = pg.get("replication_schedule") or {}
        freq = sched.get("frequency")
        since = round((now - last) / 1000) if last else None
        events.append(
            {
                "purefa_pg_replication": {
                    "array": arr.name,
                    "maintenance_active": maintenance_active,
                    "pgroup": name,
                    "targets": targets_by_pg.get(name, []),
                    "schedule_enabled": sched.get("enabled"),
                    "frequency_ms": freq,
                    "last_completed": last,
                    "since_last_completed_s": since,
                    "overdue": bool(
                        sched.get("enabled")
                        and freq
                        and since is not None
                        and since * 1000 > 2 * freq
                    ),
                    "in_progress": (
                        {
                            "name": cur.get("name"),
                            "progress": cur.get("progress"),
                            "data_transferred": cur.get("data_transferred"),
                        }
                        if cur
                        else None
                    ),
                    "stuck_polls": polls,
                    "stuck": polls >= stuck_after,
                }
            }
        )
    return events


def poll_config(arr, state, args):
    c = arr.client
    a = _first(_items(c.get_arrays(), "arrays"))
    # the management configuration when Purity names them, else the only one
    all_dns = _items(c.get_dns(), "dns")
    dns = _first([d for d in all_dns if d.get("name") == "management"] or all_dns)
    all_ds = _items(c.get_directory_services(), "directory-services")
    ds = _first([d for d in all_ds if d.get("name") == "management"] or all_ds)
    erad = a.get("eradication_config") or {}
    admin = _first(_items(c.get_admins_settings(), "admins/settings"))
    syslog = [s.get("uri") for s in _items(c.get_syslog_servers(), "syslog-servers")]
    smtp = _first(_items(c.get_smtp_servers(), "smtp-servers"))
    watchers = [
        w.get("name")
        for w in _items(c.get_alert_watchers(), "alert-watchers")
        if w.get("enabled")
    ]
    support = _first(_items(c.get_support(), "support"))
    return [
        {
            "purefa_config": {
                "array": arr.name,
                "purity_version": a.get("version"),
                "ntp_servers": a.get("ntp_servers") or [],
                "ntp_servers_csv": _csv(a.get("ntp_servers")),
                "dns_domain": dns.get("domain"),
                "dns_nameservers": dns.get("nameservers") or [],
                "dns_nameservers_csv": _csv(dns.get("nameservers")),
                "ldap_enabled": bool(ds.get("enabled")),
                "ldap_uris": ds.get("uris") or [],
                "ldap_uris_csv": _csv(ds.get("uris")),
                "ldap_base_dn": ds.get("base_dn"),
                "ldap_bind_user": ds.get("bind_user"),
                "manual_eradication": erad.get("manual_eradication"),
                "eradication_delay_ms": erad.get("eradication_delay")
                or erad.get("enabled_delay"),
                "eradication_disabled_delay_ms": erad.get("disabled_delay"),
                "lockout_duration_ms": admin.get("lockout_duration"),
                "max_login_attempts": admin.get("max_login_attempts"),
                "min_password_length": admin.get("min_password_length"),
                "sso_enabled": admin.get("single_sign_on_enabled"),
                "syslog_servers": syslog,
                "syslog_servers_csv": _csv(syslog),
                "smtp_relay_host": smtp.get("relay_host"),
                "alert_watchers_enabled": watchers,
                "alert_watchers_enabled_csv": _csv(watchers),
                "phonehome_enabled": support.get("phonehome_enabled"),
            }
        }
    ]


# telemetry is the only signal that needs every array's result before it can
# emit: the capacity event lists the other polled arrays, least used first.
def _capacity_events(rows):
    events = []
    for row in rows:
        others = sorted(
            (
                {"array": r["array"], "used_pct": r["used_pct"]}
                for r in rows
                if r is not row
            ),
            key=lambda r: (r["used_pct"] is None, r["used_pct"]),
        )
        events.append({"purefa_capacity": {**row, "others": others}})
    return events


POLLERS = {
    "alerts": poll_alerts,
    "audits": poll_audits,
    "health": poll_health,
    "replication": poll_replication,
    "config": poll_config,
}


# --------------------------------------------------------------------------
# asyncio driver
# --------------------------------------------------------------------------
async def _signal_loop(queue, signal, arrays, state, args, executor):
    loop = asyncio.get_running_loop()
    interval = float(args.get(f"{signal}_interval") or args.get("interval") or 60)

    def run_sync(arr, fn, st, a):
        try:
            return fn(arr, st, a)
        except Exception:
            arr.reset()
            raise

    async def run(arr, fn):
        # every poller on an array runs under its lock: one SDK client, one
        # request at a time, and reset() never races a call in flight
        async with arr.lock:
            return await loop.run_in_executor(executor, run_sync, arr, fn, state, args)

    async def emit(events):
        for event in events:
            await queue.put(event)

    def failed(arr, exc):
        logger.warning("purefa_events[%s]: %s poll failed: %s", signal, arr.name, exc)
        return [_poll_error(signal, arr.name, exc)]

    logger.info(
        "purefa_events[%s]: polling %s every %ss",
        signal,
        [a.name for a in arrays],
        interval,
    )
    while True:
        started = time.monotonic()
        if signal == "telemetry":
            # capacity rows are collected across the arrays and emitted after
            # the last one, because each lists the others
            rows = []
            for arr in arrays:
                try:
                    rows.append(await run(arr, _capacity_row))
                    await emit(await run(arr, _performance_events))
                except Exception as exc:  # noqa: BLE001 - report and keep polling
                    await emit(failed(arr, exc))
            await emit(_capacity_events(rows))
        else:
            for arr in arrays:
                try:
                    events = await run(arr, POLLERS[signal])
                except Exception as exc:  # noqa: BLE001 - report and keep polling
                    events = failed(arr, exc)
                await emit(events)
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, interval - elapsed))


async def main(queue: asyncio.Queue, args: dict) -> None:
    verify = _truthy(args.get("verify_ssl"), False)
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    specs = _decode_arrays(args.get("arrays"), "purefa_events") or []
    if isinstance(specs, dict):
        specs = [dict(name=k, **v) for k, v in specs.items()]
    request_timeout = float(args.get("request_timeout", 30) or 30)
    arrays = [Array(spec, verify, request_timeout) for spec in specs]
    if not arrays:
        raise ValueError("purefa_events: at least one array is required")
    signals = _as_list(args.get("signals")) or list(SIGNALS)
    bad = [s for s in signals if s not in SIGNALS]
    if bad:
        raise ValueError(f"purefa_events: unknown signals {bad}; valid: {SIGNALS}")
    state: dict = {}
    # one worker per array lock plus one, so no signal waits on a thread
    executor = ThreadPoolExecutor(max_workers=len(arrays) + 1)
    tasks = [
        asyncio.create_task(_signal_loop(queue, s, arrays, state, args, executor))
        for s in signals
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        executor.shutdown(wait=False)


if __name__ == "__main__":
    # Manual test: python purefa_events.py <name> <fa_url> <TOKEN_ENV_VAR> [signals] [interval]
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    class _Q:
        async def put(self, event):
            print(json.dumps(event, default=str), flush=True)

    asyncio.run(
        main(
            _Q(),
            {
                "arrays": [
                    {
                        "name": sys.argv[1],
                        "fa_url": sys.argv[2],
                        "api_token_env": sys.argv[3],
                    }
                ],
                "signals": sys.argv[4] if len(sys.argv) > 4 else None,
                "interval": float(sys.argv[5]) if len(sys.argv) > 5 else 30,
                "audit_lookback": 600,
            },
        )
    )
