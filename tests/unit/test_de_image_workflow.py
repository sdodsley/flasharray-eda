# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Static checks over .github/workflows/de-image.yml, the workflow that
builds the decision environment and publishes it to ghcr.io: the triggers,
the token permissions, the image name from the lowercased owner, the build
from the repository root with the Containerfile, the smoke run, and that a
pull request never logs in or pushes while the other events tag the commit
sha, `latest` from main and the tag name from a v* tag."""

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "de-image.yml"
CONTAINERFILE = ROOT / "decision-environment" / "Containerfile"
IMAGE_NAME = "flasharray-eda-de"
# the image is built from these; a change elsewhere leaves it as it is
BUILD_INPUTS = {
    "decision-environment/**",
    "extensions/**",
    "playbooks/**",
    "galaxy.yml",
    "meta/**",
    "README.md",
    "LICENSE",
    ".github/workflows/de-image.yml",
}


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow):
    # PyYAML reads the key `on` as the boolean True
    return workflow.get("on", workflow.get(True))


def _steps():
    (job,) = _workflow()["jobs"].values()
    return job["steps"]


def _step(name):
    (step,) = [s for s in _steps() if s.get("name") == name]
    return step


def test_file_starts_with_the_document_marker():
    assert WORKFLOW.read_text(encoding="utf-8").startswith("---\n")


def test_name_and_triggers():
    workflow = _workflow()
    assert workflow["name"] == "Decision environment image"
    on = _triggers(workflow)
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}
    assert on["push"]["branches"] == ["main"]
    assert on["push"]["tags"] == ["v*"]
    assert set(on["push"]["paths"]) == BUILD_INPUTS


def test_token_permissions_are_read_contents_write_packages():
    assert _workflow()["permissions"] == {"contents": "read", "packages": "write"}


def test_one_job_on_ubuntu_with_the_checkout_action_only():
    (job,) = _workflow()["jobs"].values()
    assert job["runs-on"] == "ubuntu-latest"
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses == ["actions/checkout@v4"]


def test_image_name_is_the_lowercased_owner_and_the_commit_sha():
    (job,) = _workflow()["jobs"].values()
    assert "env" not in job, "the name is composed in a step, lowercased"
    run = _step("Name the image")["run"]
    assert 'owner="${GITHUB_REPOSITORY_OWNER,,}"' in run
    assert f'REPO=ghcr.io/$owner/{IMAGE_NAME}" >> "$GITHUB_ENV"' in run
    assert f'IMAGE=ghcr.io/$owner/{IMAGE_NAME}:$GITHUB_SHA" >> "$GITHUB_ENV"' in run


def test_build_uses_the_containerfile_from_the_repository_root():
    run = _step("Build the image")["run"]
    assert CONTAINERFILE.is_file()
    assert re.search(r"podman build .*-f decision-environment/Containerfile", run)
    assert run.rstrip().endswith(" .")
    assert '-t "$IMAGE"' in run


def test_smoke_run_checks_rulebook_and_the_three_collections():
    run = _step("Smoke run")["run"]
    assert 'podman run --rm "$IMAGE" ansible-rulebook --version' in run
    assert 'podman run --rm "$IMAGE" ansible-galaxy collection list' in run
    for name in ("sdodsley.flasharray_eda", "everpure.flasharray", "ansible.eda"):
        assert name in run
    assert "if" not in _step("Smoke run"), "the smoke run gates nothing"


def test_login_and_push_skip_pull_requests_only():
    for name in ("Log in to the registry", "Push"):
        assert _step(name)["if"] == "github.event_name != 'pull_request'"
    for step in _steps():
        if step.get("name") not in ("Log in to the registry", "Push"):
            assert "if" not in step


def test_login_uses_the_run_token_from_the_environment():
    step = _step("Log in to the registry")
    assert step["env"] == {"TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    assert "podman login ghcr.io" in step["run"]
    assert "--password-stdin" in step["run"]
    assert "secrets." not in step["run"], "the token reaches the shell as env"


def test_push_tags_sha_always_latest_from_main_and_the_tag_name():
    run = _step("Push")["run"]
    assert 'podman push "$IMAGE"' in run
    assert "refs/heads/main" in run and "extra=latest" in run
    assert 'GITHUB_REF_TYPE" = tag' in run
    assert 'extra="$GITHUB_REF_NAME"' in run
    assert 'podman tag "$IMAGE" "$REPO:$extra"' in run
    assert 'podman push "$REPO:$extra"' in run


def test_no_lab_names_or_addresses():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert "/home/" not in text and "C:\\" not in text
