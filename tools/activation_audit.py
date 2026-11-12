#!/usr/bin/env python3
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Audit the FlashArray activations on an Ansible Automation Platform EDA
controller against the extra vars they should carry, and recreate the
drifted ones.

    export AAP_TOKEN=...
    python tools/activation_audit.py --url https://aap.example.com --vars vars/local-site.yml
    python tools/activation_audit.py --url ... --vars ... --recreate

An activation keeps the extra vars it was created with. aap/seed.yml renders
them from the site vars file laid over vars/example.yml, minus the keys the
attached credentials inject (purefa_arrays and config_arrays from the fleet
credential, orchestrator_url from the orchestrator credential when it is
attached), plus aap_organization. This tool renders the same thing from the
same files and compares it with every activation named
"FlashArray rulebook - *": keys that differ, are missing or are extra, values
that are still example placeholders (example.com, the documentation IP ranges,
REPLACE_AT_), and activations that are disabled or not running. The exit
status is 1 when anything is wrong, so a schedule can run it. With
--recreate each activation whose vars differ from the expected ones is
deleted and created again with those vars and the same project, rulebook,
decision environment, credentials, policies and enabled flag; a
placeholder-only, disabled or stopped activation is reported, not recreated,
and nothing is recreated while the expected vars themselves carry a
placeholder. Every delete and create is printed as it happens, old id and
new id, because the Rule Audit rows stay under the old id. The token comes
from the environment only and is never printed. Python 3.9+, standard
library plus PyYAML.
"""

import argparse
import json
import os
import pathlib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFIX = "FlashArray rulebook - "
INJECTED = ("purefa_arrays", "config_arrays")
ORCHESTRATOR_CREDENTIAL = "Orchestrator"
PLACEHOLDER_MARKS = (
    "example.com",
    "192.0.2.",
    "198.51.100.",
    "203.0.113.",
    "REPLACE_AT_",
    "changeme",
)
ENV_TOKEN = "AAP_TOKEN"
TIMEOUT_SECONDS = 60
DELETE_WAIT_SECONDS = 120


class AuditError(Exception):
    """The platform could not be read or written as asked."""


# ---------------------------------------------------------------- the maths
def expected_extra_vars(example, site, organization, orchestrator_attached):
    """What the seed renders: the site file over the example, minus the
    credential-injected keys, plus aap_organization."""
    merged = dict(example or {})
    merged.update(site or {})
    for key in INJECTED:
        merged.pop(key, None)
    if orchestrator_attached:
        merged.pop("orchestrator_url", None)
    merged["aap_organization"] = organization
    return merged


def placeholders(variables, allow=()):
    """(key, value) pairs whose value is still an example placeholder. The
    orchestrator URL only counts while the orchestrator route is on."""
    found = []
    enabled = bool(variables.get("orchestrator_enabled", False))
    for key, value in sorted(variables.items()):
        if key in allow:
            continue
        if key == "orchestrator_url" and not enabled:
            continue
        text = json.dumps(value, default=str) if not isinstance(value, str) else value
        if any(mark in text for mark in PLACEHOLDER_MARKS):
            found.append((key, value))
    return found


def diff_vars(expected, live):
    """Keys the live activation lacks, has extra, or carries another value for."""
    missing = sorted(k for k in expected if k not in live)
    extra = sorted(k for k in live if k not in expected)
    changed = {
        k: (expected[k], live[k])
        for k in sorted(expected)
        if k in live and live[k] != expected[k]
    }
    return {"missing": missing, "extra": extra, "changed": changed}


def live_vars(detail):
    """The activation's extra vars as a mapping (empty when absent)."""
    live = yaml.safe_load(detail.get("extra_var") or "") or {}
    return live if isinstance(live, dict) else None


def has_drift(detail, expected):
    """True when the activation's vars differ from the expected ones."""
    live = live_vars(detail)
    if live is None:
        return True
    diff = diff_vars(expected, live)
    return bool(diff["missing"] or diff["extra"] or diff["changed"])


def _short(value):
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= 60 else text[:57] + "..."


def audit_activation(detail, expected, allow=()):
    """One activation's problems as text lines; an empty list is a clean one."""
    live = live_vars(detail)
    if live is None:
        return ["extra vars are not a mapping"]
    problems = []
    diff = diff_vars(expected, live)
    for key in diff["missing"]:
        problems.append(f"missing {key} (expected {_short(expected[key])})")
    for key in diff["extra"]:
        problems.append(f"extra {key}")
    for key, (want, got) in diff["changed"].items():
        problems.append(f"{key} is {_short(got)}, expected {_short(want)}")
    for key, value in placeholders(live, allow):
        problems.append(f"placeholder {key}={_short(value)}")
    if not detail.get("is_enabled", True):
        problems.append("disabled")
    status = detail.get("status")
    if status and status != "running":
        problems.append(f"status {status}")
    return problems


