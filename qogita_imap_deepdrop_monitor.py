"""
Qogita Full Catalog Deep Drop Monitor — Gmail IMAP version

Runs on a schedule via GitHub Actions. Connects to Gmail via IMAP,
searches for unread emails from Qogita containing a catalog Excel
attachment, downloads and parses the attachment, then alerts Discord
for any product with a 50%+ price drop vs the stored baseline.

The first run builds the baseline silently (no alerts). Every
subsequent run compares against it.

Setup:
  - Enable 2-Step Verification on your Google account
  - Generate a Gmail App Password at myaccount.google.com/apppasswords
  - Add GMAIL_APP_PASSWORD as a GitHub repo secret
  - Trigger a "Full catalog" download from qogita.com/categories/
    via the "Download catalog" button whenever you want to refresh
    the baseline (e.g. weekly)

Env vars:
  GMAIL_ADDRESS          - your Gmail address (e.g. dapaplays@gmail.com)
  GMAIL_APP_PASSWORD     - the 16-char App Password from Google
  DISCORD_WEBHOOK_FULLCATALOG_DEEPDROP - Discord webhook URL

Deps: pip install openpyxl requests
"""

import email
import imaplib
import io
import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timezone

GMAIL_ADDRESS   = os.getenv("GMAIL_ADDRESS", "dapaplays@gmail.com")
GMAIL_PASSWORD  = os.getenv("GMAIL_APP_PASSWORD", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_FULLCATALOG_DEEPDROP", "")

SNAPSHOT_FILE   = "snapshot_qogita_fullcatalog_deepdrop_prices.json"
BASELINE_FLAG   = "baseline_done_fullcatalog_deepdrop.txt"

MIN_DROP_PCT    = 0.50
MIN_ABS_DROP    = 0.05

QOGITA_SENDER   = "noreply@qogita.com"
IMAP_SERVER     = "imap.gmail.com"
IMAP_PORT       = 993

FNF_ROLE_ID  = "1019772687528235099"
FNF_MENTION  = f"<@&{FNF_ROLE_ID}>"

COLOUR_DEEP_DROP = 0xFF0066


def connect_gmail():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
    return mail


def fetch_latest_catalog_attachment(mail):
    mail.select("INBOX")

    # Search for catalog download emails from Qogita
    status, messages = mail.search(None, 'FROM "qogita.com" SUBJECT "catalog download is ready"')
    if status != "OK" or not messages[0]:
        # Fallback — broader search
        status, messages = mail.search(None, 'FROM "qogita.com" SUBJECT "catalog"')
    if status != "OK" or not messages[0]:
        print("  No Qogita catalog emails found.")
        return None, None

    email_ids = messages[0].split()
    print(f"  Found {len(email_ids)} Qogita catalog email(s)")

    # Process most recent unread, or most recent overall if all read
    target_id = None
    for eid in reversed(email_ids):
        _, flag_data = mail.fetch(eid, "(FLAGS)")
        flags = str(flag_data[0]) if flag_data and flag_data[0] else ""
        if "\\Seen" not in flags:
            target_id = eid
            break
    if target_id is None:
        print("  All catalog emails already processed (marked as read).")
        print("  Send a fresh catalog email from qogita.com to get new data.")
        return None, None

    status, msg_data = mail.fetch(target_id, "(RFC822)")
    if status != "OK":
        return None, None

    msg = email.message_from_bytes(msg_data[0][1])
    print(f"  Processing: {msg.get('Subject', '')} | {msg.get('Date', '')}")

    # Extract the download URL from the email body
    body_text = ""
    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type in ("text/plain", "text/html"):
            try:
                body_text += part.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception:
                pass

    # Find Qogita's catalog download link — they use a redirect URL
    # through their email tracking system (email.t.qogita.com) which
    # eventually redirects to the real S3 file.
    url_patterns = [
        r'https://email\.t\.qogita\.com/e/c/[^\s<>"\']+',
        r'https://email\.m\.qogita\.com/e/c/[^\s<>"\']+',
        r'https://[^\s<>"\']*qogita[^\s<>"\']*\.xlsx[^\s<>"\']*',
        r'https://static\.prod\.qogita\.com/files/downloads/[^\s<>"\']+',
    ]

    download_url = None
    for pattern in url_patterns:
        matches = re.findall(pattern, body_text, re.IGNORECASE)
        if matches:
            download_url = matches[0]
            print(f"  Found download URL: {download_url[:80]}...")
            break

    if not download_url:
        print("  Could not find download URL in email body.")
        print(f"  Body snippet (first 1500 chars):")
        print(body_text[:1500])
        mail.store(target_id, "+FLAGS", "\\Seen")
        return None, None

    # Follow redirects to get the actual file
    print(f"  Downloading catalog file (following redirects)...")
    r = requests.get(download_url, timeout=120, allow_redirects=True)
    r.raise_for_status()
    print(f"  Final URL: {r.url[:100]}")
    print(f"  Downloaded {len(r.content):,} bytes, Content-Type: {r.headers.get('Content-Type')}")

    # Determine filename from URL or Content-Disposition header
    filename = "catalog.xlsx"
    cd = r.headers.get("Content-Disposition", "")
    fn_match = re.search(r'filename[^;=\n]*=(["\']?)([^"\';\n]+)\1', cd)
    if fn_match:
        filename = fn_match.group(2).strip()
    elif ".csv" in download_url.lower():
        filename = "catalog.csv"

    print(f"  Filename: {filename}")

    # Mark as read
    mail.store(target_id, "+FLAGS", "\\Seen")
    return r.content, filename


def parse_excel_catalog(attachment_data, filename):
    try:
        import openpyxl
    except ImportError:
        print("  [!] openpyxl not installed")
        sys.exit(1)

    print(f"  Parsing attachment...")
    wb = openpyxl.load_workbook(io.BytesIO(attachment_data), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    header_idx = None
    for i, row in enumerate(rows):
        if any("GTIN" in str(cell).upper() for cell in row if cell):
            header_idx = i
            break

    if header_idx is None:
        print(f"  [!] Could not find header row")
        print(f"  [!] First 5 rows: {rows[:5]}")
        return []

    headers = [str(h).strip() if h else "" for h in rows[header_idx]]
    col = {h: i for i, h in enumerate(headers)}

    def get(row, key, default=""):
        idx = col.get(key)
        if idx is None:
            return default
        v = row[idx] if idx < len(row) else None
        return str(v).strip() if v is not None else default

    products = []
    for row in rows[header_idx + 1:]:
        if not any(row):
            continue
        gtin = get(row, "GTIN")
        if not gtin or gtin == "None":
            continue

        price = (
            get(row, "GBP Lowest Price inc. shipping") or
            get(row, "£ Lowest Price inc. shipping") or
            get(row, "Lowest Price")
        ).replace("£", "").strip()

        product_link = get(row, "Product Link")
        qid_m = re.search(r"/products/([A-Za-z0-9]+)/", product_link)
        qid = qid_m.group(1) if qid_m else gtin

        products.append({
            "qid":      qid,
            "barcode":  gtin,
            "title":    get(row, "Name"),
            "brand":    get(row, "Brand"),
            "category": get(row, "Category"),
            "price":    price,
            "stock":    get(row, "Total Inventory of All Offers"),
            "url":      product_link or f"https://www.qogita.com/products/{qid}/",
            "image":    get(row, "Image URL"),
        })

    print(f"  Parsed {len(products):,} products")
    return products


def safe_float(val):
    try:
        return float(str(val).replace(",", "").replace("£", ""))
    except (TypeError, ValueError):
        return None


def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def sas_ean_url(barcode, cost_price):
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price)}"
    )


