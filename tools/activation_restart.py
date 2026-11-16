#!/usr/bin/env python3
# Copyright 2026 Everpure, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Restart the FlashArray activations that are not running, one at a time.

After a host reboot the platform's own restart logic is not enough: the
activation worker marks an instance that had already reached its restart
ceiling as failed and does not restart it, and the restarts it does attempt
can meet the websocket proxy before it is ready and fail as well. This tool
waits for the EDA API, then reads each activation named for a platform
rulebook file (tools/activation_audit.py's names and client) and restarts
every one that is not running, waiting for each to run before the next so
the host sees no start storm. It is what a post-boot user unit runs.

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


def restart_one(client, activation, minutes, attempts, say):
    """Restart one activation and wait for it to run; a start that fails (the
    websocket proxy not ready yet) is tried again up to `attempts` times."""
    aid, name = activation["id"], activation["name"]
    for attempt in range(1, attempts + 1):
        say(f"restarting {name} (id {aid}, attempt {attempt} of {attempts})")
        client.request("POST", f"/api/eda/v1/activations/{aid}/restart/", {})
        deadline = time.monotonic() + minutes * 60
        state = None
        while time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            try:
                status, detail = client.request(
                    "GET", f"/api/eda/v1/activations/{aid}/"
                )
            except AuditError as exc:
                say(f"could not read {name} ({str(exc)[-80:]}); still waiting")
                continue
            state = detail.get("status") if isinstance(detail, dict) else state
            if state == "running":
                return True
            if state in ("failed", "stopped", "error"):
                say(
                    f"{name} ended {state}: {(detail.get('status_message') or '')[:100]}"
                )
                break
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
    args = parser.parse_args(argv)

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
    running, restarted, failed, absent = [], [], [], []
    try:
        for name in names:
            activation = client.get_by_name(name)
            if activation is None:
                absent.append(name)
                continue
            if activation.get("status") == "running":
                running.append(name)
                continue
            if restart_one(
                client, activation, args.restart_minutes, args.attempts, say
            ):
                restarted.append(name)
            else:
                failed.append(name)
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
