# Developer entry points. `make check` is what CI runs.
PY ?= python3
PLUGINS = extensions/eda/plugins/event_source
# playbooks only: dr_drill_one.yml is a task file, tests/replay_*.yml are rulebooks
PLAYBOOKS = $(filter-out playbooks/dr_drill_one.yml,$(wildcard playbooks/*.yml)) $(filter-out $(wildcard tests/replay_*.yml),$(wildcard tests/*.yml))

.PHONY: check lint test syntax deps de

deps:
	$(PY) -m pip install -r requirements.txt
	ansible-galaxy collection install -r requirements.yml

lint:
	black --check $(PLUGINS) tests/unit
	pyflakes $(PLUGINS) tests/unit
	yamllint -s .

test:
	$(PY) -m pytest -q tests/unit

syntax:
	# the remediation playbooks take their host from the rulebook (hosts: "{{ target_array }}")
	ANSIBLE_LOCALHOST_WARNING=False ansible-playbook -i localhost, -e target_array=localhost --syntax-check $(PLAYBOOKS)

check: lint test syntax

# container image for ansible-rulebook --source-dir runs; not built in CI
de:
	podman build -t flasharray-eda-de -f decision-environment/Containerfile .
