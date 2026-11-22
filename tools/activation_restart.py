#!/usr/bin/env python3
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Restart the FlashArray activations that are not running, one at a time.

After a host reboot the platform's own restart logic is not enough: the
activation worker marks an instance that had already reached its restart
ceiling as failed and does not restart it, and the restarts it does attempt
can meet the websocket proxy before it is ready and fail as well. This tool
waits for the EDA API, pauses, then reads each activation named for a
platform rulebook file (tools/activation_audit.py's names and client) and
restarts every one that is not running, waiting for each to run before the
next so the host sees no start storm. It keeps watching for --watch-minutes
(default 10) after the API answers, since the activation worker reconciles
the instances that did not survive the boot on its own schedule, seconds or
minutes later, and until then they read as running; --passes (default 2)
bounds the restarts tried per activation, and an activation is read again
right before acting on it. It is what a post-boot user unit runs.

The platform is read as the audit reads it: AAP_HOSTNAME and AAP_TOKEN
(AAP_VALIDATE_CERTS false for a lab certificate), or --url and --insecure.
"""

import argparse
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from activation_audit import (  # noqa: E402
    ENV_TOKEN,
    ENV_URL,
    RULEBOOKS_DIR,
    AuditError,
    Client,
    env_insecure,
    env_url,
    expected_names,
)

STATUS_PATH = "/api/eda/v1/status/"
POLL_SECONDS = 10
SETTLE_SECONDS = 30
WATCH_POLL_SECONDS = 30


def wait_for_api(client, minutes, say):
    """True once the EDA API answers its status endpoint, within the budget."""
    deadline = time.monotonic() + minutes * 60
    while True:
        try:
            status, _ = client.request("GET", STATUS_PATH)
            if status == 200:
                return True
            say(f"the EDA API answered {status}; waiting")
        except AuditError as exc:
            say(f"the EDA API is not answering ({str(exc)[-80:]}); waiting")
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


STARTING = ("starting", "pending", "workers offline")
GRACE_SECONDS = 60


def wait_running(client, aid, name, minutes, say, before=None):
    """Wait for the activation to run. Returns "running", "ended" (it reached a
    final state that is the result of this attempt) or "timeout". With
    `before` (the restart count seen before a POST), a final state is only
    taken as this attempt's result once the count has moved on, or after the
    grace period, so a stale read right after the POST does not count."""
    deadline = time.monotonic() + minutes * 60
    started = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            status, detail = client.request("GET", f"/api/eda/v1/activations/{aid}/")
        except AuditError as exc:
            say(f"could not read {name} ({str(exc)[-80:]}); still waiting")
            continue
        if not isinstance(detail, dict):
            continue
        state = detail.get("status")
        if state == "running":
            return "running"
        if state in ("failed", "stopped", "error"):
            moved = before is None or detail.get("restart_count") != before
            if moved or time.monotonic() - started >= GRACE_SECONDS:
                say(
                    f"{name} ended {state}: {(detail.get('status_message') or '')[:100]}"
                )
                return "ended"
    return "timeout"


def restart_one(client, activation, minutes, attempts, say):
    """Bring one activation to running: wait for one that is already starting;
    otherwise restart it and wait, trying again up to `attempts` times when
    the start fails (the websocket proxy not ready yet)."""
    aid, name = activation["id"], activation["name"]
    if activation.get("status") in STARTING:
        say(f"{name} (id {aid}) is {activation.get('status')}; waiting for it")
        if wait_running(client, aid, name, minutes, say) == "running":
            return True
    for attempt in range(1, attempts + 1):
        say(f"restarting {name} (id {aid}, attempt {attempt} of {attempts})")
        before = activation.get("restart_count")
        try:
            status, detail = client.request("GET", f"/api/eda/v1/activations/{aid}/")
            if isinstance(detail, dict):
                before = detail.get("restart_count", before)
        except AuditError:
            pass
        client.request("POST", f"/api/eda/v1/activations/{aid}/restart/", {})
        if wait_running(client, aid, name, minutes, say, before=before) == "running":
            return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="activation_restart",
        description="Restart the FlashArray activations that are not running, one at a "
        "time, after waiting for the EDA API (a post-boot step).",
    )
    parser.add_argument(
        "--url",
        default="",
        help="the platform gateway (default: AAP_HOSTNAME or CONTROLLER_HOST)",
    )
    parser.add_argument(
        "--insecure", action="store_true", help="do not verify the TLS certificate"
    )
    parser.add_argument(
        "--rulebooks",
        default=str(RULEBOOKS_DIR),
        metavar="DIR",
        help="the platform rulebooks directory; one activation named for each file is read",
    )
    parser.add_argument(
        "--wait-minutes",
        type=float,
        default=10,
        metavar="N",
        help="how long to wait for the EDA API (default 10)",
    )
    parser.add_argument(
        "--restart-minutes",
        type=float,
        default=6,
        metavar="N",
        help="how long to wait for each activation to run (default 6)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        metavar="N",
        help="starts tried per activation (default 3)",
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=2,
        metavar="N",
        help="restarts tried per activation across the passes (default 2); one "
        "still not running after them is given up",
    )
    parser.add_argument(
        "--watch-minutes",
        type=float,
        default=10,
        metavar="N",
        help="keep reading the activations for this long after the EDA API answers "
        "(default 10): the worker reconciles the instances that did not survive "
        "the boot on its own schedule, sometimes minutes later",
    )
    args = parser.parse_args(argv)
    if args.passes < 1:
        parser.error("--passes must be at least 1")
    if args.watch_minutes < 0:
        parser.error("--watch-minutes must not be negative")
    if args.attempts < 1:
        parser.error("--attempts must be at least 1")

    token = os.environ.get(ENV_TOKEN, "")
    if not token:
        parser.error(f"set {ENV_TOKEN} in the environment")
    url = args.url or env_url()
    if not url:
        parser.error(f"give --url or set {ENV_URL[0]} in the environment")
    names = expected_names(args.rulebooks)
    if not names:
        parser.error(f"no *.yml rulebook in {args.rulebooks}; give --rulebooks")

    def say(text):
        print(text, flush=True)

    client = Client(url, token, insecure=args.insecure or env_insecure())
    if not wait_for_api(client, args.wait_minutes, say):
        print(
            f"activation_restart: the EDA API did not answer within {args.wait_minutes} minutes",
            file=sys.stderr,
        )
        return 1
    # The activation worker reconciles the instances that did not survive
    # the boot on its own schedule, seconds or minutes after the API answers;
    # until it has, every one of them still reads as running. So the tool
    # pauses, then keeps making passes until the watch window has passed and
    # a pass finds nothing to do; --passes bounds the restarts per activation.
    api_ready = time.monotonic()
    time.sleep(SETTLE_SECONDS)
    running, restarted, failed, absent = [], [], [], []
    tries = {}  # restarts tried per name; --passes is the budget
    pass_no = 0
    try:
        while True:
            pass_no += 1
            left = args.watch_minutes * 60 - (time.monotonic() - api_ready)
            pending = []
            read_error = None
            for name in names:
                if name in absent or name in failed:
                    continue
                try:
                    activation = client.get_by_name(name)
                except AuditError as exc:
                    read_error = exc
                    break
                if activation is None:
                    absent.append(name)
                    continue
                if activation.get("status") == "running":
                    if name not in running and name not in restarted:
                        running.append(name)
                    continue
                pending.append(activation)
            if read_error is not None:
                # the gateway answers 5xx or 403 for minutes while the platform
                # settles; inside the window that is a pass to try again
                if left <= 0:
                    raise read_error
                say(
                    f"could not read the activations ({str(read_error)[-80:]}); trying again"
                )
                time.sleep(min(WATCH_POLL_SECONDS, left))
                continue
            if not pending:
                if left <= 0:
                    break
                time.sleep(min(WATCH_POLL_SECONDS, left))
                continue
            if pass_no > 1:
                say(
                    f"pass {pass_no}: {len(pending)} activation(s) not running "
                    "after the last pass"
                )
            acted = False
            for activation in pending:
                name = activation["name"]
                # the pass's reads are minutes old by the time a later
                # activation's turn comes: read it again, and leave alone one
                # the platform brought up meanwhile
                try:
                    fresh = client.get_by_name(name)
                except AuditError:
                    fresh = None
                if fresh is not None:
                    activation = fresh
                if activation.get("status") == "running":
                    if name not in running and name not in restarted:
                        running.append(name)
                    continue
                if name in running:
                    running.remove(name)
                acted = True
                tries[name] = tries.get(name, 0) + 1
                if tries[name] > args.passes:
                    # restarted as often as allowed: one that is starting gets
                    # its wait, nothing else is tried again
                    outcome = "ended"
                    if activation.get("status") in STARTING:
                        outcome = wait_running(
                            client, activation["id"], name, args.restart_minutes, say
                        )
                    if outcome == "running":
                        if name not in running and name not in restarted:
                            running.append(name)
                    else:
                        if name in restarted:
                            restarted.remove(name)
                        failed.append(name)
                        say(f"giving up on {name} after {args.passes} restart(s)")
                    continue
                if restart_one(
                    client, activation, args.restart_minutes, args.attempts, say
                ):
                    if name not in restarted:
                        restarted.append(name)
                else:
                    if name in restarted:
                        restarted.remove(name)
                    failed.append(name)
            if not acted:
                # every pending activation ran by its re-read: a clean pass
                left = args.watch_minutes * 60 - (time.monotonic() - api_ready)
                if left <= 0:
                    break
                time.sleep(min(WATCH_POLL_SECONDS, left))
    except AuditError as exc:
        print(f"activation_restart: {exc}", file=sys.stderr)
        return 1
    if absent:
        say("not created (the seed makes them): " + ", ".join(absent))
    print(
        f"activation_restart: {len(running)} running, {len(restarted)} restarted, "
        f"{len(failed)} failed"
        + (f", {len(absent)} not created" if absent else "")
        + (": " + ", ".join(failed) if failed else ""),
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