# ------------------------------------------------------------ the platform
class Client:
    def __init__(self, url, token, insecure=False):
        self.base = url.rstrip("/")
        self.token = token
        self.context = ssl.create_default_context()
        if insecure:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE

    def request(self, method, path, body=None, retries=5):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(
                    request, context=self.context, timeout=TIMEOUT_SECONDS
                ) as response:
                    raw = response.read()
                    return response.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as exc:
                if exc.code in (502, 503, 504) and attempt + 1 < retries:
                    time.sleep(3)
                    continue
                detail = exc.read()[:300].decode(errors="replace")
                raise AuditError(f"{method} {path}: HTTP {exc.code} {detail}") from None
            except (urllib.error.URLError, OSError) as exc:
                raise AuditError(f"{method} {path}: {exc}") from None
        raise AuditError(f"{method} {path}: gave up after {retries} attempts")

    def list_all(self, path):
        items = []
        while path:
            status, page = self.request("GET", path)
            if not isinstance(page, dict):
                raise AuditError(f"GET {path}: the answer is not an object")
            items.extend(page.get("results", []))
            nxt = page.get("next")
            if nxt and nxt.startswith("http"):
                nxt = nxt[nxt.index("/api/") :] if "/api/" in nxt else None
            path = nxt
        return items


def _ref(detail, key):
    value = detail.get(key)
    return value.get("id") if isinstance(value, dict) else detail.get(key + "_id")


def orchestrator_attached(detail):
    return any(
        ORCHESTRATOR_CREDENTIAL.lower() in str(c.get("name", "")).lower()
        for c in detail.get("eda_credentials", [])
        if isinstance(c, dict)
    )


def recreate(client, detail, extra_var_text, say, wait_seconds=360):
    """Delete the activation and create it again with new extra vars and the
    same everything else, saying what happens as it happens; returns the new
    activation's id and status."""
    old_id = detail["id"]
    name = detail["name"]
    body = {
        "name": name,
        "description": detail.get("description") or "",
        "project_id": _ref(detail, "project"),
        "rulebook_id": _ref(detail, "rulebook"),
        "decision_environment_id": _ref(detail, "decision_environment"),
        "organization_id": _ref(detail, "organization"),
        "extra_var": extra_var_text,
        "eda_credentials": [
            c["id"] for c in detail.get("eda_credentials", []) if isinstance(c, dict)
        ],
        "restart_policy": detail.get("restart_policy") or "on-failure",
        "log_level": detail.get("log_level") or "error",
        "is_enabled": bool(detail.get("is_enabled", True)),
    }
    missing = [
        k
        for k in (
            "project_id",
            "rulebook_id",
            "decision_environment_id",
            "organization_id",
        )
        if not body[k]
    ]
    if missing:
        raise AuditError(f"{name}: cannot recreate, the detail lacks {missing}")
    say(f"deleting {name} (id {old_id}); its Rule Audit rows stay under that id")
    client.request("POST", f"/api/eda/v1/activations/{old_id}/disable/", {})
    time.sleep(5)
    client.request("DELETE", f"/api/eda/v1/activations/{old_id}/")
    deadline = time.monotonic() + DELETE_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(5)
        listed = client.list_all("/api/eda/v1/activations/?page_size=200")
        if not any(a.get("id") == old_id for a in listed):
            break
    else:
        raise AuditError(
            f"{name}: id {old_id} still listed {DELETE_WAIT_SECONDS} s after the delete; not recreating"
        )
    try:
        status, created = client.request("POST", "/api/eda/v1/activations/", body)
    except AuditError as exc:
        raise AuditError(
            f"{name}: deleted (was id {old_id}) but the create failed: {exc}; "
            "run aap/seed.yml to create it again"
        ) from None
    if not isinstance(created, dict) or "id" not in created:
        raise AuditError(
            f"{name}: deleted (was id {old_id}) but the create answered {status} "
            f"{str(created)[:200]}; run aap/seed.yml to create it again"
        )
    new_id = created["id"]
    say(f"created {name} as id {new_id}")
    deadline = time.monotonic() + wait_seconds
    state = created.get("status")
    while time.monotonic() < deadline:
        time.sleep(10)
        status, current = client.request("GET", f"/api/eda/v1/activations/{new_id}/")
        state = current.get("status") if isinstance(current, dict) else state
        if state == "running":
            break
    return new_id, state


