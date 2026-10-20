#!/usr/bin/env python3
"""Ask an array to send its test alert to every configured alert watcher.

    FA_URL=https://<array> FA_TOKEN=<token> python3 tests/send_test_alert.py

Purity sends `purity.test` to email, syslog and SNMP destinations. It does
not create an entry under /alerts, so this exercises the delivery path, not
the alerts signal. Prints one line per destination with the result.
"""
import os
import sys

import urllib3
from pypureclient import flasharray

urllib3.disable_warnings()
host = os.environ["FA_URL"].replace("https://", "").strip("/")
client = flasharray.Client(target=host, api_token=os.environ["FA_TOKEN"], verify_ssl=False)
watchers = client.get_alert_watchers()
names = [w.name for w in watchers.items] if hasattr(watchers, "items") else []
resp = client.get_alert_watchers_test(names=names)  # the endpoint needs explicit watcher names
if not hasattr(resp, "items"):
    print("test failed:", resp.errors[0].message if getattr(resp, "errors", None) else resp)
    sys.exit(1)
for item in resp.items:
    d = item.to_dict()
    print(f"{d.get('destination')}: {d.get('component_name') or d.get('component_type')} "
          f"{'ok' if d.get('success') else 'FAILED'} {d.get('result_details') or ''}")
