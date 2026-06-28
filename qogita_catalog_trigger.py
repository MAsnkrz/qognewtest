"""
Fires an asynchronous catalog-download request for a given category.
This script does NOT wait for the result — it just kicks off the job.
The actual CSV is delivered later via webhook -> Cloudflare Worker ->
GitHub repository_dispatch -> the matching *_category_monitor.py script.

Requires a webhook endpoint already registered and subscribed to
catalog_download.completed/.failed (see register_qogita_webhook.py),
otherwise this request is rejected with 400 no_webhook_subscriber.

Env vars:
  QOGITA_EMAIL, QOGITA_PASSWORD - login credentials
  CATEGORY_SLUG                 - e.g. "makeup" or "health"

Rate limit: Qogita allows 3 of these requests per minute per user.
"""

import os
import sys
import requests

API_BASE      = "https://api.qogita.com"
EMAIL         = os.getenv("QOGITA_EMAIL", "")
PASSWORD      = os.getenv("QOGITA_PASSWORD", "")
CATEGORY_SLUG = os.getenv("CATEGORY_SLUG", "")

if not EMAIL or not PASSWORD:
    print("[!] QOGITA_EMAIL and QOGITA_PASSWORD must be set")
    sys.exit(1)
if not CATEGORY_SLUG:
    print("[!] CATEGORY_SLUG must be set (e.g. 'makeup' or 'health')")
    sys.exit(1)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
})

print(f"Triggering catalog download for category_slug='{CATEGORY_SLUG}'...")

r = SESSION.post(f"{API_BASE}/auth/login/", json={"email": EMAIL, "password": PASSWORD}, timeout=15)
r.raise_for_status()
token = r.json().get("accessToken") or r.json().get("access")
headers = {"Authorization": f"Bearer {token}"}

r = SESSION.post(
    f"{API_BASE}/public/buyers/catalog-downloads/",
    json={"category_slug": [CATEGORY_SLUG]},
    headers=headers,
    timeout=15,
)

if r.status_code == 400:
    body = r.json()
    if body.get("code") == "no_webhook_subscriber":
        print("[!] No enabled webhook endpoint is subscribed to catalog download events.")
        print("[!] Run register_qogita_webhook.py first.")
        sys.exit(1)
    print(f"[!] 400 error: {body}")
    sys.exit(1)

if r.status_code == 429:
    print(f"[!] Rate limited. Retry-After: {r.headers.get('Retry-After')}s")
    sys.exit(1)

r.raise_for_status()
data = r.json()
print(f"Accepted. catalogRequestId: {data.get('catalogRequestId')}")
print("The result will arrive via webhook in a few minutes.")
