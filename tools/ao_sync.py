#!/usr/bin/env python3
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Import the automation orchestrator workflow definitions under
orchestrator/ into an orchestrator (orchestrator/README.md, "Importing";
API 1.3.1). Standard library only.

    AO_CLIENT_ID=... AO_CLIENT_SECRET=... python tools/ao_sync.py \\
        --url https://orchestrator.example.com \\
        --aap-credential NAME --aap-integration NAME --service-account NAME \\
        [--project NAME] [--dry-run] [--insecure] [--only FILE]... [DEFINITION...]

A sync never deletes. For every definition (orchestrator/*.json unless
files are given; --only FILE narrows what is written without changing what
counts as defined) the tool substitutes the REPLACE_AT_SYNC placeholders
(the ids of the AAP credential and integration named by --aap-credential
and --aap-integration on every aap_job_template and
aap_workflow_job_template node, as credential_id and integration_id; the
id of the service account named by --service-account in a trigger's
authorized_service_account_ids), has the orchestrator validate the result,
creates the workflow when the project (--project, default "default") has
none of that name, publishes ours as the next version when the exported
definition differs (a PATCH on a version changes only its name and
description; the publish call carries the new definition), leaves it alone
when they are equal, and publishes after a create or when the current
version has not been published. Workflows in the project that no file
defines are reported
as "extra: <name>" and left alone. Nothing is written before the token,
the project and every name have resolved; a placeholder left after
substitution fails its definition; --dry-run validates and reports the
decisions without writing. The service account's credentials are read from
the AO_CLIENT_ID and AO_CLIENT_SECRET environment variables only and are
never printed. One line per definition, `<file>: created|updated|unchanged,
published vN` or `<file>: error: <why>`; exit 0 when everything is in
place, 1 on any failure, 2 on a usage error.
"""

import argparse
import glob
import json
import os
import pathlib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

PLACEHOLDER = "REPLACE_AT_SYNC"
AAP_NODE_TYPES = ("aap_job_template", "aap_workflow_job_template")
DEFAULT_PATTERN = "orchestrator/*.json"
ENV_CLIENT_ID = "AO_CLIENT_ID"
ENV_CLIENT_SECRET = "AO_CLIENT_SECRET"
TOKEN_PATH = "/api/v1/auth/token"
PAGE_LIMIT = 100  # the most a list endpoint returns per page
TIMEOUT_SECONDS = 60


class SyncError(Exception):
    """A failure the tool reports; the message never holds a secret."""


class ApiError(SyncError):
    """An error answer, RFC 9457 problem details: the status and detail."""

    def __init__(self, method, path, status, detail):
        super().__init__(f"HTTP {status} on {method} {path}: {detail}")
        self.status = status
        self.detail = detail


class ValidationError(SyncError):
    """POST /api/v1/workflows/validate said the definition is not valid."""

    def __init__(self, report):
        super().__init__(
            "the orchestrator refuses the definition: "
            f"{report.get('error_count')} error(s), "
            f"{report.get('warning_count')} warning(s)"
        )
        findings = report.get("findings")
        self.findings = findings if isinstance(findings, list) else []


class Client:
    """Bearer token, JSON bodies, paginated lists, problem details."""

    def __init__(self, url, insecure=False, timeout=TIMEOUT_SECONDS):
        self.base = url.rstrip("/")
        self.token = None
        self.timeout = timeout
        self.context = ssl.create_default_context()
        if insecure:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE

    def authenticate(self, client_id, client_secret):
        """Exchange the service account's credentials for a bearer token."""
        answer = self.request(
            "POST",
            TOKEN_PATH,
            form={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        token = answer.get("access_token") if isinstance(answer, dict) else None
        if not isinstance(token, str) or not token:
            raise SyncError(f"POST {TOKEN_PATH}: no access_token in the answer")
        self.token = token

    def request(self, method, path, body=None, form=None, query=None):
        """One call; the parsed JSON answer (None when the body is empty)."""
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout, context=self.context
            ) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(
                method, path, exc.code, _problem_detail(exc.read())
            ) from None
        except urllib.error.URLError as exc:
            raise SyncError(f"{method} {path}: {exc.reason}") from None
        except OSError as exc:  # a timeout or a connection reset mid-read
            raise SyncError(f"{method} {path}: {exc}") from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise SyncError(f"{method} {path}: the answer is not JSON") from None

    def list_all(self, path):
        """Every resource of a list endpoint, following next_cursor."""
        resources = []
        cursor = None
        while True:
            query = {}
            if cursor is not None:
                query["cursor"] = cursor
            query["limit"] = PAGE_LIMIT
            page = self.request("GET", path, query=query)
            if not isinstance(page, dict) or not isinstance(
                page.get("resources"), list
            ):
                raise SyncError(f"GET {path}: the answer has no resources list")
            resources.extend(page["resources"])
            cursor = page.get("next_cursor")
            if cursor in (None, ""):
                return resources


def _problem_detail(raw):
    """The detail of an RFC 9457 problem body, or what else the body says."""
    try:
        problem = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        problem = None
    if isinstance(problem, dict):
        for key in ("detail", "title", "message", "error"):
            value = problem.get(key)
            if isinstance(value, str) and value:
                return value
    text = raw.decode("utf-8", "replace").strip()
    return text[:200] if text else "no detail"


def find_by_name(resources, name, kind):
    """The one resource called name; none or more than one is an error."""
    matches = [
        item
        for item in resources
        if isinstance(item, dict) and item.get("name") == name
    ]
    if not matches:
        raise SyncError(f"no {kind} named {name!r}")
    if len(matches) > 1:
        raise SyncError(
            f"{len(matches)} {kind}s named {name!r}; the name must be unique"
        )
    if matches[0].get("id") in (None, ""):
        raise SyncError(f"{kind} {name!r} has no id")
    return matches[0]


def _items(doc, key):
    """The objects in the doc's list called key (triggers or nodes)."""
    value = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _params(item):
    params = item.get("parameters")
    return params if isinstance(params, dict) else {}


def placeholder_needs(doc):
    """The options whose values the doc's placeholders need."""
    needs = set()
    for node in _items(doc, "nodes"):
        if (
            node.get("type") in AAP_NODE_TYPES
            and _params(node).get("credential_id") == PLACEHOLDER
        ):
            needs.update(("--aap-credential", "--aap-integration"))
    for trigger in _items(doc, "triggers"):
        accounts = _params(trigger).get("authorized_service_account_ids")
        if isinstance(accounts, list) and PLACEHOLDER in accounts:
            needs.add("--service-account")
    return needs


def substitute(doc, credential_id=None, integration_id=None, service_account_id=None):
    """A copy of doc with the placeholders replaced: every aap_job_template
    or aap_workflow_job_template node whose credential_id is the
    placeholder gets the credential's id and, as integration_id, the
    integration's; every placeholder in a trigger's
    authorized_service_account_ids becomes the service account's id. A
    placeholder whose value is None stays, for leftover_placeholders()."""
    doc = json.loads(json.dumps(doc))
    if credential_id is not None and integration_id is not None:
        for node in _items(doc, "nodes"):
            params = _params(node)
            if (
                node.get("type") in AAP_NODE_TYPES
                and params.get("credential_id") == PLACEHOLDER
            ):
                params["credential_id"] = credential_id
                params["integration_id"] = integration_id
    if service_account_id is not None:
        for trigger in _items(doc, "triggers"):
            params = _params(trigger)
            accounts = params.get("authorized_service_account_ids")
            if isinstance(accounts, list):
                params["authorized_service_account_ids"] = [
                    service_account_id if item == PLACEHOLDER else item
                    for item in accounts
                ]
    return doc


def _placeholders(value, path):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_placeholders(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_placeholders(item, f"{path}[{index}]"))
    elif value == PLACEHOLDER:
        found.append(path)
    return found


def leftover_placeholders(doc):
    """Where REPLACE_AT_SYNC is still in doc, as paths; triggers and nodes
    are named by id (nodes[gate].parameters.credential_id)."""
    found = []
    for key, value in doc.items():
        if key in ("triggers", "nodes") and isinstance(value, list):
            for index, item in enumerate(value):
                label = str(index)
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    label = item["id"]
                found.extend(_placeholders(item, f"{key}[{label}]"))
        else:
            found.extend(_placeholders(value, key))
    return found


def normalise(doc):
    """The form two definitions are compared in. The orchestrator's export
    of a published version omits `credential_names` (it resolves the names
    at publish time and keeps no copy), so the field is left out of the
    comparison on both sides; a changed credential name alone does not
    count as a changed definition. An empty `extra_vars` is dropped from
    the export as well."""
    doc = json.loads(json.dumps(doc))
    for node in doc.get("nodes", []) if isinstance(doc, dict) else []:
        params = node.get("parameters") if isinstance(node, dict) else None
        if isinstance(params, dict):
            params.pop("credential_names", None)
            if params.get("extra_vars") == {}:
                params.pop("extra_vars")
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _identity(workflow, what):
    """A workflow object's id and current_version."""
    if (
        not isinstance(workflow, dict)
        or workflow.get("id") in (None, "")
        or workflow.get("current_version") is None
    ):
        raise SyncError(f"{what}: the workflow object has no id or current_version")
    return workflow["id"], workflow["current_version"]


def _publish(client, workflow_id, version, definition=None, change=None):
    """Publish version `version` as it stands, or, with `definition`, publish
    that definition as the next version (the orchestrator stores it under
    the next number and publishes it). Returns the workflow object."""
    body = {}
    if definition is not None:
        body = {
            "workflow_definition": definition,
            "change_description": change or "synced by tools/ao_sync.py",
            "expected_version": version,
        }
    answer = client.request(
        "POST", f"/api/v1/workflows/{workflow_id}/versions/{version}/publish", body=body
    )
    return answer if isinstance(answer, dict) else {}


def sync_definition(client, project_id, existing, doc, dry_run=False):
    """Validate doc (placeholders already substituted) and bring the
    project's workflow of its name in line with it; existing is the list of
    the project's workflows of that name. Returns (what was done, the
    validator's findings)."""
    name = doc["name"]
    if len(existing) > 1:
        raise SyncError(
            f"{len(existing)} workflows named {name!r} in the project; "
            "which one to update is ambiguous"
        )
    report = client.request(
        "POST", "/api/v1/workflows/validate", body={"workflow_definition": doc}
    )
    if not isinstance(report, dict):
        raise SyncError("POST /api/v1/workflows/validate: the answer is not an object")
    if report.get("is_valid") is not True:
        raise ValidationError(report)
    findings = report.get("findings")
    findings = findings if isinstance(findings, list) else []

    if not existing:
        if dry_run:
            return "would create and publish", findings
        created = client.request(
            "POST",
            "/api/v1/workflows",
            body={
                "name": name,
                "description": doc.get("description", ""),
                "project_id": project_id,
                "workflow_definition": doc,
                "is_import": True,
            },
        )
        workflow_id, version = _identity(created, "POST /api/v1/workflows")
        _publish(client, workflow_id, version)
        return f"created, published v{version}", findings

    workflow_id, version = _identity(existing[0], "GET /api/v1/workflows")
    current = client.request(
        "GET", f"/api/v1/workflows/{workflow_id}/versions/{version}/export"
    )
    if (
        isinstance(current, dict)
        and "workflow_definition" in current
        and "schema_version" not in current
    ):
        current = current["workflow_definition"]
    if normalise(current) == normalise(doc):
        if existing[0].get("published_version_number") == version:
            return f"unchanged, published v{version}", findings
        if dry_run:
            return f"unchanged, would publish v{version}", findings
        _publish(client, workflow_id, version)
        return f"unchanged, published v{version}", findings
    if dry_run:
        return f"would update and publish v{version + 1}", findings
    published = _publish(client, workflow_id, version, definition=doc)
    new_version = (
        published.get("published_version_number")
        or published.get("current_version")
        or version + 1
    )
    return f"updated, published v{new_version}", findings


def _format_finding(finding):
    if not isinstance(finding, dict):
        return str(finding)
    parts = []
    for key in ("severity", "node_id", "path", "field", "code", "message", "detail"):
        value = finding.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    return ": ".join(parts) if parts else json.dumps(finding, sort_keys=True)


class Definition:
    """One file: its parsed document, or why it cannot be imported."""

    def __init__(self, path):
        self.path = str(path)
        self.doc = None
        self.error = None
        try:
            doc = json.loads(pathlib.Path(path).read_bytes().decode("utf-8"))
        except OSError as exc:
            self.error = f"cannot read: {exc}"
            return
        except (UnicodeDecodeError, ValueError) as exc:
            self.error = f"not valid JSON: {exc}"
            return
        if not isinstance(doc, dict) or not isinstance(doc.get("name"), str):
            self.error = "the top level is not an object with a name"
            return
        if not doc["name"].strip():
            self.error = "the name is empty"
            return
        self.doc = doc

    @property
    def name(self):
        return self.doc["name"] if self.doc is not None else None


def _expand(parser, patterns):
    paths = []
    for pattern in patterns:
        if any(char in pattern for char in "*?["):
            matches = sorted(glob.glob(pattern))
            if not matches:
                parser.error(f"no file matches {pattern}")
            paths.extend(matches)
        else:
            paths.append(pattern)
    return paths


def _select(parser, paths, only):
    """The paths --only names, by path or by file name; all when none."""
    if not only:
        return set(paths)
    selected = set()
    for wanted in only:
        matches = [
            path
            for path in paths
            if path == wanted or pathlib.Path(path).name == pathlib.Path(wanted).name
        ]
        if not matches:
            parser.error(f"--only {wanted}: not one of the definitions")
        selected.update(matches)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ao_sync",
        description="Import automation orchestrator workflow definitions: create, "
        "update by name and publish; never delete.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="DEFINITION",
        help=f"workflow definition JSON files (default {DEFAULT_PATTERN}); "
        "a glob pattern is expanded here",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="the orchestrator, e.g. https://orchestrator.example.com",
    )
    parser.add_argument(
        "--project",
        default="default",
        metavar="NAME",
        help="the project the workflows belong to (default: default)",
    )
    parser.add_argument(
        "--aap-credential",
        metavar="NAME",
        help="the AAP credential whose id replaces every credential_id placeholder",
    )
    parser.add_argument(
        "--aap-integration",
        metavar="NAME",
        help="the integration whose id goes on the same nodes as integration_id",
    )
    parser.add_argument(
        "--service-account",
        metavar="NAME",
        help="the service account whose id replaces the authorized_service_account_ids "
        "placeholder (the EDA bridge)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="FILE",
        help="write only this definition (repeatable); the others still count as defined",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report the create, update and publish decisions; write nothing",
    )
    parser.add_argument(
        "--insecure", action="store_true", help="do not verify the TLS certificate"
    )
    args = parser.parse_args(argv)

    paths = _expand(parser, args.files or [DEFAULT_PATTERN])
    selected = _select(parser, paths, args.only)
    client_id = os.environ.get(ENV_CLIENT_ID, "")
    client_secret = os.environ.get(ENV_CLIENT_SECRET, "")
    if not client_id or not client_secret:
        parser.error(f"set {ENV_CLIENT_ID} and {ENV_CLIENT_SECRET} in the environment")

    definitions = [Definition(path) for path in paths]
    given = {
        "--aap-credential": args.aap_credential,
        "--aap-integration": args.aap_integration,
        "--service-account": args.service_account,
    }
    first_of = {}
    for definition in definitions:
        if definition.doc is None:
            continue
        other = first_of.setdefault(definition.name, definition.path)
        if other != definition.path:
            definition.error = f"name {definition.name!r} is also {other}'s"
            continue
        missing = sorted(
            option
            for option in placeholder_needs(definition.doc)
            if given[option] is None
        )
        if missing:
            definition.error = f"{PLACEHOLDER} needs " + " and ".join(missing)

    client = Client(args.url, insecure=args.insecure)
    ids = {}
    try:
        client.authenticate(client_id, client_secret)
        project = find_by_name(
            client.list_all("/api/v1/projects"), args.project, "project"
        )
        for option, path, kind in (
            ("--aap-credential", "/api/v1/credentials", "credential"),
            ("--aap-integration", "/api/v1/integrations", "integration"),
            ("--service-account", "/api/v1/service_accounts", "service account"),
        ):
            if given[option] is not None:
                found = find_by_name(client.list_all(path), given[option], kind)
                ids[option] = found["id"]
        workflows = [
            workflow
            for workflow in client.list_all("/api/v1/workflows")
            if isinstance(workflow, dict)
            and workflow.get("project_id") == project["id"]
        ]
    except SyncError as exc:
        print(f"ao_sync: {exc}; nothing was written", file=sys.stderr)
        return 1
    by_name = {}
    for workflow in workflows:
        by_name.setdefault(workflow.get("name"), []).append(workflow)

    done = failed = 0
    for definition in definitions:
        if definition.path not in selected:
            continue
        findings = []
        if definition.error is None:
            doc = substitute(
                definition.doc,
                ids.get("--aap-credential"),
                ids.get("--aap-integration"),
                ids.get("--service-account"),
            )
            left = leftover_placeholders(doc)
            if left:
                definition.error = f"{PLACEHOLDER} left in " + ", ".join(left)
        if definition.error is None:
            try:
                text, findings = sync_definition(
                    client,
                    project["id"],
                    by_name.get(definition.name, []),
                    doc,
                    args.dry_run,
                )
            except ValidationError as exc:
                definition.error = str(exc)
                findings = exc.findings
            except SyncError as exc:
                definition.error = str(exc)
        if definition.error is None:
            done += 1
            print(f"{definition.path}: {text}")
        else:
            failed += 1
            print(f"{definition.path}: error: {definition.error}")
        for finding in findings:
            print("    " + _format_finding(finding))

    defined = {definition.name for definition in definitions if definition.doc}
    extras = sorted(str(name) for name in by_name if name not in defined)
    for name in extras:
        print(f"extra: {name} (in the project, no definition file; left alone)")

    note = " (dry run, nothing written)" if args.dry_run else ""
    print(
        f"ao_sync: {done} ok, {failed} failed, {len(extras)} extra{note}",
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
