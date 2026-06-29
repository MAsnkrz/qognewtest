"""
Fires an asynchronous catalog-download request, scoped to ALL leaf
sub-categories beneath a given top-level category (e.g. "Makeup" or
"Health"). This script does NOT wait for the result — it just kicks
off the job. The actual CSV is delivered later via webhook ->
Cloudflare Worker -> GitHub repository_dispatch -> the matching
*_category_monitor.py script.

Why this expands to descendant slugs:
  The catalog-download API's `category_slug` filter is an EXACT match
  only — it does NOT automatically include child categories. Real
  products are tagged with specific leaf categories (e.g. "Mascara",
  "Lip Balm"), essentially never with the broad parent name itself
  (e.g. "Makeup"). So requesting category_slug=["makeup"] alone
  matches ~zero real products. To get full category coverage (the
  same set the website's own "Download catalog" button produces), we
  fetch the live category tree and submit every leaf slug under the
  target top-level category.

Requires a webhook endpoint already registered and subscribed to
catalog_download.completed/.failed (see register_qogita_webhook.py),
otherwise this request is rejected with 400 no_webhook_subscriber.

Env vars (set exactly ONE of these):
  TOP_LEVEL_CATEGORY - e.g. "Makeup" or "Health" (expands to all
                       descendant leaf slugs automatically)
  BRAND_NAME         - e.g. "Maybelline" — for pipeline debugging

Also requires:
  QOGITA_EMAIL, QOGITA_PASSWORD - login credentials

Rate limit: Qogita allows 3 catalog-download requests per minute per
user. Category listing calls are separate and not rate-limited the
same way, but this script only calls /categories/ once per run.
"""

import os
import sys
import requests

API_BASE            = "https://api.qogita.com"
EMAIL                = os.getenv("QOGITA_EMAIL", "")
PASSWORD             = os.getenv("QOGITA_PASSWORD", "")
TOP_LEVEL_CATEGORY  = os.getenv("TOP_LEVEL_CATEGORY", "")
BRAND_NAME           = os.getenv("BRAND_NAME", "")

if not EMAIL or not PASSWORD:
    print("[!] QOGITA_EMAIL and QOGITA_PASSWORD must be set")
    sys.exit(1)
if not TOP_LEVEL_CATEGORY and not BRAND_NAME:
    print("[!] Set either TOP_LEVEL_CATEGORY (e.g. 'Makeup') or BRAND_NAME (e.g. 'Maybelline')")
    sys.exit(1)

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


def fetch_all_categories():
    all_categories = []
    url = f"{API_BASE}/categories/"
    params = {"page_size": 200}
    page = 1
    while url:
        r = SESSION.get(url, headers=headers, params=params if page == 1 else None, timeout=30)
        r.raise_for_status()
        data = r.json()
        all_categories.extend(data.get("results", []))
        url = data.get("next")
        page += 1
        if page > 30:
            break
    return all_categories


if TOP_LEVEL_CATEGORY:
    print(f"Fetching category tree to expand '{TOP_LEVEL_CATEGORY}' into descendant slugs...")
    all_categories = fetch_all_categories()

    # Every category whose path contains TOP_LEVEL_CATEGORY at index 1
    # (immediately under the root "Health & Beauty" / "Home & Garden"
    # level) is a descendant we want — this naturally includes the
    # target category itself plus every leaf beneath it, at any depth.
    descendant_slugs = [
        c["slug"] for c in all_categories
        if len(c.get("path", [])) >= 2 and c["path"][1] == TOP_LEVEL_CATEGORY
    ]

    if not descendant_slugs:
        print(f"[!] No categories found with '{TOP_LEVEL_CATEGORY}' as the top-level grouping.")
        print(f"[!] Check the exact category name (case-sensitive) and try again.")
        sys.exit(1)

    print(f"Found {len(descendant_slugs)} descendant categories under '{TOP_LEVEL_CATEGORY}'")
    request_body = {"category_slug": descendant_slugs}
    label = f"TOP_LEVEL_CATEGORY='{TOP_LEVEL_CATEGORY}' ({len(descendant_slugs)} slugs)"
else:
    request_body = {"brand_names": [BRAND_NAME]}
    label = f"brand_names=['{BRAND_NAME}'] (debug test)"

print(f"\nTriggering catalog download for {label}...")

r = SESSION.post(
    f"{API_BASE}/public/buyers/catalog-downloads/",
    json=request_body,
    headers=headers,
    timeout=30,
)

if r.status_code == 400:
    body = r.json()
    if body.get("code") == "no_webhook_subscriber":
        print("[!] No enabled webhook endpoint is subscribed to catalog download events.")
        print("[!] Run register_qogita_webhook.py first.")
        sys.exit(1)
    print(f"[!] 400 error: {body}")
    sys.exit(1)

if r.status_code == 422:
    print(f"[!] 422 validation error: {r.text[:500]}")
    sys.exit(1)

if r.status_code == 429:
    print(f"[!] Rate limited. Retry-After: {r.headers.get('Retry-After')}s")
    sys.exit(1)

r.raise_for_status()
data = r.json()
print(f"Accepted. catalogRequestId: {data.get('catalogRequestId')}")
print("The result will arrive via webhook in a few minutes.")
