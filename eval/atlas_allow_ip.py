"""Add this machine's public IP to an Atlas project's access list, as a temporary entry.

For CI runners on orgs whose policy forbids 0.0.0.0/0. Uses an Atlas service account
(ATLAS_CLIENT_ID / ATLAS_CLIENT_SECRET, Project Owner) and ATLAS_PROJECT_ID. The entry
deletes itself after ATLAS_ALLOW_HOURS (default 2), so nothing needs cleaning up.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://cloud.mongodb.com"
ACCEPT = "application/vnd.atlas.2023-01-01+json"


def http(method: str, url: str, headers: dict, body: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> HTTP {e.code}: {e.read().decode()[:500]}")
    return json.loads(raw) if raw else {}


def main() -> None:
    cid, secret, project = (os.environ[k] for k in ("ATLAS_CLIENT_ID", "ATLAS_CLIENT_SECRET", "ATLAS_PROJECT_ID"))
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    token = http("POST", f"{API}/api/oauth/token",
                 {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded",
                  "Accept": "application/json"}, b"grant_type=client_credentials")["access_token"]
    auth = {"Authorization": f"Bearer {token}", "Accept": ACCEPT, "Content-Type": "application/json"}

    ip = urllib.request.urlopen("https://api.ipify.org", timeout=15).read().decode().strip()
    expires = (datetime.now(timezone.utc) + timedelta(hours=float(os.getenv("ATLAS_ALLOW_HOURS", "2"))))
    entry = [{"ipAddress": ip, "comment": "wrasse demo runner (temporary)",
              "deleteAfterDate": expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")}]
    http("POST", f"{API}/api/atlas/v2/groups/{project}/accessList", auth, json.dumps(entry).encode())
    print(f"added {ip} to the access list until {entry[0]['deleteAfterDate']}")

    for _ in range(24):                                   # wait up to 2 minutes for it to take effect
        status = http("GET", f"{API}/api/atlas/v2/groups/{project}/accessList/{ip}/status", auth).get("STATUS")
        if status == "ACTIVE":
            print("access list entry is ACTIVE")
            return
        time.sleep(5)
    print("entry not ACTIVE yet after 2 minutes; continuing anyway")


if __name__ == "__main__":
    main()
