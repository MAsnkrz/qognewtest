"""
List all registered Qogita webhook endpoints for your account, and
optionally send a test event to confirm your Worker is reachable and
signature verification works.

Usage:
  python3 list_qogita_webhooks.py
  python3 list_qogita_webhooks.py --test    (also sends a test event)
"""

import os
import sys
import requests

API_BASE = "https://api.qogita.com"
EMAIL    = os.getenv("QOGITA_EMAIL", "dapaplays@gmail.com")
PASSWORD = os.getenv("QOGITA_PASSWORD", "Sufsat-gucqum-5detse")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
})

r = SESSION.post(f"{API_BASE}/auth/login/", json={"email": EMAIL, "password": PASSWORD}, timeout=15)
r.raise_for_status()
token = r.json().get("accessToken") or r.json().get("access")
headers = {"Authorization": f"Bearer {token}"}

r = SESSION.get(f"{API_BASE}/public/webhooks/", headers=headers, timeout=15)
r.raise_for_status()
data = r.json()

print(f"Total webhook endpoints: {data['count']}\n")
for ep in data["results"]:
    print(f"  qid:         {ep['qid']}")
    print(f"  url:         {ep['url']}")
    print(f"  eventTypes:  {ep['eventTypes']}")
    print(f"  enabled:     {ep['enabled']}")
    print(f"  description: {ep.get('description', '')}")
    print()

if "--test" in sys.argv:
    print("Sending test event to all enabled endpoints...")
    r = SESSION.post(f"{API_BASE}/public/webhooks/test-event", headers=headers, timeout=15)
    r.raise_for_status()
    print(r.json().get("message"))
    print("\nCheck your Cloudflare Worker logs (wrangler tail) to confirm receipt.")
