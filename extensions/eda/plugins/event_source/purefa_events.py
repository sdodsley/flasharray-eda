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
audit_batch       audits: newest records fetched per poll; raise it if an
                  array can log more than this between polls (default 200)
signals           list or comma-separated string, default: all of
                  alerts | audits | health | telemetry | replication | config
interval          default seconds between polls (default 60)
<signal>_interval per-signal override, e.g. audits_interval: 15
verify_ssl        verify the array certificate (default false)
automation_users  user names whose audit records count as automation
                  (default: empty). Audit events carry `by_automation`.
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
                  array per poll (default 10, server-side sorted)
top_growers       telemetry: how many fastest-growing volumes to report
                  (default 5)
baseline          telemetry: also fetch the same half hour one week ago
                  from the array's own history (default true)
rpo_seconds       replication: informational; copied onto replica-link
                  events so rules can compare lag with it (default 0 = none)

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
purefa_health           hardware_issues[], hardware_issue_count,
                        drive_issues[], drive_issue_count, controllers[],
                        primary_controller,
                        maintenance_active, maintenance_windows[],
                        remote_assist_active, phonehome_enabled,
                        open_alert_count, purity_version
purefa_hardware         one per hardware component whose status is an issue:
                        component, type, status, details, slot, serial
purefa_controller_failover
                        previous_primary, primary, controllers[]
purefa_capacity         capacity_bytes, used_bytes, used_pct, data_reduction,
                        total_reduction, snapshot_bytes, growth_bytes_per_day,
                        days_to_full, days_to_full_known, top_growers[],
                        pending_eradication_*,
                        others[] (other polled arrays, least used first)
purefa_array_performance
                        usec_per_read_op, usec_per_write_op, reads_per_sec,
                        writes_per_sec, read/write_bytes_per_sec, queue_depth,
                        baseline_* (same half hour last week), read/write
                        latency ratio against the baseline, baseline_known
purefa_volume_performance
                        volume, latencies, IOPS, high_latency_polls,
                        array_usec_per_read_op, array_usec_per_write_op
purefa_array_connection remote, status, type, replication_transport,
                        paths_total, paths_connected, throttle
purefa_replica_link     local_pod, remote_pod, remote_array, status,
                        direction, paused, lag_ms, lag_s, recovery_point,
                        recovery_point_age_s, rpo_seconds, rpo_breached
purefa_stretched_pod    pod, members[{array,status,mediator_status}],
                        all_online, mediator_online
purefa_pg_replication   pgroup, targets[], schedule_enabled, frequency_ms,
                        last_completed, since_last_completed_s, overdue,
                        in_progress{name,progress,data_transferred},
                        stuck_polls
purefa_config           purity_version, ntp_servers(_csv), dns_domain,
                        dns_nameservers(_csv), ldap_enabled, ldap_uris(_csv),
                        ldap_base_dn, ldap_bind_user, manual_eradication,
                        eradication_delay_ms, lockout_duration_ms,
                        max_login_attempts, min_password_length, sso_enabled,
                        syslog_servers(_csv), smtp_relay_host,
                        alert_watchers_enabled(_csv), phonehome_enabled
