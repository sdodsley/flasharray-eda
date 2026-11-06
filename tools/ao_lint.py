#!/usr/bin/env python3
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Offline linter for the automation orchestrator workflow definitions under
orchestrator/. Standard library only.

    python tools/ao_lint.py orchestrator/*.json [--allowed-templates FILE]

Prints one line per problem as file:node:rule:message and exits 1 when there
is any, 2 on a usage error, 0 when every definition is clean. The rules
encode the contract described in orchestrator/README.md: the definition
shape the orchestrator imports, the trigger and node types it knows, the
ports its approval, condition and switch nodes expose, the wiring mistakes
that only show up at run time (one successor shared by approved and
rejected, a cycle, a plain node fed by two branches, a node nothing leads
to, a from_port on a node without ports, an expression that names a node
that has not run yet or a trigger field the trigger does not expose, a
condition the safe evaluator cannot parse), a parameter a type's schema
does not have (the orchestrator refuses the import) and the published
caps. An edge is {from, to, from_port?, to_port?}; how a loop node's body
returns to it is not part of the checked contract, so no back edge is
exempt from the cycle rule (orchestrator/README.md, "Checked against a
live instance"). A shell that does not expand globs can pass them as-is.
"""

import argparse
import ast
import collections
import glob
import ipaddress
import json
import pathlib
import re
import sys
from urllib.parse import urlsplit

SCHEMA_VERSION = "2.0.0"
ID_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
EXPRESSION_RE = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)")
REQUIRED_TOP_LEVEL = (
    "schema_version",
    "name",
    "description",
    "triggers",
    "nodes",
    "edges",
)
TRIGGER_TYPES = (
    "manual_trigger",
    "scheduled_trigger",
    "webhook_trigger",
    "eda_trigger",
)
NODE_TYPES = (
    "aap_job_template",
    "aap_workflow_job_template",
    "approval",
    "condition",
    "switch",
    "loop",
    "converge",
    "wait",
    "http_request",
    "script",
    "agentic",
)
EDGE_KEYS = ("from", "to", "from_port", "to_port")
# expression namespaces that are not node ids; a trigger's id is an alias of
# trigger (the engine registers the trigger output under both names)
EXPRESSION_NAMESPACES = ("trigger", "workflow_context", "loop")
# the node types whose outgoing edges carry a from_port (approval: approved,
# rejected; condition: true, false; switch: its cases; loop: iterate,
# complete); nothing else has ports, triggers included
PORTED_NODE_TYPES = ("approval", "condition", "switch", "loop")
# the parameters each trigger and node type accepts, from the orchestrator's
# node schemas (additionalProperties false: an unknown key is refused at
# import). A type not listed here has not had its schema checked and is not
# held to a key set.
PARAMETER_KEYS = {
    "manual_trigger": ("input_schema",),
    "webhook_trigger": (
        "webhook_path",
        "input_schema",
        "authorized_service_account_ids",
    ),
    "eda_trigger": ("webhook_path", "input_schema", "authorized_service_account_ids"),
    "scheduled_trigger": (
        "schedule_type",
        "cron",
        "interval",
        "timezone",
        "missed_schedule_policy",
    ),
    "aap_job_template": (
        "credential_id",
        "integration_id",
        "job_template_id",
        "job_template_name",
        "organization_name",
        "organization_id",
        "inventory_id",
        "inventory_name",
        "job_credentials",
        "credential_names",
        "extra_vars",
        "limit",
        "tags",
        "skip_tags",
        "verbosity",
        "timeout",
        "job_type",
        "forks",
        "job_slicing",
        "diff_mode",
        "execution_environment",
        "instance_group_id",
        "instance_group_name",
        "labels",
    ),
    "approval": (
        "credential_id",
        "approver_users",
        "approver_groups",
        "prompt",
        "fallback_decision",
        "decision_window",
    ),
    "condition": ("condition",),
    "switch": ("cases", "default_port"),
}
# what the safe evaluator accepts in a condition (expression-system.md):
# comparisons, and/or/not, in/not in, names and literals. Every ${...} is
# replaced by a name before parsing, so Load is the only context that occurs.
CONDITION_AST_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Gt,
    ast.Lt,
    ast.GtE,
    ast.LtE,
    ast.In,
    ast.NotIn,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
)
MAX_NODES = 200
MAX_EDGES = 500
MAX_BYTES = 1536 * 1024  # 1.5 MB serialized

# every rule the linter can report; tests/unit/test_ao_lint.py requires a
# failing fixture for each one
RULES = (
    "json",
    "schema_version",
    "top_level",
    "trigger_type",
    "trigger_parameters",
    "parameter_keys",
    "node_keys",
    "node_type",
    "id_format",
    "id_unique",
    "edge_keys",
    "edge_ref",
    "edge_target",
    "unreachable",
    "approval_ports",
    "approval_approvers",
    "condition_expression",
    "condition_ports",
    "switch_cases",
    "converge_inputs",
    "single_input",
    "cycle",
    "cap_nodes",
    "cap_edges",
    "cap_size",
    "job_template",
    "job_template_allowed",
    "agentic",
    "http_request_ip",
    "expression_ref",
    "trigger_ref",
)

Problem = collections.namedtuple("Problem", "file node rule message")


def format_problem(problem):
    return "{}:{}:{}:{}".format(*problem)


class _Collector:
    def __init__(self, shown):
        self.shown = shown
        self.problems = []

    def add(self, node, rule, message):
        if rule not in RULES:  # pragma: no cover - a programming error
            raise ValueError(f"unknown rule {rule}")
        self.problems.append(Problem(self.shown, node, rule, message))


def load_allowed_templates(path):
    """One job template name per line; blank lines and # comments ignored."""
    names = set()
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(line)
    return names


def lint_file(path, allowed_templates=None):
    """Lint one definition file. Returns a list of Problem."""
    path = pathlib.Path(path)
    out = _Collector(str(path))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        out.add("", "json", f"cannot read: {exc}")
        return out.problems
    if len(raw) > MAX_BYTES:
        out.add("", "cap_size", f"{len(raw)} bytes serialized, the cap is {MAX_BYTES}")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        out.add("", "json", f"not valid JSON: {exc}")
        return out.problems
    out.problems.extend(lint_definition(doc, allowed_templates, str(path)))
    return out.problems


def lint_definition(doc, allowed_templates=None, shown=""):
    """Lint a parsed definition. Returns a list of Problem."""
    out = _Collector(shown)
    if not isinstance(doc, dict):
        out.add("", "json", "the top level is not a JSON object")
        return out.problems

    missing = [key for key in REQUIRED_TOP_LEVEL if key not in doc]
    if missing:
        out.add("", "top_level", "missing " + ", ".join(missing))
    for key in ("name", "description"):
        if key in doc and not isinstance(doc[key], str):
            out.add("", "top_level", f"{key} is not a string")
    if "schema_version" in doc and doc["schema_version"] != SCHEMA_VERSION:
        out.add(
            "",
            "schema_version",
            f"schema_version is {doc['schema_version']!r}, expected {SCHEMA_VERSION!r}",
        )
    lists = {}
    for key in ("triggers", "nodes", "edges"):
        value = doc.get(key, [])
        if not isinstance(value, list):
            out.add("", "top_level", f"{key} is not a list")
            value = []
        lists[key] = value
    triggers, nodes, edges = lists["triggers"], lists["nodes"], lists["edges"]

    if len(nodes) > MAX_NODES:
        out.add("", "cap_nodes", f"{len(nodes)} nodes, the cap is {MAX_NODES}")
    if len(edges) > MAX_EDGES:
        out.add("", "cap_edges", f"{len(edges)} edges, the cap is {MAX_EDGES}")

    seen = set()
    trigger_ids = set()
    node_by_id = {}
    for index, trigger in enumerate(triggers):
        label = _label(trigger, f"triggers[{index}]")
        if not isinstance(trigger, dict):
            out.add(label, "node_keys", "trigger is not an object")
            continue
        for key in ("id", "type"):
            if key not in trigger:
                out.add(label, "node_keys", f"trigger has no {key}")
        if _check_id(out, trigger.get("id"), label, seen):
            trigger_ids.add(trigger["id"])
        ttype = trigger.get("type")
        if "type" in trigger and ttype not in TRIGGER_TYPES:
            out.add(
                label,
                "trigger_type",
                f"unknown trigger type {ttype!r}; one of " + ", ".join(TRIGGER_TYPES),
            )
        _check_trigger_parameters(out, trigger, label)
        _check_parameter_keys(out, label, ttype, trigger.get("parameters"))

    for index, node in enumerate(nodes):
        label = _label(node, f"nodes[{index}]")
        if not isinstance(node, dict):
            out.add(label, "node_keys", "node is not an object")
            continue
        for key in ("id", "type", "name"):
            if key not in node:
                out.add(label, "node_keys", f"node has no {key}")
        if _check_id(out, node.get("id"), label, seen):
            node_by_id[node["id"]] = node
        if "type" in node and node["type"] not in NODE_TYPES:
            out.add(
                label,
                "node_type",
                f"unknown node type {node['type']!r}; one of " + ", ".join(NODE_TYPES),
            )

    # the triggers whose type is known decide what ${trigger...} may read
    typed_triggers = [
        trigger
        for trigger in triggers
        if isinstance(trigger, dict) and trigger.get("type") in TRIGGER_TYPES
    ]

    all_ids = trigger_ids | set(node_by_id)
    incoming = collections.defaultdict(list)
    outgoing = collections.defaultdict(list)
    graph = {node_id: set() for node_id in all_ids}
    for index, edge in enumerate(edges):
        label = f"edges[{index}]"
        if not isinstance(edge, dict):
            out.add(label, "edge_keys", "edge is not an object")
            continue
        src, dst = edge.get("from"), edge.get("to")
        if isinstance(src, str):
            label = src
        unknown = sorted(set(edge) - set(EDGE_KEYS))
        if unknown:
            out.add(
                label,
                "edge_keys",
                "unknown keys "
                + ", ".join(unknown)
                + "; an edge has "
                + ", ".join(EDGE_KEYS),
            )
        resolved = True
        for key, value in (("from", src), ("to", dst)):
            if key not in edge:
                out.add(label, "edge_keys", f"edge has no {key}")
                resolved = False
            elif value not in all_ids:
                out.add(
                    label, "edge_ref", f"{key} {value!r} is not a trigger or node id"
                )
                resolved = False
        if not resolved:
            continue
        if dst in trigger_ids:
            out.add(
                label,
                "edge_target",
                f"edge to {dst!r}: an edge cannot end at a trigger",
            )
            continue
        outgoing[src].append(edge)
        incoming[dst].append(edge)
        graph[src].add(dst)

    for trigger_id in sorted(trigger_ids):
        for edge in outgoing[trigger_id]:
            if "from_port" in edge:
                out.add(
                    trigger_id,
                    "edge_keys",
                    f"from_port {edge['from_port']!r} on an edge leaving a trigger, "
                    "which has no ports",
                )

    ancestors = _ancestors(node_by_id, incoming)
    for node_id, node in node_by_id.items():
        if not incoming[node_id]:
            out.add(node_id, "unreachable", "no edge leads to this node; it never runs")
        _check_node(
            out, node_id, node, outgoing[node_id], incoming[node_id], allowed_templates
        )
        # ${trigger...} and ${<trigger id>...} read the same output
        _check_expressions(
            out,
            node_id,
            node,
            set(node_by_id),
            ancestors[node_id],
            typed_triggers,
            trigger_ids,
        )

    _check_cycles(out, graph)
    return out.problems


def _ancestors(node_by_id, incoming):
    """node id -> the ids of the nodes that run before it, found by walking
    the incoming edges breadth first; triggers are not nodes and are left
    out, and a cycle (reported separately) does not stop the walk."""
    result = {}
    for node_id in node_by_id:
        seen = set()
        queue = collections.deque(edge["from"] for edge in incoming[node_id])
        while queue:
            src = queue.popleft()
            if src in seen or src not in node_by_id:
                continue
            seen.add(src)
            queue.extend(edge["from"] for edge in incoming[src])
        result[node_id] = seen
    return result


def _label(item, fallback):
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return item["id"]
    return fallback


def _check_id(out, item_id, label, seen):
    """Record a well-formed, unused id; report a bad or repeated one."""
    if item_id is None:
        return False
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        out.add(label, "id_format", f"id {item_id!r} does not match {ID_RE.pattern}")
        return False
    if item_id in seen:
        out.add(
            label,
            "id_unique",
            f"id {item_id!r} is used by more than one trigger or node",
        )
        return False
    seen.add(item_id)
    return True


def _check_trigger_parameters(out, trigger, label):
    params = trigger.get("parameters")
    if not isinstance(params, dict):
        out.add(
            label,
            "trigger_parameters",
            "parameters object missing ({} for a manual trigger)",
        )
        return
    ttype = trigger.get("type")
    if ttype in ("webhook_trigger", "eda_trigger"):
        if not _text(params.get("webhook_path")):
            out.add(label, "trigger_parameters", f"{ttype} has no webhook_path")
        accounts = params.get("authorized_service_account_ids")
        if not isinstance(accounts, list) or not accounts:
            out.add(
                label,
                "trigger_parameters",
                f"{ttype} has no authorized_service_account_ids "
                "(REPLACE_AT_SYNC placeholders count)",
            )
    if ttype == "scheduled_trigger":
        kind = params.get("schedule_type")
        if kind not in ("cron", "interval"):
            out.add(
                label,
                "trigger_parameters",
                "scheduled_trigger has no schedule_type (cron or interval)",
            )
        elif not _text(params.get(kind)):
            out.add(
                label,
                "trigger_parameters",
                f"scheduled_trigger with schedule_type {kind} has no {kind}",
            )


def _check_parameter_keys(out, label, item_type, params):
    """A trigger or node of a type whose schema is known carries only the
    parameters that schema lists."""
    allowed = PARAMETER_KEYS.get(item_type)
    if allowed is None or not isinstance(params, dict):
        return
    unknown = sorted(key for key in params if key not in allowed)
    if unknown:
        out.add(
            label,
            "parameter_keys",
            f"{item_type} has no parameter "
            + ", ".join(unknown)
            + "; it takes "
            + ", ".join(allowed),
        )


def _text(value):
    return isinstance(value, str) and value.strip() != ""


def _ref_id(value):
    """A template id: non-empty text or a positive integer (not a bool)."""
    if isinstance(value, bool):
        return False
    return _text(value) or (isinstance(value, int) and value > 0)


def _check_node(out, node_id, node, outs, ins, allowed_templates):
    ntype = node.get("type")
    params = node.get("parameters") if isinstance(node.get("parameters"), dict) else {}
    _check_parameter_keys(out, node_id, ntype, node.get("parameters"))
    if ntype == "converge":
        if len(ins) < 2:
            out.add(
                node_id,
                "converge_inputs",
                f"converge node has {len(ins)} incoming edge(s); it needs at least two",
            )
    elif len(ins) > 1:
        sources = ", ".join(sorted(str(edge.get("from")) for edge in ins))
        out.add(
            node_id,
            "single_input",
            f"{len(ins)} incoming edges (from {sources}); a plain node runs on the first "
            "predecessor that finishes, so join branches with a converge node",
        )
    ports = collections.defaultdict(list)
    for edge in outs:
        ports[edge.get("from_port")].append(edge.get("to"))
    if ntype not in PORTED_NODE_TYPES:
        for edge in outs:
            if "from_port" in edge:
                out.add(
                    node_id,
                    "edge_keys",
                    f"from_port {edge['from_port']!r} on an edge leaving a node of "
                    f"type {ntype}, which has no ports",
                )

    if ntype == "approval":
        for port in ("approved", "rejected"):
            if port not in ports:
                out.add(node_id, "approval_ports", f"no edge leaves port {port!r}")
        unknown = sorted(
            str(port) for port in ports if port not in ("approved", "rejected")
        )
        if unknown:
            out.add(
                node_id,
                "approval_ports",
                "edges leave unknown ports " + ", ".join(unknown),
            )
        shared = sorted(set(ports.get("approved", ())) & set(ports.get("rejected", ())))
        if shared:
            out.add(
                node_id,
                "approval_ports",
                "approved and rejected both lead to "
                + ", ".join(shared)
                + "; give each port its own successor",
            )
        approvers = [
            key
            for key in ("approver_groups", "approver_users")
            if isinstance(params.get(key), list) and params[key]
        ]
        if not approvers:
            out.add(
                node_id,
                "approval_approvers",
                "lists neither approver_groups nor approver_users (group and user names)",
            )

    elif ntype == "condition":
        condition = params.get("condition")
        if not _text(condition):
            out.add(
                node_id,
                "condition_expression",
                "parameters.condition is missing or empty",
            )
        else:
            reason = _condition_problem(condition)
            if reason:
                out.add(
                    node_id, "condition_expression", f"parameters.condition {reason}"
                )
        for port in ("true", "false"):
            if port not in ports:
                out.add(node_id, "condition_ports", f"no edge leaves port {port!r}")
        unknown = sorted(str(port) for port in ports if port not in ("true", "false"))
        if unknown:
            out.add(
                node_id,
                "condition_ports",
                "edges leave unknown ports " + ", ".join(unknown),
            )

    elif ntype == "switch":
        known_ports = set()
        default_port = params.get("default_port")
        if not _text(default_port):
            out.add(node_id, "switch_cases", "no default_port")
        else:
            known_ports.add(default_port)
        cases = params.get("cases")
        if not isinstance(cases, list) or not cases:
            out.add(node_id, "switch_cases", "no cases")
            cases = []
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                out.add(node_id, "switch_cases", f"cases[{index}] is not an object")
                continue
            for key in ("port", "label", "condition"):
                if not _text(case.get(key)):
                    out.add(node_id, "switch_cases", f"cases[{index}] has no {key}")
            if _text(case.get("condition")):
                reason = _condition_problem(case["condition"])
                if reason:
                    out.add(
                        node_id,
                        "condition_expression",
                        f"cases[{index}].condition {reason}",
                    )
            if _text(case.get("port")):
                known_ports.add(case["port"])
        unknown = sorted(str(port) for port in ports if port not in known_ports)
        if unknown:
            out.add(
                node_id,
                "switch_cases",
                "edges leave ports that are neither a case nor the default: "
                + ", ".join(unknown),
            )

    elif ntype in ("aap_job_template", "aap_workflow_job_template"):
        # both reference their template by name (plus organization_name) or
        # by id, and both launch through the AAP credential; a node with no
        # parameters object has none of these
        if ntype == "aap_job_template":
            kind, name_key, id_key = (
                "job template",
                "job_template_name",
                "job_template_id",
            )
        else:
            kind = "workflow job template"
            name_key, id_key = "workflow_job_template_name", "workflow_job_template_id"
        for key in ("organization_name", "credential_id"):
            if not _text(params.get(key)):
                out.add(
                    node_id, "job_template", f"parameters.{key} is missing or empty"
                )
        name = params.get(name_key)
        if not _text(name) and not _ref_id(params.get(id_key)):
            out.add(
                node_id,
                "job_template",
                f"parameters has neither {name_key} nor {id_key}",
            )
        if (
            allowed_templates is not None
            and _text(name)
            and name not in allowed_templates
        ):
            out.add(
                node_id,
                "job_template_allowed",
                f"{kind} {name!r} is not in the allowed list",
            )

    elif ntype == "agentic":
        for key in ("credential_id", "prompt"):
            if not _text(params.get(key)):
                out.add(node_id, "agentic", f"parameters.{key} is missing or empty")

    elif ntype == "http_request":
        url = params.get("url")
        if _text(url):
            try:
                host = urlsplit(url).hostname
            except ValueError:
                host = None
            if host is not None:
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    pass
                else:
                    out.add(
                        node_id,
                        "http_request_ip",
                        f"url {url!r} addresses a bare IP; use a host name",
                    )


def _condition_problem(expr):
    """None when the safe evaluator can take expr, else why it cannot: with
    every ${...} replaced by a name, what is left must parse as a Python
    expression built from comparisons, and/or/not, in/not in, names and
    literals (the operators expression-system.md lists)."""
    plain = re.sub(r"\$\{[^}]*\}", "_x_", expr)
    try:
        tree = ast.parse(plain, mode="eval")
    except (SyntaxError, ValueError) as exc:
        column = getattr(exc, "offset", None) or 0
        return f"is not an expression (syntax error at column {column}): {expr!r}"
    for item in ast.walk(tree):
        if not isinstance(item, CONDITION_AST_NODES):
            return (
                f"uses {type(item).__name__}, which the evaluator does not have "
                f"(== != > < >= <= and or not in, names and literals): {expr!r}"
            )
    return None


def _input_properties(params):
    """The property names of a trigger's input_schema, or None when it
    declares none (then any ${trigger.<name>} may be right)."""
    schema = params.get("input_schema")
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    return set(properties)


def _trigger_rest(raw):
    """'.event.array' -> 'event.array'."""
    rest = raw.strip()
    if rest.startswith("."):
        rest = rest[1:].strip()
    return rest


def _trigger_ref_problem(rest, triggers):
    """None when one of the triggers exposes ${trigger.<rest>}: a webhook or
    EDA trigger passes its body through unwrapped and a manual trigger its
    inputs, so all three expose the properties of their input_schema
    (anything, when they declare none); a scheduled trigger has
    scheduled_at and triggered_at. Else a message saying what the triggers
    do expose."""
    first = re.split(r"[.\[\s]", rest, maxsplit=1)[0]
    exposes = []
    for trigger in triggers:
        ttype = trigger.get("type")
        params = trigger.get("parameters")
        if not isinstance(params, dict):
            params = {}
        if ttype == "scheduled_trigger":
            if rest in ("scheduled_at", "triggered_at"):
                return None
            exposes.append(
                "scheduled_trigger exposes only scheduled_at and triggered_at"
            )
        else:
            properties = _input_properties(params)
            if properties is None or first in properties:
                return None
            exposes.append(
                f"{ttype}'s input_schema has "
                + (", ".join(sorted(properties)) or "no properties")
                + ("" if ttype == "manual_trigger" else " (the body is not wrapped)")
            )
    shown = "${trigger" + ("." + rest if rest else "") + "}"
    return f"{shown} resolves to nothing: " + "; ".join(exposes)


def _check_expressions(
    out, node_id, node, node_ids, ancestors, triggers, trigger_ids=frozenset()
):
    """Every ${name....} in the node's parameters names a node that runs
    before this one, one of the orchestrator's namespaces or a trigger id
    (an alias of trigger), and every ${trigger...} or ${<trigger id>...}
    reads what the trigger exposes."""
    params = node.get("parameters")
    if params is None:
        return
    text = json.dumps(params)
    for name in sorted(set(EXPRESSION_RE.findall(text))):
        if name in node_ids:
            if name not in ancestors:
                out.add(
                    node_id,
                    "expression_ref",
                    f"${{{name}...}} names a node that does not run before this one",
                )
        elif name not in EXPRESSION_NAMESPACES and name not in trigger_ids:
            out.add(
                node_id,
                "expression_ref",
                f"${{{name}...}} references no node id, trigger id or namespace "
                "(" + ", ".join(EXPRESSION_NAMESPACES) + ")",
            )
    if not triggers:
        return
    names = "|".join(re.escape(name) for name in ("trigger", *sorted(trigger_ids)))
    ref_re = re.compile(r"\$\{\s*(?:" + names + r")\b\s*([^}]*)\}")
    for rest in sorted({_trigger_rest(raw) for raw in ref_re.findall(text)}):
        message = _trigger_ref_problem(rest, triggers)
        if message:
            out.add(node_id, "trigger_ref", message)


def _check_cycles(out, graph):
    """Kahn's algorithm over every edge; what is left with an incoming edge
    sits on a cycle."""
    indegree = {node_id: 0 for node_id in graph}
    for targets in graph.values():
        for target in targets:
            indegree[target] += 1
    ready = sorted(
        (node_id for node_id, count in indegree.items() if count == 0), reverse=True
    )
    while ready:
        node_id = ready.pop()
        for target in sorted(graph[node_id], reverse=True):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    stuck = sorted(node_id for node_id, count in indegree.items() if count > 0)
    if stuck:
        out.add(
            stuck[0],
            "cycle",
            "cycle through "
            + ", ".join(stuck)
            + "; edges only run forward (a loop node's return wiring is not "
            "part of the published model)",
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ao_lint",
        description="Lint automation orchestrator workflow definitions offline.",
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="DEFINITION",
        help="workflow definition JSON files; a glob pattern is expanded here",
    )
    parser.add_argument(
        "--allowed-templates",
        metavar="FILE",
        help="one template name per line; an aap_job_template or aap_workflow_job_template "
        "node that names its template must name one of them",
    )
    args = parser.parse_args(argv)

    paths = []
    for pattern in args.files:
        if any(char in pattern for char in "*?["):
            matches = sorted(glob.glob(pattern))
            if not matches:
                parser.error(f"no file matches {pattern}")
            paths.extend(matches)
        else:
            paths.append(pattern)
    allowed = None
    if args.allowed_templates:
        try:
            allowed = load_allowed_templates(args.allowed_templates)
        except OSError as exc:
            parser.error(f"cannot read {args.allowed_templates}: {exc}")

    problems = []
    for path in paths:
        problems.extend(lint_file(path, allowed))
    for problem in problems:
        print(format_problem(problem))
    if problems:
        print(
            f"ao_lint: {len(problems)} problem(s) in {len(paths)} definition(s)",
            file=sys.stderr,
        )
        return 1
    print(f"ao_lint: {len(paths)} definition(s) ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
