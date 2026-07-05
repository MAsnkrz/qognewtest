"""
Vitabiotics Brand Monitor — Webhook-driven version.

This script is invoked by a GitHub Actions workflow that triggers on
`repository_dispatch` (event_type: qogita_catalog_vitabiotics), fired by a
Cloudflare Worker when Qogita's async catalog-download job (filtered
by brand_names=["Vitabiotics"]) completes.

It does NOT call the Qogita API directly for the catalog — the CSV
download_url is passed in via the QOGITA_DOWNLOAD_URL env var (set
from the repository_dispatch client_payload by the workflow).

This replaces the older Playwright + per-product API hybrid approach:
no browser needed, no per-product API looping — just one CSV per run,
delivered async via webhook.

CSV columns (confirmed from a real brand-filtered download):
  GTIN, Name, Category, Brand, £ Lowest Price inc. shipping, Unit,
  Lowest Priced Offer Inventory, Is a pre-order?,
  Estimated Delivery Time (weeks), Number of Offers,
  Total Inventory of All Offers, Product Link, Image URL

Detects (Discord alerts fire ONLY for these):
  - New product listings (in stock only)
  - Price drops (decreased >1% and >£0.02)
  - Back in stock (was OOS, now available)

Does NOT alert on: price increases, stock fluctuations, going OOS.

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

BRAND_DISPLAY_NAME = "Vitabiotics"
SNAPSHOT_FILE    = "snapshot_qogita_vitabiotics_brand.json"
BASELINE_FLAG    = "baseline_done_vitabiotics_brand.txt"

DOWNLOAD_URL     = os.getenv("QOGITA_DOWNLOAD_URL", "")
DOWNLOAD_STATUS  = os.getenv("QOGITA_DOWNLOAD_STATUS", "completed")
ERROR_MESSAGE    = os.getenv("QOGITA_ERROR_MESSAGE", "")
DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK_VITABIOTICS", "")

COLOUR_NEW     = 0xE91E8C
COLOUR_BACK    = 0x9B59B6
COLOUR_FAILED  = 0x95A5A6

# Discord role to ping for exceptional (25%+) price drops
FNF_ROLE_ID = "1019772687528235099"
FNF_MENTION = f"<@&{FNF_ROLE_ID}>"

# ---------------------------------------------------------------------------
# CSV FETCH + PARSE
# ---------------------------------------------------------------------------

def fetch_csv(url):
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    print(f"  Fetched {len(r.content):,} bytes, Content-Type: {r.headers.get('Content-Type')}")
    # Use utf-8-sig to transparently strip a leading BOM if present —
    # CSV exports from many backends include one, which would otherwise
    # make the header row look like '﻿GTIN' instead of 'GTIN' and
    # silently break header detection below.
    text = r.content.decode("utf-8-sig", errors="replace")
    print(f"  First 200 chars of decoded CSV: {text[:200]!r}")
    return text


def _safe_int(val):
    try:
        return int(float(str(val).replace(",", "")))
    except (TypeError, ValueError):
        return None


def parse_catalog_csv(csv_text):
    """
    The file has a few metadata rows before the real header row
    ('Filters - Category: ...' etc). Find the row that starts with
    'GTIN' and treat that as the header.
    """
    lines = csv_text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        # Real header lines look like '"GTIN","Name",...' — quoted CSV,
        # so a strict startswith("GTIN") check (even after stripping
        # BOM/whitespace) never matches, since the actual first char is
        # a literal quote. Checking for GTIN anywhere near the start of
        # the line sidesteps quoting/BOM edge cases entirely.
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
        cheapest_stock = _safe_int(row.get("Lowest Priced Offer Inventory", ""))
        num_offers = _safe_int(row.get("Number of Offers", "")) or 0

        products.append({
            "qid":            qid,
            "title":          row.get("Name", "") or "",
            "category":       row.get("Category", "") or "",
            "brand":          row.get("Brand", "") or "",
            "url":            product_link or f"https://www.qogita.com/products/{qid}/",
            "image":          row.get("Image URL", "") or "",
            "barcode":        gtin,
            "price":          (row.get("£ Lowest Price inc. shipping", "") or "").strip(),
            "bundle_size":    (row.get("Unit", "") or "").strip(),
            "cheapest_stock": cheapest_stock,
            "stock":          total_stock,
            "all_offers":     num_offers,
            "is_preorder":    (row.get("Is a pre-order?", "") or "").strip().lower() == "yes",
            "delivery_weeks": (row.get("Estimated Delivery Time (weeks)", "") or "").strip(),
            "in_stock":       (total_stock or 0) > 0,
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


def selleramp_url_ean(barcode, cost_price_str):
    """SAS lookup by EAN/GTIN barcode."""
    if not barcode:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price_str)}"
    )


def selleramp_url_title(title, cost_price_str):
    """SAS lookup by product title (URL-encoded)."""
    if not title:
        return None
    from urllib.parse import quote as _quote
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={_quote(title)}&sas_cost_price={vat_price(cost_price_str)}"
    )

# ---------------------------------------------------------------------------
# DISCORD EMBEDS
# ---------------------------------------------------------------------------

def _base_fields(product):
    barcode        = product.get("barcode", "")
    title          = product.get("title", "")
    stock          = product.get("stock")
    cheapest_stock = product.get("cheapest_stock")
    in_stock       = product.get("in_stock", True)
    all_offers     = product.get("all_offers", 0)
    brand          = product.get("brand", "")
    category       = product.get("category", "")
    price          = product.get("price", "")
    sas_ean   = selleramp_url_ean(barcode, price)
    sas_title = selleramp_url_title(title, price)

    if stock is not None:
        stock_val = f"**{stock:,} units**"
    elif in_stock:
        stock_val = "✅ In stock"
    else:
        stock_val = "❌ Out of stock"

    fields = [
        {"name": "🏷️ Brand",        "value": brand if brand else "-",            "inline": True},
        {"name": "📂 Category",     "value": category if category else "-",      "inline": True},
        {"name": "🔢 GTIN / EAN",   "value": f"`{barcode}`" if barcode else "-",  "inline": True},
        {"name": "📊 Total Stock",   "value": stock_val,                           "inline": True},
        {"name": "📊 Cheapest Offer Stock", "value": f"{cheapest_stock:,} units" if cheapest_stock is not None else "-", "inline": True},
        {"name": "🏭 Sellers",       "value": f"{all_offers}" if all_offers else "-", "inline": True},
    ]
    if sas_title:
        fields.append({"name": "🔍 SAS Title", "value": f"[Search by title]({sas_title})", "inline": True})
    if sas_ean:
        fields.append({"name": "🔍 SAS EAN", "value": f"[Search by barcode]({sas_ean})", "inline": True})
    return fields


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


def _thumbnail(product):
    image = product.get("image", "")
    return {"url": image} if image else None


def notify_new(product):
    price = product.get("price", "")
    fields = [
        {"name": "💰 Price (incl. shipping)", "value": f"**£{price}**" if price else "-", "inline": True},
        {"name": "💷 Price (inc. VAT)",        "value": f"£{vat_price(price)}" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🆕  NEW LISTING — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Qogita {BRAND_DISPLAY_NAME} Brand Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: NEW — {product.get('title', '')[:60]}")


def notify_price_change(product, old_price, new_price, pct_change):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct_display = f"{pct_change * 100:.1f}%"

    if pct_change >= 0.20:
        colour, tier = 0x00C853, "🔥"
    elif pct_change >= 0.10:
        colour, tier = 0x2ECC71, "💰"
    else:
        colour, tier = 0x82E0AA, "💵"

    fields = [
        {"name": "💰 Old Price", "value": f"£{old_price}",     "inline": True},
        {"name": "💰 New Price", "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Drop",      "value": f"↓ {diff} (**{pct_display}**)", "inline": True},
        {"name": "💷 New Price (inc. VAT)", "value": f"£{vat_price(new_price)}" if new_price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{tier}  PRICE DROP -{pct_display} — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Qogita {BRAND_DISPLAY_NAME} Brand Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    # Ping @fnf role for exceptional drops (25%+)
    mention = FNF_MENTION if pct_change >= 0.25 else None
    _send_embed(embed, content=mention)
    print(f"  Discord: PRICE DROP -{pct_display} — {product.get('title', '')[:50]}")


def notify_back_in_stock(product):
    price = product.get("price", "")
    fields = [
        {"name": "💰 Price (incl. shipping)", "value": f"**£{price}**" if price else "-", "inline": True},
        {"name": "💷 Price (inc. VAT)",        "value": f"£{vat_price(price)}" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  BACK IN STOCK — {product.get('title', '')}",
        "url":       product.get("url", "https://www.qogita.com"),
        "color":     COLOUR_BACK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Qogita {BRAND_DISPLAY_NAME} Brand Monitor • qogita.com"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: BACK IN STOCK — {product.get('title', '')[:60]}")


def notify_download_failed():
    embed = {
        "title":     f"⚠️  Catalog download failed — {BRAND_DISPLAY_NAME}",
        "color":     COLOUR_FAILED,
        "description": ERROR_MESSAGE or "Qogita reported a catalog generation failure.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Qogita {BRAND_DISPLAY_NAME} Brand Monitor • qogita.com"},
    }
    _send_embed(embed)

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
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


def save_snapshot(data):
    tmp_file = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f)
    os.replace(tmp_file, SNAPSHOT_FILE)


def snapshot_entry(product):
    return {
        "title":      product.get("title", ""),
        "url":        product.get("url", ""),
        "image":      product.get("image", ""),
        "barcode":    product.get("barcode", ""),
        "brand":      product.get("brand", ""),
        "category":   product.get("category", ""),
        "price":      product.get("price", ""),
        "stock":      product.get("stock"),
        "in_stock":           product.get("in_stock", True),
        "last_alerted_price": product.get("last_alerted_price", ""),
        "first_seen":         product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    old_price    = old.get("price", "")
    new_price    = product.get("price", "")
    was_in_stock = old.get("in_stock", True)
    now_in_stock = product.get("in_stock", True)

    for key in ("barcode", "brand", "category", "image"):
        if not product.get(key):
            product[key] = old.get(key, "")

    old_f = safe_float(old_price)
    new_f = safe_float(new_price)

    if not was_in_stock and now_in_stock:
        notify_back_in_stock(product)
        time.sleep(1)
        return

    if old_f and new_f and old_f > 0:
        pct_change = (old_f - new_f) / old_f
        abs_change = old_f - new_f
        if pct_change > 0.05 and abs_change > 0.05:  # 5%+ AND £0.05+ absolute
            # Guard against re-alerting the same drop twice when two runs
            # overlap before the first run's snapshot commit lands in git.
            last_alerted = old.get("last_alerted_price", "")
            if last_alerted and abs(safe_float(last_alerted) or 0 - (new_f or 0)) < 0.01:
                pass  # same price already alerted — skip
            else:
                product["last_alerted_price"] = new_price
                notify_price_change(product, old_price, new_price, pct_change)
                time.sleep(1)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("=" * 55)
    print(f"  Qogita {BRAND_DISPLAY_NAME} Brand Monitor (webhook-driven)")
    print("=" * 55)

    if not DISCORD_WEBHOOK:
        print("  [!] DISCORD_WEBHOOK_VITABIOTICS must be set")
        sys.exit(1)

    if DOWNLOAD_STATUS == "failed":
        print(f"  Catalog generation failed: {ERROR_MESSAGE}")
        notify_download_failed()
        sys.exit(0)

    if not DOWNLOAD_URL:
        print("  [!] QOGITA_DOWNLOAD_URL must be set when status is 'completed'")
        sys.exit(1)

    print(f"  Downloading catalog CSV...")
    csv_text = fetch_csv(DOWNLOAD_URL)
    products = parse_catalog_csv(csv_text)
    print(f"  Parsed {len(products)} products")

    if not products:
        print("  [!] No products parsed — possible format change, aborting")
        sys.exit(1)

    snapshot      = load_snapshot()
    known_qids    = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    current_qids = {p["qid"] for p in products}
    new_qids     = current_qids - known_qids

    if is_first_run:
        print(f"  First run — building baseline from {len(products)} products (no alerts)...")
    else:
        print(f"  {len(new_qids)} new products out of {len(products)} total")

    for product in products:
        qid = product["qid"]

        if is_first_run:
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[qid] = entry
        elif qid in new_qids:
            if product.get("in_stock", True):
                print(f"  -> NEW: {product['title'][:60]}")
                notify_new(product)
                time.sleep(1.5)
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[qid] = entry
        else:
            old = snapshot[qid]
            check_changes(product, old)
            entry = snapshot_entry(product)
            entry["first_seen"] = old.get("first_seen", entry["first_seen"])
            snapshot[qid] = entry

    save_snapshot(snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline complete — {len(snapshot)} products. No alerts sent.")
    else:
        print(f"  Done — {len(snapshot)} products tracked")


if __name__ == "__main__":
    main()