# -------------------------------------------------------------------- main
def _line(entry):
    if not entry["problems"]:
        return f"{entry['name']}: ok ({entry['status']})"
    return f"{entry['name']} (id {entry['id']}): " + "; ".join(entry["problems"])


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="activation_audit",
        description="Compare every FlashArray activation's extra vars with what the seed "
        "would render; optionally recreate the drifted ones.",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="the platform gateway, e.g. https://aap.example.com",
    )
    parser.add_argument(
        "--vars",
        required=True,
        metavar="FILE",
        help="the site vars file the seed uses (FAEDA_VARS)",
    )
    parser.add_argument(
        "--example",
        default=str(ROOT / "vars" / "example.yml"),
        metavar="FILE",
        help="the example vars file the site file is laid over (default vars/example.yml)",
    )
    parser.add_argument(
        "--organization",
        default="FlashArray",
        help="aap_organization (default FlashArray)",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="KEY",
        help="accept a placeholder value for this key (repeatable)",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="delete and recreate every activation whose vars differ from the expected ones",
    )
    parser.add_argument(
        "--insecure", action="store_true", help="do not verify the TLS certificate"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    token = os.environ.get(ENV_TOKEN, "")
    if not token:
        parser.error(f"set {ENV_TOKEN} in the environment")
    try:
        example = yaml.safe_load(pathlib.Path(args.example).read_text()) or {}
        site = yaml.safe_load(pathlib.Path(args.vars).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        parser.error(f"cannot read the vars files: {exc}")

    # progress goes to stdout as it happens, or to stderr when stdout is the JSON report
    def say(text):
        print(text, file=sys.stderr if args.json else sys.stdout)

    client = Client(args.url, token, insecure=args.insecure)
    report = []
    try:
        activations = [
            a
            for a in client.list_all("/api/eda/v1/activations/?page_size=200")
            if str(a.get("name", "")).startswith(PREFIX)
        ]
        if not activations:
            print(
                f"activation_audit: no activation named {PREFIX}* found",
                file=sys.stderr,
            )
            return 1
        for summary in sorted(activations, key=lambda a: a["name"]):
            status, detail = client.request(
                "GET", f"/api/eda/v1/activations/{summary['id']}/"
            )
            attached = orchestrator_attached(detail)
            expected = expected_extra_vars(example, site, args.organization, attached)
            problems = audit_activation(detail, expected, args.allow)
            entry = {
                "name": detail["name"],
                "id": detail["id"],
                "status": detail.get("status"),
                "problems": problems,
                "drift": has_drift(detail, expected),
            }
            if entry["drift"] and args.recreate:
                stuck = placeholders(expected, args.allow)
                if stuck:
                    entry["problems"].append(
                        "expected vars still carry placeholder "
                        + ", ".join(k for k, _ in stuck)
                        + "; fix the site file before recreating"
                    )
                    entry["not_recreated"] = True
                else:
                    if not args.json:
                        say(_line(entry))
                    text = yaml.safe_dump(
                        expected, default_flow_style=False, sort_keys=False
                    )
                    new_id, state = recreate(client, detail, text, say)
                    entry["recreated"] = {
                        "old_id": detail["id"],
                        "new_id": new_id,
                        "status": state,
                    }
                    report.append(entry)
                    continue
            report.append(entry)
            if not args.json:
                say(_line(entry))
    except AuditError as exc:
        # say what was done before the failure, so the old -> new ids survive it
        for entry in report:
            if entry.get("recreated"):
                r = entry["recreated"]
                print(
                    f"{entry['name']}: recreated as id {r['new_id']} ({r['status']}); was id {r['old_id']}",
                    file=sys.stderr,
                )
        print(f"activation_audit: {exc}", file=sys.stderr)
        return 1

    wrong = 0
    for entry in report:
        recreated = entry.get("recreated")
        if recreated:
            if recreated["status"] != "running":
                wrong += 1
            continue
        if entry["problems"]:
            wrong += 1
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for entry in report:
            if entry.get("recreated"):
                r = entry["recreated"]
                print(
                    f"    {entry['name']}: recreated as id {r['new_id']} ({r['status']}); "
                    f"the old id {r['old_id']} keeps its Rule Audit rows"
                )
    print(
        f"activation_audit: {len(report)} activation(s), {wrong} wrong"
        + (", drifted ones recreated" if args.recreate else ""),
        file=sys.stderr,
    )
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
