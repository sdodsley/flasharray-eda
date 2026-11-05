# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Static checks over the rulebooks: they validate against ansible-rulebook's
own schema, every variable they reference is in vars/example.yml, every
playbook they run exists, and the replay copies in tests/ have not drifted
from the real rules."""

import json
import pathlib
import re

import pytest
import yaml

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

RULEBOOKS = sorted(pathlib.Path("extensions/eda/rulebooks").glob("*.yml"))
REPLAYS = sorted(pathlib.Path("tests").glob("replay_*.yml"))
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
    for name, rule in _rules_by_name(replay).items():
        assert name in real_rules, f"{replay.name}: rule '{name}' is not in {real.name}"
        assert (
            rule == real_rules[name]
        ), f"{replay.name}: rule '{name}' has drifted from {real.name}"