def sas_title_url(title, cost_price):
    from urllib.parse import quote
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={quote(title)}&sas_cost_price={vat_price(cost_price)}"
    )


def _send_embed(embed, content=None):
    payload = {"embeds": [embed]}
    if content:
        payload["content"] = content
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def notify_deep_drop(product, old_price, new_price, pct_change):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct_display = f"{pct_change * 100:.1f}%"
    barcode = product.get("barcode", "")
    title   = product.get("title", "")

    fields = [
        {"name": "💰 Old Price",  "value": f"£{old_price}",     "inline": True},
        {"name": "💰 New Price",  "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Drop",       "value": f"↓ {diff} (**{pct_display}**)", "inline": True},
        {"name": "💷 Inc. VAT",   "value": f"£{vat_price(new_price)}", "inline": True},
        {"name": "🏷️ Brand",      "value": product.get("brand", "-"),    "inline": True},
        {"name": "📂 Category",   "value": product.get("category", "-"), "inline": True},
        {"name": "🔢 GTIN / EAN", "value": f"`{barcode}`" if barcode else "-", "inline": True},
        {"name": "📊 Total Stock","value": product.get("stock", "-"),    "inline": True},
        {"name": "🔍 SAS Title",  "value": f"[Search by title]({sas_title_url(title, new_price)})",    "inline": True},
        {"name": "🔍 SAS EAN",    "value": f"[Search by barcode]({sas_ean_url(barcode, new_price)})",  "inline": True},
    ]

    embed = {
        "title":     f"🚨  DEEP DROP -{pct_display} — {title}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_DEEP_DROP,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Full Catalog Deep Drop Monitor • qogita.com"},
    }
    if product.get("image"):
        embed["thumbnail"] = {"url": product["image"]}

    _send_embed(embed, content=FNF_MENTION)
    print(f"  Discord: DEEP DROP -{pct_display} — {title[:50]}")


