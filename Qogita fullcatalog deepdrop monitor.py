"""
Qogita Full Catalog — Deep Price Drop Monitor (webhook-driven)

Monitors the ENTIRE Health & Beauty category (every brand, every
sub-category — Makeup, Health, Fragrance, Hair, Body, etc.) via the
same async catalog-download + Cloudflare Worker webhook pipeline used
by the Makeup/Health/brand monitors.

UNLIKE the brand monitors, this one ONLY alerts on very steep price
drops — 50% to 100% off the previous price. No new-listing alerts, no
back-in-stock alerts, no small price-drop alerts. The goal is to
surface only the rare, extreme deals worth acting on across the whole
category, not everyday small fluctuations.

This script is invoked by a GitHub Actions workflow that triggers on
`repository_dispatch` (event_type: qogita_catalog_full_deepdrop),
fired by the Cloudflare Worker when Qogita's async catalog-download
job (filtered to every Health & Beauty sub-category) completes.

It does NOT call the Qogita API directly for the catalog — the CSV
download_url is passed in via the QOGITA_DOWNLOAD_URL env var (set
from the repository_dispatch client_payload by the workflow).

Given the category can be very large, the snapshot stores ONLY the
minimal data needed for next time (qid -> price), not full product
metadata, to keep the snapshot file a manageable size. Full details
(title, image, stock, brand) are read fresh from the CSV every run
and only attached to the Discord embed at the moment an alert fires.

CSV columns (confirmed from real category-filtered downloads):
  GTIN, Name, Category, Brand, £ Lowest Price inc. shipping, Unit,
  Lowest Priced Offer Inventory, Is a pre-order?,
  Estimated Delivery Time (weeks), Number of Offers,
  Total Inventory of All Offers, Product Link, Image URL

Deps: pip install requests
"""

import csv
import io
import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SNAPSHOT_FILE   = "snapshot_qogita_fullcatalog_deepdrop_prices.json"
BASELINE_FLAG   = "baseline_done_fullcatalog_deepdrop.txt"

DOWNLOAD_URL    = os.getenv("QOGITA_DOWNLOAD_URL", "")
DOWNLOAD_STATUS = os.getenv("QOGITA_DOWNLOAD_STATUS", "completed")
ERROR_MESSAGE   = os.getenv("QOGITA_ERROR_MESSAGE", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_FULLCATALOG_DEEPDROP", "")

# Only alert on drops within this range (fractions: 0.50 = 50%, 1.00 = 100%)
MIN_DROP_PCT = 0.50
MAX_DROP_PCT = 1.00

# Minimum absolute price difference required, to avoid noise on
# extremely cheap items where a "50% drop" might just be a few pence
MIN_ABS_DROP = 0.05

COLOUR_DEEP_DROP = 0xFF0066   # hot pink/red — rare, extreme deals
COLOUR_FAILED    = 0x95A5A6

# ---------------------------------------------------------------------------
# CSV FETCH + PARSE
# ---------------------------------------------------------------------------

def fetch_csv(url):
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    print(f"  Fetched {len(r.content):,} bytes, Content-Type: {r.headers.get('Content-Type')}")
    text = r.content.decode("utf-8-sig", errors="replace")
    print(f"  First 200 chars of decoded CSV: {text[:200]!r}")
    return text


def _safe_int(val):
    try:
        return int(float(str(val).replace(",", "")))
    except (TypeError, ValueError):
        return None


def parse_catalog_csv(csv_text):
    lines = csv_text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if "GTIN" in line[:10].upper():
            header_idx = i
            break
    if header_idx is None:
        print(f"  [!] Could not find a header row containing GTIN in {len(lines)} lines")
        print(f"  [!] First 5 raw lines for inspection:")
        for l in lines[:5]:
            print(f"      {l!r}")
        return []

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    products = []
    for row in reader:
        gtin = (row.get("GTIN", "") or "").strip()
        if not gtin:
            continue

        product_link = row.get("Product Link", "") or ""
        qid_m = re.search(r"/products/([A-Za-z0-9]+)/", product_link)
        qid = qid_m.group(1) if qid_m else gtin

        total_stock = _safe_int(row.get("Total Inventory of All Offers", ""))
        num_offers = _safe_int(row.get("Number of Offers", "")) or 0

        products.append({
            "qid":        qid,
            "title":      row.get("Name", "") or "",
            "category":   row.get("Category", "") or "",
            "brand":      row.get("Brand", "") or "",
            "url":        product_link or f"https://www.qogita.com/products/{qid}/",
            "image":      row.get("Image URL", "") or "",
            "barcode":    gtin,
            "price":      (row.get("£ Lowest Price inc. shipping", "") or "").strip(),
            "stock":      total_stock,
            "all_offers": num_offers,
        })
    return products

# ---------------------------------------------------------------------------
# PRICING HELPERS
# ---------------------------------------------------------------------------

def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def safe_float(val):
    try:
        return float(str(val).replace(",", ""))
    except (TypeError, ValueError):
        return None


def selleramp_url(barcode, cost_price_str):
    if not barcode:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price_str)}"
    )

# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def _send_embed(embed):
    payload = {"embeds": [embed]}
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