purefa_poll_error       signal, error
"""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import urllib3
from pypureclient import flasharray

logger = logging.getLogger(__name__)

SIGNALS = ("alerts", "audits", "health", "telemetry", "replication", "config")
DAY_MS = 86_400_000
HALF_HOUR_MS = 1_800_000


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _as_list(value):
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)


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


def _maintenance(arr, state, max_age=20.0):
    """Active maintenance windows on the array, cached briefly across signals.

    Every signal stamps `maintenance_active` on its events so a rule can
    suppress remediation while someone has declared work on the array,
    without cross-signal facts in the rulebook.
    """
    cache = state.setdefault("maintenance", {}).setdefault(arr.name, {"at": 0, "windows": []})
    if time.monotonic() - cache["at"] > max_age:
        cache["windows"] = _items(arr.client.get_maintenance_windows(), "maintenance windows")
        cache["at"] = time.monotonic()
    return cache["windows"]


# --------------------------------------------------------------------------
# array handle: one SDK client per array, rebuilt after a failure
# --------------------------------------------------------------------------
class Array:
    def __init__(self, spec, verify_ssl):
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
            raise ValueError(f"array {self.name}: api_token or api_token_env is required")
        self._token = token
        self._verify = verify_ssl
        self._client = None
        self.lock = asyncio.Lock()

    @property
    def client(self):
        if self._client is None:
            self._client = flasharray.Client(
                target=self.host, api_token=self._token, verify_ssl=self._verify)
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
        return {"purefa_alert": {
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
        }}

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
            events.append(event(a, "closing" if a.get("state") == "closing" else "updated"))
    if not first:
        for aid in set(seen) - set(current):
            final = _first(_items(c.get_alerts(names=[aid]), "alert"), {"name": aid, "state": "closed"})
            events.append(event(final, "closed"))
    st["seen"] = current
    return events


def poll_audits(arr, state, args):
    c = arr.client
    st = state.setdefault("audits", {}).setdefault(arr.name, {"cursor": None, "ids": set()})
    automation = set(_as_list(args.get("automation_users")))
    # /audits does not accept a filter on `time` (REST 2.46: "Unknown
    # attribute"), so fetch the newest records and cut locally at the cursor.
    batch = int(args.get("audit_batch", 200))
    if st["cursor"] is None:
        latest = _first(_items(c.get_audits(limit=1, sort=["time-"]), "audits"))
        lookback = float(args.get("audit_lookback", 0) or 0) * 1000
        st["cursor"] = int((latest.get("time") or _now_ms()) - lookback)
        if not lookback:
            st["ids"] = {latest.get("name")}
    rows = _items(c.get_audits(limit=batch, sort=["time-"]), "audits")
    rows = [r for r in rows if (r.get("time") or 0) >= st["cursor"]]
    rows.reverse()  # oldest first
    events = []
    newest = st["cursor"]
    ids_at_newest = set(st["ids"])
    maintenance_active = bool(_maintenance(arr, state)) if rows else False
    for r in rows:
        if r.get("name") in st["ids"] and r.get("time") == st["cursor"]:
            continue
        user = r.get("user")
        events.append({"purefa_audit": {
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
        }})
        t = r.get("time") or newest
        if t > newest:
            newest, ids_at_newest = t, {r.get("name")}
        elif t == newest:
            ids_at_newest.add(r.get("name"))
    st["cursor"], st["ids"] = newest, ids_at_newest
    return events


def poll_health(arr, state, args):
    c = arr.client
    st = state.setdefault("health", {}).setdefault(arr.name, {"primary": None})
    hw_ignore = set(_as_list(args.get("hardware_ignore_status")) or ["ok", "not_installed", "unused"])
    drive_ignore = set(_as_list(args.get("drive_ignore_status")) or ["healthy", "unused", "empty", "evacuated"])

    hardware = _items(c.get_hardware(), "hardware")
    issues = [h for h in hardware if (h.get("status") or "unknown") not in hw_ignore]
    drives = _items(c.get_drives(), "drives")
    drive_issues = [d for d in drives if (d.get("status") or "unknown") not in drive_ignore]
    controllers = _items(c.get_controllers(), "controllers")
    primary = next((ct.get("name") for ct in controllers if ct.get("mode") == "primary"), None)
    windows = _maintenance(arr, state, max_age=0)
    support = _first(_items(c.get_support(), "support"))
    arrays = _first(_items(c.get_arrays(), "arrays"))
    open_alerts = c.get_alerts(filter="state='open'", limit=1, total_item_count=True)
    open_count = _total(open_alerts) if hasattr(open_alerts, "items") else None

    events = [{"purefa_health": {
        "array": arr.name,
        "purity_version": arrays.get("version"),
        "hardware_issues": [{"component": h.get("name"), "type": h.get("type"), "status": h.get("status")} for h in issues],
        "hardware_issue_count": len(issues),
        "drive_issues": [{"drive": d.get("name"), "status": d.get("status"), "type": d.get("type")} for d in drive_issues],
        "drive_issue_count": len(drive_issues),
        "controllers": [{"name": ct.get("name"), "mode": ct.get("mode"), "status": ct.get("status"),
                         "mode_since": ct.get("mode_since")} for ct in controllers],
        "primary_controller": primary,
        "maintenance_active": bool(windows),
        "maintenance_windows": [{"name": w.get("name"), "created": w.get("created"), "expires": w.get("expires")} for w in windows],
        "remote_assist_active": support.get("remote_assist_active"),
        "phonehome_enabled": support.get("phonehome_enabled"),
        "open_alert_count": open_count,
    }}]
    for h in issues:
        events.append({"purefa_hardware": {
            "array": arr.name,
            "component": h.get("name"),
            "type": h.get("type"),
            "status": h.get("status"),
            "details": h.get("details"),
            "slot": h.get("slot"),
            "serial": h.get("serial"),
            "identify_enabled": h.get("identify_enabled"),
            "maintenance_active": bool(windows),
        }})
    if st["primary"] and primary and st["primary"] != primary:
        events.append({"purefa_controller_failover": {
            "array": arr.name,
            "previous_primary": st["primary"],
            "primary": primary,
            "controllers": [{"name": ct.get("name"), "mode": ct.get("mode"), "status": ct.get("status")} for ct in controllers],
        }})
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
    try:
        hist = _items(c.get_arrays_space(resolution=DAY_MS, start_time=now - 7 * DAY_MS, end_time=now), "space history")
        pts = [(h.get("time"), (h.get("space") or {}).get("total_physical")) for h in hist
               if h.get("time") and (h.get("space") or {}).get("total_physical") is not None]
        if len(pts) >= 2:
            pts.sort()
            span_days = (pts[-1][0] - pts[0][0]) / DAY_MS
            if span_days > 0:
                growth = int((pts[-1][1] - pts[0][1]) / span_days)
                if growth > 0 and capacity:
                    days_to_full = round((capacity - used) / growth, 1)
    except Exception as exc:  # noqa: BLE001 - history is optional
        logger.debug("purefa_events[%s]: space history unavailable: %s", arr.name, exc)

    top_n = int(args.get("top_growers", 5))
    vols = _items(c.get_volumes_space(limit=500, sort=["space.total_physical-"], destroyed=False), "volumes/space")
    sizes = {v.get("name"): (v.get("space") or {}).get("total_physical") or 0 for v in vols}
    prev = st.get("vol_sizes") or {}
    growers = sorted(({"volume": n, "delta_bytes": s - prev[n], "total_physical": s}
                      for n, s in sizes.items() if n in prev and s - prev[n] > 0),
                     key=lambda g: g["delta_bytes"], reverse=True)[:top_n]
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
        "growth_bytes_per_day": growth,
        "days_to_full": days_to_full,
        "days_to_full_known": days_to_full is not None,
        "top_growers": growers,
        "pending_eradication_volumes": _pending(c.get_volumes),
        "pending_eradication_snapshots": _pending(c.get_volume_snapshots),
        "time": space.get("time"),
    }


def _perf_fields(perf, prefix=""):
    return {prefix + k: perf.get(k) for k in (
        "usec_per_read_op", "usec_per_write_op", "reads_per_sec", "writes_per_sec",
        "read_bytes_per_sec", "write_bytes_per_sec", "queue_depth")}


def _performance_events(arr, state, args):
    c = arr.client
    st = state.setdefault("telemetry", {}).setdefault(arr.name, {})
    now = _now_ms()
    perf = _first(_items(c.get_arrays_performance(), "arrays/performance"))
    ev = {"array": arr.name, "time": perf.get("time"), **_perf_fields(perf)}
    if _truthy(args.get("baseline"), True):
        try:
            week_ago = now - 7 * DAY_MS
            hist = _items(c.get_arrays_performance(resolution=HALF_HOUR_MS, start_time=week_ago - HALF_HOUR_MS,
                                                   end_time=week_ago + HALF_HOUR_MS), "performance baseline")
            for k in ("usec_per_read_op", "usec_per_write_op", "reads_per_sec", "writes_per_sec"):
                ev["baseline_" + k] = _avg(h.get(k) for h in hist)
            ev["read_latency_ratio"] = _ratio(ev["usec_per_read_op"], ev.get("baseline_usec_per_read_op"))
            ev["write_latency_ratio"] = _ratio(ev["usec_per_write_op"], ev.get("baseline_usec_per_write_op"))
            ev["baseline_known"] = ev["write_latency_ratio"] is not None
        except Exception as exc:  # noqa: BLE001 - baseline is optional
            logger.debug("purefa_events[%s]: baseline unavailable: %s", arr.name, exc)
    ev.setdefault("baseline_known", False)
    events = [{"purefa_array_performance": ev}]

    top = int(args.get("top_volumes", 10))
    threshold = float(args.get("latency_threshold_usec", 2000))
    min_iops = float(args.get("min_iops", 1))
    counters = st.setdefault("high_latency_polls", {})
    merged = {}
    for sort in ("usec_per_write_op-", "usec_per_read_op-"):
        for v in _items(c.get_volumes_performance(limit=top, sort=[sort], destroyed=False), "volumes/performance"):
            merged[v.get("name")] = v
    seen = set()
    for name, vp in merged.items():
        iops = (vp.get("reads_per_sec") or 0) + (vp.get("writes_per_sec") or 0)
        if iops < min_iops:
            counters.pop(name, None)
            continue
        seen.add(name)
        lat = max(vp.get("usec_per_read_op") or 0, vp.get("usec_per_write_op") or 0)
        counters[name] = counters.get(name, 0) + 1 if lat >= threshold else 0
        events.append({"purefa_volume_performance": {
            "array": arr.name,
            "volume": name,
            "usec_per_read_op": vp.get("usec_per_read_op"),
            "usec_per_write_op": vp.get("usec_per_write_op"),
            "reads_per_sec": vp.get("reads_per_sec"),
            "writes_per_sec": vp.get("writes_per_sec"),
            "high_latency_polls": counters[name],
            "array_usec_per_read_op": perf.get("usec_per_read_op"),
            "array_usec_per_write_op": perf.get("usec_per_write_op"),
        }})
    for name in [n for n in counters if n not in seen]:
        counters.pop(name, None)
    return events


def poll_replication(arr, state, args):
    c = arr.client
    st = state.setdefault("replication", {}).setdefault(arr.name, {"progress": {}})
    now = _now_ms()
    rpo = int(args.get("rpo_seconds", 0) or 0)
    events = []
    maintenance_active = bool(_maintenance(arr, state))

    paths = _items(c.get_array_connections_path(), "array-connections/path")
    for conn in _items(c.get_array_connections(), "array-connections"):
        mine = [p for p in paths if p.get("name") == conn.get("name")]
        events.append({"purefa_array_connection": {
            "array": arr.name,
            "maintenance_active": maintenance_active,
            "remote": conn.get("name"),
            "status": conn.get("status"),
            "type": conn.get("type"),
            "replication_transport": conn.get("replication_transport"),
            "paths_total": len(mine),
            "paths_connected": sum(1 for p in mine if p.get("status") == "connected"),
            "throttle": conn.get("throttle") or {},
            "remote_version": conn.get("version"),
        }})

    for link in _items(c.get_pod_replica_links(), "pod-replica-links"):
        rp = link.get("recovery_point")
        events.append({"purefa_replica_link": {
            "array": arr.name,
            "maintenance_active": maintenance_active,
            "local_pod": _name(link.get("local_pod")),
            "remote_pod": _name(link.get("remote_pod")),
            "remote_array": ",".join(_name(r) for r in (link.get("remotes") or [])),
            "status": link.get("status"),
            "direction": link.get("direction"),
            "paused": link.get("paused"),
            "lag_ms": link.get("lag"),
            "lag_s": round((link.get("lag") or 0) / 1000, 1),
            "recovery_point": rp,
            "recovery_point_age_s": round((now - rp) / 1000) if rp else None,
            "rpo_seconds": rpo,
            # rulebook conditions cannot do arithmetic, so the comparison is made here
            "rpo_breached": bool(rpo > 0 and (link.get("lag") or 0) > rpo * 1000),
        }})

    for pod in _items(c.get_pods(filter="destroyed='false' and array_count>1"), "pods"):
        members = [{"array": m.get("name"), "status": m.get("status"), "mediator_status": m.get("mediator_status"),
                    "pre_elected": m.get("pre_elected")} for m in (pod.get("arrays") or [])]
        events.append({"purefa_stretched_pod": {
            "array": arr.name,
            "maintenance_active": maintenance_active,
            "pod": pod.get("name"),
            "members": members,
            "all_online": all(m["status"] == "online" for m in members),
            "mediator_online": all(m["mediator_status"] == "online" for m in members),
        }})

    stuck_after = int(args.get("stuck_transfer_polls", 3))
    pgs = _items(c.get_protection_groups(filter="target_count>0 and destroyed='false' and is_local='true'"), "protection-groups")
    targets_by_pg = {}
    if pgs:
        for t in _items(c.get_protection_groups_targets(group_names=[p.get("name") for p in pgs]), "pg targets"):
            targets_by_pg.setdefault(_name(t.get("group")), []).append(_name(t.get("member")))
    for pg in pgs:
        name = pg.get("name")
        transfers = _items(c.get_protection_group_snapshots_transfer(filter=f"name='{name}.*'", limit=20), "pg transfers")
        completed = [t.get("completed") for t in transfers if t.get("completed")]
        last = max(completed) if completed else None
        running = [t for t in transfers if not t.get("completed") and t.get("progress") is not None]
        running.sort(key=lambda t: t.get("started") or 0, reverse=True)
        cur = running[0] if running else None
        key = f"{name}"
        if cur:
            prevp = st["progress"].get(key)
            polls = (prevp[1] + 1) if prevp and prevp[0] == (cur.get("name"), cur.get("progress")) else 0
            st["progress"][key] = ((cur.get("name"), cur.get("progress")), polls)
        else:
            polls = 0
            st["progress"].pop(key, None)
        sched = pg.get("replication_schedule") or {}
        freq = sched.get("frequency")
        since = round((now - last) / 1000) if last else None
        events.append({"purefa_pg_replication": {
            "array": arr.name,
            "maintenance_active": maintenance_active,
            "pgroup": name,
            "targets": targets_by_pg.get(name, []),
            "schedule_enabled": sched.get("enabled"),
            "frequency_ms": freq,
            "last_completed": last,
            "since_last_completed_s": since,
            "overdue": bool(sched.get("enabled") and freq and since is not None and since * 1000 > 2 * freq),
            "in_progress": {"name": cur.get("name"), "progress": cur.get("progress"),
                            "data_transferred": cur.get("data_transferred")} if cur else None,
            "stuck_polls": polls,
            "stuck": polls >= stuck_after,
        }})
    return events


def poll_config(arr, state, args):
    c = arr.client
    a = _first(_items(c.get_arrays(), "arrays"))
    dns = _first([d for d in _items(c.get_dns(), "dns") if d.get("name") == "management"] or _items(c.get_dns(), "dns"))
    ds = _first([d for d in _items(c.get_directory_services(), "directory-services") if d.get("name") == "management"]
                or _items(c.get_directory_services(), "directory-services"))
    erad = a.get("eradication_config") or {}
    admin = _first(_items(c.get_admins_settings(), "admins/settings"))
    syslog = [s.get("uri") for s in _items(c.get_syslog_servers(), "syslog-servers")]
    smtp = _first(_items(c.get_smtp_servers(), "smtp-servers"))
    watchers = [w.get("name") for w in _items(c.get_alert_watchers(), "alert-watchers") if w.get("enabled")]
    support = _first(_items(c.get_support(), "support"))
    return [{"purefa_config": {
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
        "eradication_delay_ms": erad.get("eradication_delay") or erad.get("enabled_delay"),
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
    }}]


# telemetry is the only signal that needs every array's result before it can
# emit (the capacity event lists the other arrays), so it is a cycle poller.
def poll_telemetry_cycle(arrays, state, args, run):
    rows, events = [], []
    for arr in arrays:
        try:
            rows.append(run(arr, _capacity_row, state, args))
            events.extend(run(arr, _performance_events, state, args))
        except Exception as exc:  # noqa: BLE001
            events.append({"purefa_poll_error": {"signal": "telemetry", "array": arr.name, "error": str(exc)}})
    for row in rows:
        others = sorted(({"array": r["array"], "used_pct": r["used_pct"]} for r in rows if r is not row),
                        key=lambda r: (r["used_pct"] is None, r["used_pct"]))
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
        async with arr.lock:
            return await loop.run_in_executor(executor, run_sync, arr, fn, state, args)

    logger.info("purefa_events[%s]: polling %s every %ss", signal, [a.name for a in arrays], interval)
    while True:
        started = time.monotonic()
        if signal == "telemetry":
            def sync_run(arr, fn, st, a):
                return run_sync(arr, fn, st, a)
            events = await loop.run_in_executor(
                executor, poll_telemetry_cycle, arrays, state, args, sync_run)
            for event in events:
                await queue.put(event)
        else:
            for arr in arrays:
                try:
                    events = await run(arr, POLLERS[signal])
                except Exception as exc:  # noqa: BLE001 - report and keep polling
                    logger.warning("purefa_events[%s]: %s poll failed: %s", signal, arr.name, exc)
                    events = [{"purefa_poll_error": {"signal": signal, "array": arr.name, "error": str(exc)}}]
                for event in events:
                    await queue.put(event)
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, interval - elapsed))


async def main(queue: asyncio.Queue, args: dict) -> None:
    verify = _truthy(args.get("verify_ssl"), False)
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    specs = args.get("arrays") or []
    if isinstance(specs, dict):
        specs = [dict(name=k, **v) for k, v in specs.items()]
    arrays = [Array(spec, verify) for spec in specs]
    if not arrays:
        raise ValueError("purefa_events: at least one array is required")
    signals = _as_list(args.get("signals")) or list(SIGNALS)
    bad = [s for s in signals if s not in SIGNALS]
    if bad:
        raise ValueError(f"purefa_events: unknown signals {bad}; valid: {SIGNALS}")
    state: dict = {}
    executor = ThreadPoolExecutor(max_workers=max(2, len(arrays)))
    tasks = [asyncio.create_task(_signal_loop(queue, s, arrays, state, args, executor)) for s in signals]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        executor.shutdown(wait=False)


if __name__ == "__main__":
    # Manual test: python purefa_events.py <name> <fa_url> <TOKEN_ENV_VAR> [signals] [interval]
    import json
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    class _Q:
        async def put(self, event):
            print(json.dumps(event, default=str), flush=True)

    asyncio.run(main(_Q(), {
        "arrays": [{"name": sys.argv[1], "fa_url": sys.argv[2], "api_token_env": sys.argv[3]}],
        "signals": sys.argv[4] if len(sys.argv) > 4 else None,
        "interval": float(sys.argv[5]) if len(sys.argv) > 5 else 30,
        "audit_lookback": 600,
    }))