def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  [!] Snapshot corrupted ({e}) — starting fresh")
            os.rename(SNAPSHOT_FILE, f"{SNAPSHOT_FILE}.corrupted.{int(time.time())}")
            return {}
    return {}


def save_snapshot(data):
    tmp = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, SNAPSHOT_FILE)


def main():
    print("=" * 55)
    print("  Qogita Full Catalog — Deep Drop Monitor (IMAP)")
    print(f"  Alerting on drops of {MIN_DROP_PCT*100:.0f}%+ across all brands")
    print("=" * 55)

    if not GMAIL_PASSWORD:
        print("  [!] GMAIL_APP_PASSWORD must be set")
        sys.exit(1)
    if not DISCORD_WEBHOOK:
        print("  [!] DISCORD_WEBHOOK_FULLCATALOG_DEEPDROP must be set")
        sys.exit(1)

    print(f"\n  Connecting to Gmail ({GMAIL_ADDRESS})...")
    try:
        mail = connect_gmail()
    except Exception as e:
        print(f"  [!] Gmail connection failed: {e}")
        sys.exit(1)
    print("  Connected.")

    attachment_data, filename = fetch_latest_catalog_attachment(mail)
    mail.logout()

    if attachment_data is None:
        print("  No new catalog email — nothing to process.")
        sys.exit(0)

    products = parse_excel_catalog(attachment_data, filename)
    if not products:
        print("  [!] No products parsed from attachment")
        sys.exit(1)

    snapshot      = load_snapshot()
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    if is_first_run:
        print(f"\n  First run — recording baseline from {len(products):,} products (no alerts)...")
        new_snapshot = {p["qid"]: p["price"] for p in products if p.get("qid")}
        save_snapshot(new_snapshot)
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline complete — {len(new_snapshot):,} prices recorded. No alerts sent.")
        return

    print(f"\n  Comparing {len(products):,} products against baseline...")
    alerts_fired = 0
    new_snapshot = {}

    for product in products:
        qid = product.get("qid")
        if not qid:
            continue
        new_price = product.get("price", "")
        new_snapshot[qid] = new_price
        old_price = snapshot.get(qid)
        if old_price is None:
            continue
        old_f = safe_float(old_price)
        new_f = safe_float(new_price)
        if not old_f or not new_f or old_f <= 0:
            continue
        pct_change = (old_f - new_f) / old_f
        abs_change = old_f - new_f
        if pct_change >= MIN_DROP_PCT and abs_change >= MIN_ABS_DROP:
            notify_deep_drop(product, old_price, new_price, pct_change)
            alerts_fired += 1
            time.sleep(1)

    save_snapshot(new_snapshot)
    print(f"\n  Done — {len(new_snapshot):,} prices tracked, {alerts_fired} deep-drop alert(s) fired")


if __name__ == "__main__":
    main()
