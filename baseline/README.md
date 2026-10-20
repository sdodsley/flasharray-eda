# Baseline

Desired state per array, one YAML file named after the inventory host
(`<array>.yml`). This directory is the "git baseline": in production it is
its own repository, checked out on the EDA host, pulled by
`playbooks/apply_baseline.yml` when the change control rulebook receives
a push webhook, and read by `playbooks/reconcile.yml` when an audited
out-of-band change needs reverting.

`example.yml` shows the schema. Site files are gitignored as
`local-*.yml`; name a real one `<array>.yml` in the baseline repository.

Settings the reconcile and apply playbooks currently manage:

| Key | Module | On audit of |
| --- | --- | --- |
| `ntp_servers` | `everpure.flasharray.purefa_ntp` | `purearray setattr --ntpserver` |
| `dns.domain`, `dns.nameservers` | `playbooks/set_dns.yml` (SDK) | `puredns setattr` |

Other governed commands (directory services, eradication config, admin
settings, SMTP, syslog, SNMP, alert watchers) are reported with the audit
details but not rewritten: each of those either needs a secret the
baseline must not hold or can lock an administrator out.
