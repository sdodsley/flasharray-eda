# Baseline

Desired state per array, one YAML file named after the inventory host
(`<array>.yml`). This directory only shows the schema. The "git baseline"
itself is a separate repository, checked out on the EDA host at the path
`baseline_dir` names in the vars file (`/opt/flasharray-eda-baseline` in
`vars/example.yml`), pulled by `playbooks/apply_baseline.yml` when the
change control rulebook receives a signed push webhook, and read by
`playbooks/reconcile.yml` when an audited out-of-band change needs
reverting.

`example.yml` shows the schema. Real `<array>.yml` files live in the
baseline repository that `baseline_dir` points at. Any `<array>.yml` placed
in this directory (for example for a quick hand run with
`-e baseline_dir=$PWD/baseline`) is gitignored, so site data is never
committed here. With this directory as `baseline_dir` there is no
`baseline/.git`, so the apply playbook skips the `git pull` and records an
empty `git_head`; use a separate checkout (the default
`/opt/flasharray-eda-baseline`) when the push webhook should fast-forward
the baseline before applying it.

Settings the reconcile and apply playbooks currently manage:

| Key | Module | On audit of |
| --- | --- | --- |
| `ntp_servers` | `everpure.flasharray.purefa_ntp` | `purearray setattr --ntpserver` |
| `dns.domain`, `dns.nameservers` | `everpure.flasharray.purefa_dns` (>= 1.46.0) | `puredns setattr` |

Other governed commands (directory services, eradication config, admin
settings, SMTP, syslog, SNMP, alert watchers) are reported with the audit
details but not rewritten: each of those either needs a secret the
baseline must not hold or can lock an administrator out.
