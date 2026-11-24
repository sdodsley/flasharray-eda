# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
plain_text.py - ansible-rulebook event filter that carries event text as
plain text.

Every string in the event, keys and values alike and however deep, has its
template markers and the vault header broken by a space: `{{` becomes `{ {`,
`{%` becomes `{ %`, `{#` becomes `{ #`, `#jinja2` becomes `# jinja2` and
`$ANSIBLE_VAULT` becomes `$ ANSIBLE_VAULT`. A `{`, a `#` or a `$` that ends
a string, or a `#` or a `$` followed there by the start of `jinja2` or of
`ANSIBLE_VAULT`, is followed by a space too, so text joined on after it
completes neither. The text stays readable, and the rules and the playbooks
handle it as data. Numbers, booleans and null pass through untouched.

The project's own event sources (purefa_events, purefa_syslog) already emit
plain text. This filter is for the stock sources whose text comes from
outside the array: the listener rulebooks put it on ansible.eda.alertmanager
(alert labels and annotations) and ansible.eda.webhook (the request body),
and a rulebook of yours can add it to any other source the same way.

Arguments: none.
"""

import re

DOCUMENTATION = r"""
---
name: plain_text
short_description: Carry event text as plain text.
description:
  - An event filter that breaks the template markers and the vault header
    in every string of the event ("{{", "{%", "{#", "#jinja2" and
    "$ANSIBLE_VAULT", and a "{", "#" or "$" that ends a string, or a "#"
    followed there by the start of "jinja2", or a "$" followed there by
    the start of "ANSIBLE_VAULT") with a space, keys and values alike, so
    the rules and the playbooks handle the text as data. Numbers, booleans
    and null pass through untouched.
options: {}
"""

EXAMPLES = r"""
- ansible.eda.alertmanager:
    host: 0.0.0.0
    port: 5002
  filters:
    - sdodsley.flasharray_eda.plain_text: {}
"""


_MARKER = re.compile(r"\{(?=[{%#]|\Z)")
_OVERRIDE = re.compile(r"#(?=jinja2|(?:j(?:i(?:n(?:ja?)?)?)?)?\Z)", re.IGNORECASE)
_VAULT = re.compile(
    r"\$(?=ANSIBLE_VAULT|(?:A(?:N(?:S(?:I(?:B(?:L(?:E(?:_(?:V(?:A(?:U(?:L)?)?)?)?)?)?)?)?)?)?)?)?\Z)"
)


def plain_text(value):
    """Return value with the template markers and the vault header in every
    string broken by a space ("{{" -> "{ {", "{%" -> "{ %", "{#" -> "{ #",
    "#jinja2" -> "# jinja2", "$ANSIBLE_VAULT" -> "$ ANSIBLE_VAULT"),
    recursively through dicts (text keys too), lists and tuples; anything
    else is returned as is. A "{", a "#" or a "$" that ends a string, or a
    "#" or a "$" followed there by the start of "jinja2" or of
    "ANSIBLE_VAULT", gets the space too, so text joined on after it
    completes neither. The text stays readable. Kept identical in the
    plain_text event filter, purefa_events.py and purefa_syslog.py
    (tests/unit/test_plain_text.py checks); ansible-rulebook loads each
    plugin from its own file, so none of them can import it from another."""
    if isinstance(value, str):
        return _VAULT.sub("$ ", _OVERRIDE.sub("# ", _MARKER.sub("{ ", value)))
    if isinstance(value, dict):
        return {
            (plain_text(k) if isinstance(k, str) else k): plain_text(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [plain_text(v) for v in value]
    return value


def main(event: dict) -> dict:
    """The event with every string carried as plain text."""
    return plain_text(event)
