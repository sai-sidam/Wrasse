"""Print this machine's public IP, then wait until MongoDB Atlas accepts the connection.

Lets a human add the IP under Atlas → Network Access while a CI run waits.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.request

from pymongo import MongoClient
from pymongo.errors import OperationFailure

ip = urllib.request.urlopen("https://api.ipify.org", timeout=15).read().decode().strip()
wait_min = float(os.getenv("ATLAS_WAIT_MINUTES", "20"))
print("=" * 70)
print(f"ADD THIS IP IN ATLAS → Network Access → Add IP Address:   {ip}")
print(f"waiting up to {wait_min:g} minutes for Atlas to accept connections...")
print("=" * 70, flush=True)

deadline = time.time() + wait_min * 60
while time.time() < deadline:
    try:
        MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=8000).admin.command("ping")
        print(f"Atlas accepted {ip}. Continuing.", flush=True)
        sys.exit(0)
    except OperationFailure as e:
        sys.exit(f"Atlas reached, but it rejected the login ({e.details.get('errmsg') if e.details else e}). "
                 "Fix the username/password in the MONGODB_URI secret.")
    except Exception as e:
        print(f"  not yet ({type(e).__name__}); still waiting for {ip} to be allowed...", flush=True)
        time.sleep(10)
sys.exit(f"Atlas never accepted {ip} within {wait_min:g} minutes")
