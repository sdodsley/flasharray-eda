# Phase 3 record: approvals, QoS hysteresis, the composite rule

Date: 2026-10-20. Same lab and tooling as [PHASE0.md](PHASE0.md).

## What was built

- **Approval gate** (`capacity.yml`, `capacity_request.yml`,
  `capacity_reclaim.yml`). A capacity breach writes a request file with an
  id, the days-to-full estimate, the top growers, what is pending eradication
  and the arrays with headroom. A second ruleset on `ansible.eda.webhook`
  takes `{request_id, approve, approver, action}`; only an approved request
  whose action was proposed runs, and `eradicate_pending` eradicates
  destroyed volumes and snapshots, reporting anything the array refuses. In
  AAP the webhook ruleset is replaced by a workflow approval node.
- **QoS cap with hysteresis** (`performance.yml`, `qos_cap.yml`). A volume
  over the latency threshold for N polls, holding a large share of a hot
  array's IOPS, gets a temporary IOPS cap. The cap is recorded as volume tags
  in namespace `eda` (`qos_cap` = when, `qos_prev_iops` = the limit before),
  which the telemetry signal reads back as `eda_capped`; after M normal polls
  the previous limit is restored and the tags removed. State lives on the
  array, not in the rulebook.
- **Composite anomaly rule** (`anomaly.yml`, `protect_now.yml`). One ruleset,
  two signals, an `all` condition with a 15 minute timeout joining a data
  reduction drop, a snapshot or write surge and a human's destructive
  snapshot command on the same array. The response snapshots every local
  protection group, lengthens the enabled eradication delay and pages.

## Exit test results

| Step | What was done | Result |
| --- | --- | --- |
| approval gate | a scratch volume destroyed (pending eradication); rulebook with a 1 % threshold; webhook approval 45 s later | request `<array>-<stamp>` written and logged; on approval the scratch volume was eradicated and a SafeMode-locked snapshot was reported as refused by the array, not forced |
| QoS cap | thresholds lowered so the lab's one busy volume counted as a noisy neighbour | IOPS limit 100000 applied, tags `qos_cap` and `qos_prev_iops` set, record written |
| QoS lift | thresholds raised so the same volume read as normal | after two normal polls the limit was cleared and the tags removed |
| anomaly | three replayed signals for the array | the multi-event condition fired; snapshots created on every protection group with members (empty groups reported as failures); enabled eradication delay went from 1 to 2 days and was restored by the test; the snapshot in a SafeMode-protected group could not be eradicated afterwards, as designed |

## What the lab taught us

- SDK models raise `AttributeError` for fields the array did not return
  (`volume.qos.iops_limit` on an uncapped volume); use `to_dict()`.
- `purefa_volume_tags` splits `key:value` on every colon and removes only an
  exact pair; timestamps need a colon-free value, so the tags are set and
  removed with the SDK.
- `purefa_volume` takes `iops_qos: 0` to clear a limit and rejects the
  array's own "no limit" value 100000000.
- `purefa_eradication` cannot change an array whose enabled and disabled
  delays have diverged, and defaults the disabled delay to one day; the
  protect playbook patches `enabled_delay` with the SDK and keeps
  `disabled_delay` as it was.
- Volume and array performance samples are not taken at the same instant;
  the IOPS share is clamped to 100 %.
- The multi-event `all` condition with `events.x <<` assignment, an `or`
  across two event types and a `timeout` parses and fires on
  ansible-rulebook 1.3.2.
