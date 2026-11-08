# Packaging

The repository is the Ansible collection `sdodsley.flasharray_eda`. This
page describes how the collection is laid out, built and installed, what
the decision environment contains, and what changes when the content moves
into `everpure.flasharray`.

## The repository is the collection

`galaxy.yml` sits at the repository root, so the root directory is the
collection source. The collection content is what ansible-rulebook and the
EDA controller need to resolve the event sources and run the playbooks:

| Path | Content |
| --- | --- |
| `extensions/eda/plugins/event_source/` | `purefa_events.py` and `purefa_syslog.py` |
| `extensions/eda/rulebooks/` | the rulebooks, which name their sources by FQCN |
| `playbooks/` | the playbooks and `runbooks/` |
| `meta/runtime.yml` | `requires_ansible: ">=2.15.0"` |
| `README.md`, `LICENSE` | the collection readme and licence |

Everything else in the tree (tests, docs, the platform seed under `aap/`,
the orchestrator definitions, the Prometheus rules, the baseline schema,
the local launcher and the environment files, `tools/`, `slack/`, `venv/`,
`collections/` and `build/`) is development material and is listed in
`build_ignore`, so the built artifact carries the collection content only.

The event sources are `sdodsley.flasharray_eda.purefa_events` and
`sdodsley.flasharray_eda.purefa_syslog`, and the nine standalone rulebooks
declare them under those names. ansible-rulebook resolves a FQCN source
from `ANSIBLE_COLLECTIONS_PATH`, which is why the collection has to be
installed before a rulebook starts; `run.sh` still passes `--source-dir`,
which ansible-rulebook consults first for bare names and which is harmless
for a FQCN. The dependencies are declared in `galaxy.yml`: `ansible.eda`
>= 2.13.0 (the stock webhook, tick, generic and alertmanager sources) and
`everpure.flasharray` >= 1.46.0 (the modules the playbooks run).
`requirements.yml` lists the same two for `ansible-galaxy collection
install -r`, and `aap/requirements.yml` adds the Automation Hub collections
the seed needs.

## Build and install

    make collection

builds the tarball into `build/` with `ansible-galaxy collection build` and
installs it, with its dependencies when they are not already installed,
under `./collections` with `ansible-galaxy collection install --force -p
collections`. `env.sh` puts `./collections` first on
`ANSIBLE_COLLECTIONS_PATH` when that directory exists, so `run.sh` and the
end-to-end scripts resolve the FQCN sources from it. `make deps` installs
the Python requirements and the two collections and then runs `make
collection`, so a fresh checkout is ready after `make deps`. CI runs `make
collection` after the lint (so that the installed collections are not
linted) and before the unit tests. `build/` and `collections/` are
gitignored and in `build_ignore`, so building from a tree that already
holds an installed copy does not package it twice.

## The decision environment

`decision-environment/Containerfile` and
`decision-environment/execution-environment.yml` (ansible-builder v3) build
the same image on `quay.io/ansible/ansible-rulebook:v1.3.2`;
`aap/decision-environment.yml` builds the same content on the supported
`de-supported-rhel9` base for Ansible Automation Platform. Each image
contains:

- py-pure-client (`decision-environment/requirements.txt` or the inline
  Python list);
- the collection `sdodsley.flasharray_eda`, built from the checkout at
  image build time and installed under `/usr/share/ansible/collections`
  together with `everpure.flasharray` and, where the base image does not
  already carry it, `ansible.eda`. The Containerfile copies the collection
  content in, runs `ansible-galaxy collection build` and installs the
  tarball; the ansible-builder definitions copy the content into the build
  context with `additional_build_files` and install it through a galaxy
  requirement of type `dir`, which ansible-galaxy builds on the fly in the
  galaxy stage (the form ansible-builder v3 supports for content that is
  not on a Galaxy server; a `dir` requirement honours `build_ignore`);
- the rulebooks and playbooks as plain files under `/opt/flasharray-eda`,
  the working directory, so that a standalone `podman run` names a rulebook
  by path and `run_playbook: playbooks/<name>.yml` resolves;
- `/var/log/flasharray-eda` and `/var/lib/flasharray-eda/requests`, the
  default `alert_log` and `request_dir`, writable by the runtime user.

