# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The plain_text event filter and the identical helper inside the two event
sources: every string in an event is carried as plain text, and everything a
source puts on the rulebook queue has been through the helper."""

import ast
import inspect

import pytest
import yaml

MARKERS = ("{{", "{%", "{#", "#jinja2", "$ansible_vault")

CASES = [
    ("{{ 7 * 7 }}", "{ { 7 * 7 }}"),
    ("{{{ x }}}", "{ { { x }}}"),
    ("{{{{ 7 * 7 }}", "{ { { { 7 * 7 }}"),
    ("{{% if true %}x{% endif %}", "{ { % if true %}x{ % endif %}"),
    ("{#{{ x }}#}", "{ #{ { x }}#}"),
    ("{% if a %}b{% endif %}", "{ % if a %}b{ % endif %}"),
    ("a {# c #} b", "a { # c #} b"),
    ("#jinja2:trim_blocks:False\n{{ x }}", "# jinja2:trim_blocks:False\n{ { x }}"),
    ("#JINJA2:x\n", "# JINJA2:x\n"),
    ("#jinja2", "# jinja2"),
    ("x #Jinja2 y", "x # Jinja2 y"),
    # the vault header
    ("$ANSIBLE_VAULT;1.1;AES256", "$ ANSIBLE_VAULT;1.1;AES256"),
    ("a $ANSIBLE_VAULT b", "a $ ANSIBLE_VAULT b"),
    # a string that ends where a marker could start gets the space too
    ("{", "{ "),
    ("x{", "x{ "),
    ("#", "# "),
    ("#j", "# j"),
    ("#ji", "# ji"),
    ("x #jin", "x # jin"),
    ("x #jinj", "x # jinj"),
    ("#JINJA", "# JINJA"),
    ("{#", "{ # "),
    ("x$", "x$ "),
    ("x $A", "x $ A"),
    ("x $ANSIBLE_VA", "x $ ANSIBLE_VA"),
    # anywhere else a lone brace, hash or dollar sign is kept
    ("#jin x", "#jin x"),
    ("a # b { c", "a # b { c"),
    ("{\n", "{\n"),
    ("$HOME and $ANSIBLE_CONFIG cost $5", "$HOME and $ANSIBLE_CONFIG cost $5"),
    ("$ansible_vault", "$ansible_vault"),
    # JSON in an audit's arguments keeps its braces
    ('{"a": {"b": 1}}', '{"a": {"b": 1}}'),
    ("{ {already}", "{ {already}"),
    ("plain text, no braces", "plain text, no braces"),
    ("", ""),
]

# text that ends where a marker could start, and text that could complete one
TAILS = (
    ("{", "x{", "#JIN", "a #Jinja2", "{#", "{{", "x$ANSIBLE_", "a $ANSIBLE_VAULT b")
    + tuple("#jinja2"[:n] for n in range(1, 8))
    + tuple("$ANSIBLE_VAULT"[:n] for n in range(1, 15))
)
HEADS = (
    ("{ x }}", "% if x %}", "# c #}", "{", "#", "$")
    + tuple("jinja2:x"[n:] for n in range(7))
    + tuple("ANSIBLE_VAULT;1.1"[n:] for n in range(14))
)
LITERALS = ("", ":", "2:", "jinja2:", "VAULT", "ANSIBLE_VAULT", " ", ": ")


@pytest.mark.parametrize("text, expected", CASES)
def test_markers_are_broken_and_other_text_is_kept(plain_text_filter, text, expected):
    assert plain_text_filter.plain_text(text) == expected


@pytest.mark.parametrize("text, expected", CASES)
def test_no_marker_survives(plain_text_filter, text, expected):
    # the vault header is matched as written, the others in any case
    assert "$ANSIBLE_VAULT" not in expected
    for marker in MARKERS[:-1]:
        assert marker not in expected.lower()
    assert not expected.endswith(("{", "#", "$"))


@pytest.mark.parametrize("tail", TAILS)
def test_no_marker_forms_where_text_is_joined_to_a_field(plain_text_filter, tail):
    """Whatever follows a field, another field or a literal and then a field,
    the join completes no marker."""
    plain = plain_text_filter.plain_text
    for head in HEADS:
        for literal in LITERALS:
            joined = (plain(tail) + literal + plain(head)).lower()
            for marker in MARKERS:
                assert marker not in joined, (tail, literal, head)


def test_the_filter_is_idempotent(plain_text_filter):
    for text, _ in CASES:
        once = plain_text_filter.plain_text(text)
        assert plain_text_filter.plain_text(once) == once


def test_nested_structures_keys_and_other_types(plain_text_filter):
    event = {
        "alert": {
            "labels": {"alertname": "X", "{{ k }}": "{{ v }}"},
            "annotations": {"summary": "{% raw %}{{ 7 * 7 }}{% endraw %}"},
            "count": 3,
            "firing": True,
            "nothing": None,
            "ratio": 0.5,
            "list": ["{{ a }}", 1, ("{{ t }}",)],
        }
    }
    out = plain_text_filter.plain_text(event)
    assert out["alert"]["labels"] == {"alertname": "X", "{ { k }}": "{ { v }}"}
    assert (
        out["alert"]["annotations"]["summary"] == "{ % raw %}{ { 7 * 7 }}{ % endraw %}"
    )
    assert out["alert"]["count"] == 3 and out["alert"]["firing"] is True
    assert out["alert"]["nothing"] is None and out["alert"]["ratio"] == 0.5
    assert out["alert"]["list"] == ["{ { a }}", 1, ["{ { t }}"]]
    assert event["alert"]["labels"]["{{ k }}"] == "{{ v }}", "the input is kept"


def test_a_key_that_is_not_text_is_kept(plain_text_filter):
    event = {("a", 1): "{{ v }}", 3: ["{{ w }}"], None: "x"}
    assert plain_text_filter.plain_text(event) == {
        ("a", 1): "{ { v }}",
        3: ["{ { w }}"],
        None: "x",
    }


def test_main_returns_the_event_as_plain_text(plain_text_filter):
    event = {"payload": {"approver": "{{ 7 * 7 }}"}, "meta": {"received_at": "t"}}
    assert plain_text_filter.main(event) == {
        "payload": {"approver": "{ { 7 * 7 }}"},
        "meta": {"received_at": "t"},
    }


def test_the_sources_carry_the_same_helper(plain_text_filter, events, syslog):
    """ansible-rulebook loads each plugin from its own file, so the two sources
    carry a copy of the helper; this keeps the three identical."""
    reference = inspect.getsource(plain_text_filter.plain_text)
    assert inspect.getsource(events.plain_text) == reference
    assert inspect.getsource(syslog.plain_text) == reference
    for module in (events, syslog):
        for name in ("_MARKER", "_OVERRIDE", "_VAULT"):
            theirs, ours = getattr(module, name), getattr(plain_text_filter, name)
            assert theirs.pattern == ours.pattern, name
            assert theirs.flags == ours.flags, name


def _queue_puts(path):
    """Every put on the rulebook queue (the name `queue`) in a source."""
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("put", "put_nowait")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "queue"
        ):
            yield node


@pytest.mark.parametrize("name", ["purefa_events.py", "purefa_syslog.py"])
def test_everything_a_source_queues_goes_through_the_helper(repo_root, name):
    """An event reaches ansible-rulebook only as plain_text(...) or, in the
    syslog source, as _forwarded(...), which returns plain_text(...)."""
    source = repo_root / "extensions" / "eda" / "plugins" / "event_source" / name
    puts = list(_queue_puts(source))
    assert puts, f"{name}: no put on the rulebook queue"
    for call in puts:
        argument = call.args[0] if len(call.args) == 1 and not call.keywords else None
        assert (
            isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Name)
            and argument.func.id in ("plain_text", "_forwarded")
        ), f"{name}:{call.lineno}"


def test_documentation_names_the_filter_and_the_example_uses_it(plain_text_filter):
    doc = yaml.safe_load(plain_text_filter.DOCUMENTATION)
    assert doc["name"] == "plain_text"
    assert doc["options"] == {}
    example = yaml.safe_load(plain_text_filter.EXAMPLES)
    assert example[0]["filters"] == [{"sdodsley.flasharray_eda.plain_text": {}}]
