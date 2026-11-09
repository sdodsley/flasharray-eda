# Developer entry points. `make check` is what CI runs.
PY ?= python3
PLUGINS = extensions/eda/plugins/event_source
# playbooks only: dr_drill_one.yml is a task file, tests/replay_*.yml are rulebooks
PLAYBOOKS = $(filter-out playbooks/dr_drill_one.yml,$(wildcard playbooks/*.yml)) $(filter-out $(wildcard tests/replay_*.yml),$(wildcard tests/*.yml))
# the collection the repository root builds into (galaxy.yml)
COLLECTION = sdodsley-flasharray_eda

.PHONY: check lint lint-ao test syntax deps collection de

# Python requirements, the two collections the rulebooks and playbooks use,
# then this repository as the collection sdodsley.flasharray_eda, so that a
# fresh checkout resolves the rulebooks' FQCN sources
deps:
	$(PY) -m pip install -r requirements.txt
	ansible-galaxy collection install -r requirements.yml
	$(MAKE) collection

# build the collection into build/ and install the tarball (with its
# dependencies, when they are not already installed) under ./collections,
# which env.sh puts first on ANSIBLE_COLLECTIONS_PATH
collection:
	rm -f build/$(COLLECTION)-*.tar.gz
	mkdir -p build collections
	ansible-galaxy collection build --output-path build .
	ansible-galaxy collection install --force -p collections build/$(COLLECTION)-*.tar.gz

lint:
	black --check $(PLUGINS) slack tests/*.py tests/unit
	pyflakes $(PLUGINS) slack tests/*.py tests/unit
	yamllint -s .

# the automation orchestrator workflow definitions (orchestrator/README.md)
# and the linter itself; standard library only, so it needs no deps
lint-ao:
	black --check tools
	pyflakes tools
	$(PY) tools/ao_lint.py orchestrator/*.json --allowed-templates orchestrator/job_templates.txt

test:
	$(PY) -m pytest -q tests/unit

syntax:
	# the remediation playbooks take their host from the rulebook (hosts: "{{ target_array }}");
	# the DR switchover names both sides of a replica link (source_array and target_array)
	ANSIBLE_LOCALHOST_WARNING=False ansible-playbook -i localhost, -e target_array=localhost -e source_array=localhost --syntax-check $(PLAYBOOKS)

check: lint lint-ao test syntax

# container image for standalone ansible-rulebook runs and EDA controller
# activations; builds and installs the collection; not built in CI
de:
	podman build -t flasharray-eda-de -f decision-environment/Containerfile .
