# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Static checks over the rulebooks: they validate against ansible-rulebook's
own schema, every variable they reference is in vars/example.yml, every
playbook they run exists, and the replay copies in tests/ have not drifted
from the real rules."""

import json
import pathlib
import re

import jinja2
import pytest
import yaml
from jinja2 import nodes

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

RULEBOOKS = sorted(pathlib.Path("extensions/eda/rulebooks").glob("*.yml"))
REPLAYS = sorted(pathlib.Path("tests").glob("replay_*.yml"))
PLATFORM_RULEBOOKS = sorted(
    pathlib.Path("extensions/eda/rulebooks/platform").glob("*.yml")
)
VAR_REF = re.compile(r"\bvars\.([a-z_][a-z0-9_]*)")
JINJA_REF = re.compile(r"{{\s*([a-z_][a-z0-9_]*)\b")


def _rulebook_schema():
    try:
        import ansible_rulebook
    except ImportError:
        return None
    for candidate in pathlib.Path(ansible_rulebook.__file__).parent.rglob(
        "ruleset_schema.json"
    ):
        return json.loads(candidate.read_text())
    return None


SCHEMA = _rulebook_schema()


@pytest.mark.parametrize("path", RULEBOOKS + REPLAYS, ids=lambda p: p.name)
def test_rulebook_matches_the_ansible_rulebook_schema(path):
    if SCHEMA is None or jsonschema is None:
        pytest.skip("ansible-rulebook or jsonschema not installed")
    doc = yaml.safe_load(path.read_text())
    jsonschema.validate(doc, SCHEMA)


def _example_vars(repo_root):
    return set(yaml.safe_load((repo_root / "vars" / "example.yml").read_text()))


@pytest.mark.parametrize("path", RULEBOOKS, ids=lambda p: p.name)
def test_every_referenced_var_is_documented(path, repo_root):
    text = path.read_text()
    known = _example_vars(repo_root)
    referenced = set(VAR_REF.findall(text))
    # {{ name }} at the top level of a rulebook is a vars-file key too, except
    # event/events/fact(s) and the loop-ish names rulebooks use
    for name in JINJA_REF.findall(text):
        if name not in ("event", "events", "fact", "facts", "vars"):
            referenced.add(name)
    missing = sorted(referenced - known)
    assert (
        not missing
    ), f"{path.name} references vars not in vars/example.yml: {missing}"


@pytest.mark.parametrize("path", RULEBOOKS + REPLAYS, ids=lambda p: p.name)
def test_every_playbook_a_rule_runs_exists(path, repo_root):
    doc = yaml.safe_load(path.read_text())
    for ruleset in doc:
        for rule in ruleset.get("rules", []):
            run = rule.get("action", {}).get("run_playbook")
            if run:
                assert (
                    repo_root / run["name"]
                ).exists(), f"{path.name}: {run['name']} does not exist"


def _rules_by_name(path):
    out = {}
    for ruleset in yaml.safe_load(path.read_text()):
        for rule in ruleset.get("rules", []):
            out[rule["name"]] = rule
    return out


@pytest.mark.parametrize("replay", REPLAYS, ids=lambda p: p.name)
def test_replay_rules_match_the_real_rulebook(replay):
    """tests/replay_<x>.yml copies rules from extensions/eda/rulebooks/<x>.yml
    (ansible-rulebook has no include). Every copied rule must be identical,
    throttle included: each fixture hits a throttled rule once with its own
    key and every run is a fresh process, so nothing is suppressed and the
    throttle form gets validated too."""
    real = pathlib.Path("extensions/eda/rulebooks") / replay.name.replace("replay_", "")
    assert real.exists(), f"no real rulebook for {replay.name}"
    real_rules = _rules_by_name(real)
    copied = _rules_by_name(replay)
    for name, rule in copied.items():
        assert name in real_rules, f"{replay.name}: rule '{name}' is not in {real.name}"
        assert (
            rule == real_rules[name]
        ), f"{replay.name}: rule '{name}' has drifted from {real.name}"
    # first match wins: the copies must keep the real rulebook's order
    real_order = [n for n in real_rules if n in copied]
    assert (
        list(copied) == real_order
    ), f"{replay.name}: rules are not in {real.name}'s order"


# the project's two sources emit plain text themselves; the tick carries none
OWN_OR_CLOCK = (
    "sdodsley.flasharray_eda.purefa_events",
    "sdodsley.flasharray_eda.purefa_syslog",
    "ansible.eda.tick",
)
PLAIN_TEXT = "sdodsley.flasharray_eda.plain_text"


@pytest.mark.parametrize("path", RULEBOOKS + PLATFORM_RULEBOOKS, ids=str)
def test_every_other_source_carries_the_plain_text_filter(path):
    """Text from Alertmanager, a webhook body or any source added later
    reaches the rules as plain text: every source that is not one of the
    project's own names the project's filter."""
    for ruleset in yaml.safe_load(path.read_text()):
        for entry in ruleset.get("sources") or []:
            source = next(k for k in entry if k not in ("name", "filters"))
            if source in OWN_OR_CLOCK:
                continue
            names = [next(iter(f)) for f in entry.get("filters") or []]
            assert (
                PLAIN_TEXT in names
            ), f"{path}: {ruleset['name']} / {source} has no {PLAIN_TEXT} filter"


def _strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


ALL_RULEBOOKS = RULEBOOKS + PLATFORM_RULEBOOKS + REPLAYS
JINJA = jinja2.Environment()
MARKERS = ("{{", "{%", "{#", "#jinja2")
VAULT_HEADER = "$ANSIBLE_VAULT"  # matched as written
OPENING_WORD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)[ =:.]")
TEXT_VALUES = (
    "plain text",
    "['a', 'b']",
    "{'a': 1}",
    "007",
    "85.2",
    "True",
    'say "hi"',
    "it's",
    "",
    "'x' #",
    "'a' 'b'",
    "  leading and trailing  ",
    "long " * 60,
)
TYPED_VALUES = (7, 2.5, True, None, ["a b", 3], {"k": "v w"})
# text that ends where a marker could start, and text that could complete one
TAILS = (
    "{",
    "x{",
    "#",
    "#jin",
    "#jinja2",
    "#JINJA2",
    "$",
    "$ANSIBLE_VAUL",
    "x $ANSIBLE_VAULT;1.1",
)
HEADS = (
    "{ x }}",
    "% x %}",
    "# x #}",
    "jinja2:x",
    "ja2:x",
    "2:x",
    ":x",
    "ANSIBLE_VAULT;1.1",
    "T;1.1",
    "",
)


def _action_templates(path):
    """Every action string that holds a template: the rule's name, the string
    and its parsed tree."""
    for ruleset in yaml.safe_load(path.read_text()):
        for rule in ruleset.get("rules") or []:
            actions = rule.get("actions") or [rule.get("action") or {}]
            for action in actions:
                for text in _strings(action):
                    if "{" in text:
                        yield rule["name"], text, JINJA.parse(text)


def _event_path(node):
    """("event", "a", "b") for event.a.b or event["a"]["b"], else None."""
    parts = []
    while True:
        if isinstance(node, nodes.Getattr):
            parts.append(node.attr)
        elif (
            isinstance(node, nodes.Getitem)
            and isinstance(node.arg, nodes.Const)
            and isinstance(node.arg.value, str)
        ):
            parts.append(node.arg.value)
        else:
            break
        node = node.node
    if isinstance(node, nodes.Name) and node.name in ("event", "events"):
        return (node.name, *reversed(parts))
    return None


def _event_paths(node):
    """The event fields a tree reads, in the order the string names them."""
    path = _event_path(node)
    if path:
        yield path
        return
    for child in node.iter_child_nodes():
        yield from _event_paths(child)


def _event_strings(path):
    """The action templates that read an event field."""
    for rule, text, tree in _action_templates(path):
        if next(_event_paths(tree), None):
            yield rule, text, tree


def _event_strings_once():
    """Each distinct action template that reads an event field, with its
    parsed tree and every `file: rule` that holds it. The platform and replay
    files repeat the standalone strings, and a render depends only on the
    string and the vars file."""
    found = {}
    for path in ALL_RULEBOOKS:
        for rule, text, tree in _event_strings(path):
            found.setdefault(text, (tree, []))[1].append(f"{path}: {rule}")
    return found


def _numbers(tree):
    """The event fields a string compares or does arithmetic on."""
    return {
        path
        for node in tree.find_all((nodes.Compare, nodes.BinExpr))
        for path in _event_paths(node)
    }


def _literals(tree):
    """The text a string holds itself: what stands between its expressions
    and every quoted literal inside them, in a branch taken or not."""
    for node in tree.find_all((nodes.Const, nodes.TemplateData)):
        value = node.data if isinstance(node, nodes.TemplateData) else node.value
        if isinstance(value, str):
            yield value


def _one_expression(tree):
    """The expression of a string that is one `{{ ... }}` and nothing else."""
    if len(tree.body) == 1 and isinstance(tree.body[0], nodes.Output):
        parts = tree.body[0].nodes
        if len(parts) == 1 and not isinstance(parts[0], nodes.TemplateData):
            return parts[0]
    return None


def _opens_with_text(tree):
    first = tree.body[0]
    return isinstance(first, nodes.Output) and isinstance(
        first.nodes[0], nodes.TemplateData
    )


def _operand(text):
    """What a one-expression string hands to `| pprint`, as source."""
    inner = text.strip()[2:-2].strip()
    assert inner.endswith("pprint"), text
    return inner[: -len("pprint")].rstrip().rstrip("|")


def _context(tree, known, value_of):
    """The vars file plus an event in which each field the string reads holds
    value_of(its position in the string, its path)."""
    context = dict(known)
    fields = list(dict.fromkeys(_event_paths(tree)))
    for path in sorted(fields, key=lambda p: -len(p)):
        value = value_of(fields.index(path), path)
        node = context.setdefault(path[0], {})
        if len(path) == 1:
            node.setdefault("field", value)
            continue
        for part in path[1:-1]:
            node = node.setdefault(part, {})
        node.setdefault(path[-1], value)
    return context


@pytest.mark.parametrize("path", ALL_RULEBOOKS, ids=str)
def test_action_strings_keep_text_between_two_expressions(path):
    """A rule composes its text from event fields with a literal between any
    two of them, never `}}{{`: each field then stands alone in the text a
    playbook receives."""
    for rule, text, tree in _action_templates(path):
        for output in tree.find_all(nodes.Output):
            literal = [isinstance(n, nodes.TemplateData) for n in output.nodes]
            assert all(
                a or b for a, b in zip(literal, literal[1:])
            ), f"{path}: {rule}: {text[:80]}"


@pytest.mark.parametrize("path", ALL_RULEBOOKS, ids=str)
def test_a_string_that_opens_with_an_event_field_is_one_expression(path):
    """An action string that opens with an expression is that one expression
    ending in `| pprint` (in parentheses when it joins several parts), so the
    playbook receives the value it was: text stays text, a number or a list
    keeps its type. A string that carries a field later on opens with a plain
    word."""
    for rule, text, tree in _event_strings(path):
        where = f"{path}: {rule}: {text[:80]}"
        if _opens_with_text(tree):
            word = OPENING_WORD.match(text)
            assert word, where
            assert word.group(1) not in ("True", "False", "None", "set"), where
        else:
            root = _one_expression(tree)
            assert isinstance(root, nodes.Filter) and root.name == "pprint", where


@pytest.mark.parametrize("path", ALL_RULEBOOKS, ids=str)
def test_an_event_field_reaches_the_action_as_the_value_it_was(path, repo_root):
    """Rendered the way ansible-rulebook renders an action, a one-expression
    string gives exactly what its expression evaluates to: text that looks
    like a number, a list or a boolean stays that text, and a number, a list
    or a boolean keeps its type."""
    util = pytest.importorskip("ansible_rulebook.util")
    known = yaml.safe_load((repo_root / "vars" / "example.yml").read_text())
    for rule, text, tree in _event_strings(path):
        if _opens_with_text(tree):
            continue
        where = f"{path}: {rule}: {text[:80]}"
        expression = JINJA.compile_expression(_operand(text), undefined_to_none=False)
        texts = typed = refused = 0
        for value in TEXT_VALUES + TYPED_VALUES:
            context = _context(tree, known, lambda _i, _p: value)
            try:
                expected = expression(**context)
            except TypeError:  # the expression compares or calculates
                refused += isinstance(value, str)
                continue
            except Exception:  # the expression has no meaning for this value
                continue
            if isinstance(expected, jinja2.Undefined):
                continue
            got = util.substitute_variables(text, context)
            assert got == expected and type(got) is type(expected), (where, value)
            if isinstance(value, str):
                texts += 1
            else:
                typed += 1
        # an expression that compares a field with a number or calculates with
        # it takes no text: every text value then raised TypeError
        number_only = 0 < len(TEXT_VALUES) == refused
        counts = f"{texts} text and {typed} typed values compared"
        assert typed and (texts or number_only), (where, counts)


def test_no_join_in_an_action_completes_a_marker(repo_root, plain_text_filter):
    """Whatever plain text the fields of an event hold, every action string
    renders, and the text it builds from them and its own literals holds no
    template marker and no vault header. A field the string compares with a
    number holds a number when the text does not render. A render takes one
    path through a string, so each of its literals is also joined on its own
    to the same text, before it and after it: a branch or a fallback that no
    render takes completes nothing either."""
    util = pytest.importorskip("ansible_rulebook.util")
    plain = plain_text_filter.plain_text
    known = yaml.safe_load((repo_root / "vars" / "example.yml").read_text())
    strings = _event_strings_once()
    assert strings
    for text, (tree, where) in strings.items():
        for literal in _literals(tree):
            for value in TAILS + HEADS:
                for out in (literal + plain(value), plain(value) + literal):
                    case = (where, text[:80], literal, value)
                    assert VAULT_HEADER not in out, case
                    for marker in MARKERS:
                        assert marker not in out.lower(), case
        numbers = _numbers(tree)
        for tail in TAILS:
            for head in HEADS:
                for first, second in ((tail, head), (head, tail)):

                    def text_of(i, _p):
                        return plain(second if i % 2 else first)

                    def text_or_number(i, p):
                        return 7 if p in numbers else text_of(i, p)

                    case = (where, text[:80], first, second)
                    try:
                        try:
                            got = util.substitute_variables(
                                text, _context(tree, known, text_of)
                            )
                        except TypeError:  # the string compares a field with a number
                            got = util.substitute_variables(
                                text, _context(tree, known, text_or_number)
                            )
                    except Exception as error:  # name the string that did not render
                        raise AssertionError(case) from error
                    for out in _strings(got):
                        assert VAULT_HEADER not in out, case
                        for marker in MARKERS:
                            assert marker not in out.lower(), case
