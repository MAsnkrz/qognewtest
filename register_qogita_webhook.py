"""
One-time setup script: registers your Cloudflare Worker URL with Qogita
as a webhook endpoint, subscribed to catalog_download.completed and
catalog_download.failed.

Run this ONCE after deploying the Cloudflare Worker (you need the
Worker's live URL first, e.g. https://qogita-webhook-receiver.<you>.workers.dev/).

The response includes a `signingSecret` that is shown ONLY ONCE — copy
it immediately into your Cloudflare Worker's secrets
(QOGITA_SIGNING_SECRET). If you lose it, delete the webhook endpoint
and run this script again to get a fresh one.

Usage:
  python3 register_qogita_webhook.py
"""

import os
import requests

API_BASE = "https://api.qogita.com"
EMAIL    = os.getenv("QOGITA_EMAIL", "dapaplays@gmail.com")
PASSWORD = os.getenv("QOGITA_PASSWORD", "Sufsat-gucqum-5detse")

# >>> EDIT THIS to your real deployed Worker URL before running <<<
WORKER_URL = "https://qogita-webhook-receiver.YOUR-SUBDOMAIN.workers.dev/"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
})

print("Logging in...")
r = SESSION.post(f"{API_BASE}/auth/login/", json={"email": EMAIL, "password": PASSWORD}, timeout=15)
r.raise_for_status()
token = r.json().get("accessToken") or r.json().get("access")
headers = {"Authorization": f"Bearer {token}"}
print("Authenticated.\n")

if "YOUR-SUBDOMAIN" in WORKER_URL:
    print("!! Edit WORKER_URL at the top of this script to your real deployed")
    print("!! Cloudflare Worker URL before running this for real.")
    raise SystemExit(1)

print(f"Registering webhook endpoint: {WORKER_URL}")
r = SESSION.post(
    f"{API_BASE}/public/webhooks/",
    json={
        "url": WORKER_URL,
        "eventTypes": ["catalog_download.completed", "catalog_download.failed"],
        "description": "Makeup/Health category monitor webhook",
    },
    headers=headers,
    timeout=15,
)

if r.status_code == 409:
    print("A webhook endpoint with this URL already exists for your account.")
    print("Use list_qogita_webhooks.py to see existing endpoints, or delete it first.")
    raise SystemExit(1)

r.raise_for_status()
data = r.json()

print("\n" + "=" * 60)
print("WEBHOOK REGISTERED SUCCESSFULLY")
print("=" * 60)
print(f"  qid:            {data['qid']}")
print(f"  url:            {data['url']}")
print(f"  eventTypes:     {data['eventTypes']}")
print(f"  enabled:        {data['enabled']}")
print()
print("  signingSecret (SHOWN ONLY ONCE — copy this now):")
print(f"  {data['signingSecret']}")
print("=" * 60)
print()
print("Next step: set this as a Cloudflare Worker secret:")
print("  wrangler secret put QOGITA_SIGNING_SECRET")
print("  (then paste the signingSecret value when prompted)")