The upstream base image ships `ansible.eda` under `/app/.ansible/collections`
and `ansible-galaxy collection install` leaves a dependency that is already
installed there alone, so `ANSIBLE_COLLECTIONS_PATH` in those two images is
`/usr/share/ansible/collections:/app/.ansible/collections`; with the first
directory alone ansible-rulebook cannot find `ansible.eda.alertmanager` and
the other stock sources. The supported base already keeps `ansible.eda`
under `/usr/share/ansible/collections`, so `aap/decision-environment.yml`
needs no override.

Two things about ansible-builder 3 that the definitions are written around:
`additional_build_files` copies a directory's *contents* into
`_build/<dest>`, so each collection directory names its own `dest`
(`flasharray_eda/extensions`, `flasharray_eda/playbooks`,
`flasharray_eda/meta`) or the three merge into one; and the base image's
`/usr/bin/python3` is 3.9 while ansible-rulebook and the `PYTHONPATH` it
sets are 3.12, so `python_interpreter` pins `/usr/bin/python3.12` for the
build stages. On a rootless host with native overlay, buildah can fail
every multi-stage `COPY --from` with "copier: stat ... no such file or
directory" although the stage image holds the path; a separate image
store with the `vfs` driver (`podman --root <dir> --storage-driver vfs`,
put first on the PATH as `podman` for the build) builds the image.

Because the sources resolve from the installed collection, the image serves
two uses with one tree: a standalone `podman run ... ansible-rulebook
--rulebook extensions/eda/rulebooks/<name>.yml` without `--source-dir` (the
Containerfile header has the full line), and EDA controller activations,
which pass no `--source-dir` and resolve custom sources from the
collections in the decision environment only.

## Moving the content into everpure.flasharray

The content is already in collection layout, so the move is a rename plus
the collection's own review steps:

1. **Source names.** `sdodsley.flasharray_eda.purefa_events` and
   `sdodsley.flasharray_eda.purefa_syslog` become
   `everpure.flasharray.purefa_events` and
   `everpure.flasharray.purefa_syslog` in the nine standalone rulebooks,
   in the platform rulebooks generated from them, in the plugins'
   `EXAMPLES` blocks and in `aap/seed.yml`'s activations. The
   `ansible.eda` dependency moves into the collection's `galaxy.yml`.
2. **Playbook references.** `run_playbook: name: playbooks/notify.yml`
   becomes the collection playbook FQCN (`everpure.flasharray.notify`),
   one per playbook. Re-check `alert_runbook.yml`'s relative include of
   `runbooks/<code>.yml` from a collection playbook, where `playbook_dir`
   points into the installed collection, and the shared
   `playbooks/tasks/resolve_array.yml` include for the same reason.
3. **Variables and secrets.** The `--vars` file (`vars/example.yml`) is the
   activation's extra vars. `APPROVAL_TOKEN` and `GIT_WEBHOOK_SECRET` are
   credentials injected as environment variables in a standalone run
   (`--env-vars`); on the platform the two webhook rulesets are event
   streams, which authenticate at the platform, and the platform rulebooks
   omit them.
4. **Documentation.** Both event sources carry `DOCUMENTATION` and
   `EXAMPLES` blocks (a unit test asserts every argument the code reads is
   an option). On the move, re-check them with the collection's doc
   tooling, add a README section for the EDA content, and a changelog
   fragment (quote entries that contain `key: value` text or the changelog
   sanity test fails).
5. **Tests.** `tests/unit/test_events_plugin.py` and
   `tests/unit/test_syslog_parse.py` with the `conftest.py` SDK stub move
   to `tests/unit/plugins/event_source/`; `ansible-test sanity` and
   `ansible-lint` run as the collection's CI does. The replay rulebooks
   and `tests/unit/test_rulebooks.py` stay in this repository.
6. **Decision environment.** The Containerfile and the builder definitions
   install the released collection from `requirements.yml` instead of
   building it from the checkout.
7. **Certification.** Red Hat's review of certified EDA content looks for
   asyncio sources with `main(queue, args)` that never block the event loop
   (the pollers run SDK calls in a thread pool), clean shutdown on
   cancellation, documented arguments, rulebook examples under
   `extensions/eda/rulebooks`, unit tests, and a published decision
   environment definition. Confirm the list against the partner guide
   before submitting. Automation Hub judges the built artifact, so check
   that `build_ignore` keeps `extensions/`.
