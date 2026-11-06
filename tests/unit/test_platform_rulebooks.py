# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The committed platform rulebooks under extensions/eda/rulebooks/platform/
are exactly what tools/platform_rulebooks.py generates from the standalone
rulebooks beside them, every job template they name is listed in
orchestrator/job_templates.txt, and the generated form keeps the contract
the generator documents (FQCN sources, no webhook-only ruleset, no
run_playbook, no template-carried extra var)."""

import importlib.util
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "tools" / "platform_rulebooks.py"
RULEBOOKS = ROOT / "extensions" / "eda" / "rulebooks"
PLATFORM = RULEBOOKS / "platform"
TEMPLATES = ROOT / "orchestrator" / "job_templates.txt"
STANDALONE = sorted(p.name for p in RULEBOOKS.glob("*.yml"))


def _load_generator():
    spec = importlib.util.spec_from_file_location("platform_rulebooks", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return _load_generator()


@pytest.fixture(scope="module")
def generated(generator):
    return generator.generate(RULEBOOKS)


def _allowed_templates():
    names = set()
    for line in TEMPLATES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(line)
    return names


def _actions(doc):
    for ruleset in doc:
        for rule in ruleset.get("rules", []):
            yield from rule.get("actions") or [rule.get("action", {})]


def test_every_standalone_rulebook_has_one_platform_file(generated):
    assert sorted(generated) == STANDALONE
    assert sorted(p.name for p in PLATFORM.glob("*.yml")) == STANDALONE


@pytest.mark.parametrize("name", STANDALONE)
def test_committed_platform_file_equals_the_generators_output(name, generated):
    committed = (PLATFORM / name).read_text(encoding="utf-8")
    assert committed == generated[name], (
        f"platform/{name} differs from the generator's output; "
        "run python tools/platform_rulebooks.py"
    )


@pytest.mark.parametrize("name", STANDALONE)
def test_platform_file_keeps_the_generators_contract(name, generator):
    doc = yaml.safe_load((PLATFORM / name).read_text(encoding="utf-8"))
    standalone = yaml.safe_load((RULEBOOKS / name).read_text(encoding="utf-8"))
    kept = [rs for rs in standalone if not generator.webhook_only(rs)]
    assert [rs["name"] for rs in doc] == [rs["name"] for rs in kept]
    for ruleset in doc:
        for entry in ruleset["sources"]:
            source = generator.source_type(entry)
            assert source != generator.WEBHOOK_SOURCE, f"{name}: webhook source kept"
            assert source not in generator.SOURCE_FQCN, f"{name}: bare {source}"
    for action in _actions(doc):
        assert "run_playbook" not in action, f"{name}: run_playbook left"
        job = action.get("run_job_template")
        if job is None:
            continue
        assert job["organization"] == generator.ORGANIZATION
        extra_vars = job.get("job_args", {}).get("extra_vars", {})
        carried = set(extra_vars) & set(generator.TEMPLATE_VARS)
        assert not carried, f"{name}: {job['name']} passes {sorted(carried)}"


def test_every_run_job_template_name_is_an_allowed_template():
    allowed = _allowed_templates()
    used = set()
    for path in sorted(PLATFORM.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for action in _actions(doc):
            job = action.get("run_job_template")
            if job:
                used.add(job["name"])
    assert used, "no run_job_template action in the platform rulebooks"
    assert used <= allowed, f"not in job_templates.txt: {sorted(used - allowed)}"


def test_every_mapped_playbook_exists_and_every_run_playbook_is_mapped(generator):
    for playbook in generator.PLAYBOOK_TEMPLATES:
        assert (ROOT / playbook).exists(), f"{playbook} is mapped but missing"
    for name in STANDALONE:
        doc = yaml.safe_load((RULEBOOKS / name).read_text(encoding="utf-8"))
        for action in _actions(doc):
            run = action.get("run_playbook")
            if run:
                assert run["name"] in generator.PLAYBOOK_TEMPLATES, run["name"]


def test_rules_survive_the_conversion_unchanged_but_for_the_action(generator):
    """Everything on a rule except its action (name, condition, throttle) is
    the standalone rule's, and the extra_vars that are passed are the same."""
    for name in STANDALONE:
        standalone = yaml.safe_load((RULEBOOKS / name).read_text(encoding="utf-8"))
        platform = yaml.safe_load((PLATFORM / name).read_text(encoding="utf-8"))
        kept = [rs for rs in standalone if not generator.webhook_only(rs)]
        for before, after in zip(kept, platform):
            assert len(before["rules"]) == len(after["rules"])
            for rule_before, rule_after in zip(before["rules"], after["rules"]):
                for key, value in rule_before.items():
                    if key not in ("action", "actions"):
                        assert (
                            rule_after[key] == value
                        ), f"{name}: {rule_before['name']}"
                run = rule_before.get("action", {}).get("run_playbook")
                if run:
                    job = rule_after["action"]["run_job_template"]
                    expected = {
                        k: v
                        for k, v in (run.get("extra_vars") or {}).items()
                        if k not in generator.TEMPLATE_VARS
                    }
                    assert job.get("job_args", {}).get("extra_vars", {}) == expected
                    assert job["name"] == generator.PLAYBOOK_TEMPLATES[run["name"]]