def notify_deep_price_drop(product, old_price, new_price, pct_change):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct_display = f"{pct_change * 100:.1f}%"

    barcode = product.get("barcode", "")
    sas_url = selleramp_url(barcode, new_price)

    fields = [
        {"name": "💰 Old Price", "value": f"£{old_price}",     "inline": True},
        {"name": "💰 New Price", "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Drop",      "value": f"↓ {diff} (**{pct_display}**)", "inline": True},
        {"name": "💷 New Price (inc. VAT)", "value": f"£{vat_price(new_price)}" if new_price else "-", "inline": True},
        {"name": "🏷️ Brand",     "value": product.get("brand", "") or "-",    "inline": True},
        {"name": "📂 Category",  "value": product.get("category", "") or "-", "inline": True},
        {"name": "🔢 GTIN / EAN", "value": f"`{barcode}`" if barcode else "-", "inline": True},
        {"name": "📊 Total Stock", "value": f"{product.get('stock'):,} units" if product.get("stock") is not None else "-", "inline": True},
        {"name": "🏭 Sellers",    "value": f"{product.get('all_offers', 0)}", "inline": True},
    ]
    if sas_url:
        fields.append({"name": "🔍 SellerAmp SAS", "value": f"[Open in SellerAmp]({sas_url})", "inline": False})

    embed = {
        "title":     f"🚨  DEEP PRICE DROP -{pct_display} — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_DEEP_DROP,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Qogita Full Catalog Deep Drop Monitor • qogita.com"},
    }
    image = product.get("image", "")
    if image:
        embed["thumbnail"] = {"url": image}

    _send_embed(embed)
    print(f"  Discord: DEEP DROP -{pct_display} — {product.get('title', '')[:50]}")


def notify_download_failed():
    embed = {
        "title":       "⚠️  Catalog download failed — Full Catalog (Deep Drop)",
        "color":       COLOUR_FAILED,
        "description": ERROR_MESSAGE or "Qogita reported a catalog generation failure.",
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "Qogita Full Catalog Deep Drop Monitor • qogita.com"},
    }
    _send_embed(embed)

# ---------------------------------------------------------------------------
# SNAPSHOT — minimal (qid -> price only) to keep the file small at scale
# ---------------------------------------------------------------------------

def load_price_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  [!] Snapshot corrupted ({e}) — backing up and starting fresh")
            try:
                os.rename(SNAPSHOT_FILE, f"{SNAPSHOT_FILE}.corrupted.{int(time.time())}")
            except OSError:
                pass
            return {}
    return {}


def save_price_snapshot(data):
    tmp_file = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f)
    os.replace(tmp_file, SNAPSHOT_FILE)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("=" * 55)
    print("  Qogita Full Catalog — Deep Price Drop Monitor")
    print(f"  Scope: ENTIRE Qogita catalogue — all categories, all brands")
    print(f"  Alerting only on drops of {MIN_DROP_PCT*100:.0f}%-{MAX_DROP_PCT*100:.0f}%")
    print("=" * 55)

    if not DISCORD_WEBHOOK:
        print("  [!] DISCORD_WEBHOOK_HB_DEEPDROP must be set")
        sys.exit(1)

    if DOWNLOAD_STATUS == "failed":
        print(f"  Catalog generation failed: {ERROR_MESSAGE}")
        notify_download_failed()
        sys.exit(0)

    if not DOWNLOAD_URL:
        print("  [!] QOGITA_DOWNLOAD_URL must be set when status is 'completed'")
        sys.exit(1)

    print("  Downloading catalog CSV...")
    csv_text = fetch_csv(DOWNLOAD_URL)
    products = parse_catalog_csv(csv_text)
    print(f"  Parsed {len(products)} products")

    if not products:
        print("  [!] No products parsed — possible format change, aborting")
        sys.exit(1)

    price_snapshot = load_price_snapshot()
    baseline_done  = os.path.exists(BASELINE_FLAG)
    is_first_run   = not baseline_done

    if is_first_run:
        print(f"  First run — recording baseline prices for {len(products)} products (no alerts)...")
        new_snapshot = {p["qid"]: p["price"] for p in products if p.get("qid")}
        save_price_snapshot(new_snapshot)
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline complete — {len(new_snapshot)} prices recorded. No alerts sent.")
        return

    alerts_fired = 0
    new_snapshot = {}

    for product in products:
        qid = product.get("qid")
        if not qid:
            continue

        new_price = product.get("price", "")
        new_snapshot[qid] = new_price

        old_price = price_snapshot.get(qid)
        if old_price is None:
            continue  # genuinely new product — no alert, just record it

        old_f = safe_float(old_price)
        new_f = safe_float(new_price)
        if not old_f or not new_f or old_f <= 0:
            continue

        pct_change = (old_f - new_f) / old_f
        abs_change = old_f - new_f

        if MIN_DROP_PCT <= pct_change <= MAX_DROP_PCT and abs_change >= MIN_ABS_DROP:
            print(f"  -> DEEP DROP: {product['title'][:50]} £{old_price} -> £{new_price} (-{pct_change*100:.1f}%)")
            notify_deep_price_drop(product, old_price, new_price, pct_change)
            alerts_fired += 1
            time.sleep(1)

    save_price_snapshot(new_snapshot)
    print(f"  Done — {len(new_snapshot)} prices tracked, {alerts_fired} deep-drop alert(s) fired")


if __name__ == "__main__":
    main()
