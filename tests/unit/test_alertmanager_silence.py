"""playbooks/alertmanager_silence.yml creates or expires an Alertmanager
silence on the replica-link rules of named arrays, and is a recorded no-op
without an Alertmanager."""

import re

import yaml


def _play(repo_root):
    return yaml.safe_load(
        (repo_root / "playbooks" / "alertmanager_silence.yml").read_text()
    )[0]


def _task(play, name):
    return next(t for t in play["tasks"] if t["name"] == name)


def test_silence_targets_the_link_rules_of_the_named_arrays(repo_root):
    play = _play(repo_root)
    assert play["hosts"] == "localhost"
    assert play["vars"]["silence_alertname_regex"] == "FlashArrayReplicaLink.*"
    create = _task(play, "Create the silence")
    body = create["ansible.builtin.uri"]["body"]
    matchers = {m["name"]: m for m in body["matchers"]}
    assert matchers["alertname"]["value"] == "{{ silence_alertname_regex }}"
    assert matchers["alertname"]["isRegex"] is True
    assert matchers["array_name"]["value"] == "{{ silence_array_regex }}"
    assert matchers["array_name"]["isRegex"] is True
    assert "mode == 'create'" in create["when"]
    assert "alertmanager_url | default('') | length > 0" in create["when"]
    assert create["ansible.builtin.uri"]["status_code"] == [200]
    # an Alertmanager that does not answer is recorded, never fatal
    assert create.get("failed_when") is False
    expire = _task(play, "Expire the silence")
    assert expire.get("failed_when") is False
    assert expire["ansible.builtin.uri"]["method"] == "DELETE"
    assert "silence_id | default('') | length > 0" in expire["when"]
    # expiring a silence that is already gone is not a failure
    assert 404 in expire["ansible.builtin.uri"]["status_code"]
    # endsAt comes from the local-naive now(), the value timestamp() expects;
    # now(utc=true) is naive too and would shift the epoch by the zone offset
    assert "silence_minutes" in body["endsAt"]
    assert "now().timestamp()" in body["endsAt"]
    assert "now(utc=true).timestamp()" not in body["endsAt"]


def test_guard_requires_the_array_regex_only_on_create(repo_root):
    play = _play(repo_root)
    guard = _task(play, "Refuse a mode or a regex that is not what the workflow sends")
    clauses = guard["ansible.builtin.assert"]["that"]
    that = "\n".join(clauses)
    assert "mode in ['create', 'expire']" in that
    assert "alertmanager_url" in that
    clause = next(c for c in clauses if "silence_array_regex" in c)
    # the expire node sends no regex, so the clause must not look at it then
    assert clause.strip().startswith("mode == 'expire' or")
    pattern = re.search(r"is match\('([^']+)'\)", clause).group(1)
    assert re.match(pattern, "array-a|array-b")
    assert re.match(pattern, "z-fa.site_1")
    for bad in ("", ".*", "|a", "a|", "a||b", "a b"):
        assert not re.match(pattern, bad), bad
    # and nothing defaults the regex to "everything"
    text = (repo_root / "playbooks" / "alertmanager_silence.yml").read_text()
    assert "default('.*')" not in text


def test_silence_is_a_recorded_no_op_without_an_alertmanager_and_publishes_its_id(
    repo_root,
):
    play = _play(repo_root)
    note = _task(play, "Note when no Alertmanager is configured")
    assert note["when"] == "alertmanager_url | default('') | length == 0"
    record = _task(play, "Record the silence")["ansible.builtin.lineinfile"]
    assert record["path"] == "{{ alert_log }}"
    assert "'kind': 'alertmanager_silence'" in record["line"]
    stats = play["tasks"][-1]
    assert "ansible.builtin.set_stats" in stats
    data = stats["ansible.builtin.set_stats"]["data"]
    for key in ("'silence_id'", "'mode'", "'alertmanager'", "'changed'", "'error'"):
        assert key in data
    # touches no array: no token, no array resolution
    text = (repo_root / "playbooks" / "alertmanager_silence.yml").read_text()
    assert "api_token" not in text and "resolve_array" not in text
