"""
danabooks.py — Section 6: Dana Books → Airtable Cost Sync (French Inventories)
Split out of the original combined app.py. Code below is unchanged from the
merged version; only the file location and these import lines changed.
"""
import os
import time
import threading
from flask import request, jsonify
import requests
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from shared import app
from amazon_sync import AIRTABLE_TOKEN
from abandoned_cart import AIRTABLE_BASE_ID

# NOTE: reuses AIRTABLE_BASE_ID already defined in Section 4 instead of
# redeclaring it. Reuses AIRTABLE_TOKEN as a fallback if AIRTABLE_API_KEY
# isn't set separately for this service.

DANABOOKS_URL        = "https://transactionhub.zerobook.shop/api/v1/transaction-history"
DANABOOKS_TOKEN      = os.environ.get("DANABOOKS_TOKEN", "")
DANABOOKS_IDENTIFIER = os.environ.get("DANABOOKS_IDENTIFIER", "thirdparty@danabooks.com")

AIRTABLE_API_KEY     = os.environ.get("AIRTABLE_API_KEY") or AIRTABLE_TOKEN
DANABOOKS_TABLE_NAME = "French Inventories"

DANABOOKS_IST = pytz.timezone("Asia/Kolkata")
# NOTE: kept separate from Section 5's `IST` (zoneinfo.ZoneInfo) — same
# timezone, different library object, so it's not safe to just reuse that name.

# Number of SKUs per Dana Books batch request
DANABOOKS_BATCH_SIZE  = 500
# Delay between batch requests (seconds)
DANABOOKS_BATCH_DELAY = 1.0
# Max retries on 429/520
DANABOOKS_MAX_RETRIES = 3
# Wait time before retry (seconds)
DANABOOKS_RETRY_WAIT  = 10

# Thread-safe counters
_danabooks_progress_lock = threading.Lock()


# ── Dana Books helpers (Section 6) ─────────────────────────────────────────────

def get_prices_for_batch(skus):
    """
    Fetch latest purchase prices from Dana Books for a batch of SKUs.
    Returns dict: { sku: float_price_or_None, ... }
    """
    headers = {
        "Authorization": f"Bearer {DANABOOKS_TOKEN}",
        "Identifier": DANABOOKS_IDENTIFIER,
        "Content-Type": "application/json"
    }
    payload = {
        "itemsku": skus,
        "opcode": "PUR"
    }

    for attempt in range(1, DANABOOKS_MAX_RETRIES + 1):
        resp = requests.post(DANABOOKS_URL, json=payload, headers=headers, timeout=30)

        if resp.status_code == 429:
            if attempt < DANABOOKS_MAX_RETRIES:
                print(f"[dana] 429 Rate limit, waiting {DANABOOKS_RETRY_WAIT}s (attempt {attempt}/{DANABOOKS_MAX_RETRIES})", flush=True)
                time.sleep(DANABOOKS_RETRY_WAIT)
                continue
            else:
                resp.raise_for_status()

        if resp.status_code == 520:
            if attempt < DANABOOKS_MAX_RETRIES:
                print(f"[dana] 520 Cloudflare error, waiting {DANABOOKS_RETRY_WAIT}s (attempt {attempt}/{DANABOOKS_MAX_RETRIES})", flush=True)
                time.sleep(DANABOOKS_RETRY_WAIT)
                continue
            else:
                print(f"[dana] 520 Cloudflare error after {DANABOOKS_MAX_RETRIES} attempts, skipping batch", flush=True)
                return {sku: None for sku in skus}

        resp.raise_for_status()
        data = resp.json()

        # Response: { "data": { "SKU1": [...records], "SKU2": [...records] } }
        sku_data = data.get("data", {})
        if not isinstance(sku_data, dict):
            return {sku: None for sku in skus}

        result = {}
        for sku in skus:
            records = sku_data.get(sku, [])
            if not records or not isinstance(records, list):
                result[sku] = None
                continue

            # Records are returned newest first — take the first one
            first_record = records[0]
            if not isinstance(first_record, dict):
                result[sku] = None
                continue

            price = first_record.get("item_price")
            if price is None:
                result[sku] = None
                continue

            try:
                result[sku] = float(price)
            except (ValueError, TypeError):
                print(f"[dana] WARNING: non-numeric item_price for {sku}: {price!r}", flush=True)
                result[sku] = None

        return result

    return {sku: None for sku in skus}


# ── Airtable helpers (Section 6)
# NOTE: renamed with danabooks_ prefix to avoid conflict with helpers of the
# same purpose already defined in Sections 2/3/4 ────────────────────────────

def danabooks_get_all_airtable_skus():
    """
    Fetch ALL records from French Inventories that have a SKU.
    Returns list of dicts: [{"record_id": ..., "sku": ..., "current_cost": ...}, ...]
    Retries each page up to 3 times on timeout/connection errors.
    """
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(DANABOOKS_TABLE_NAME)}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    params = {
        "filterByFormula": "{SKU}!=''",
        "fields[]": ["SKU", "Cost"],
        "pageSize": 100
    }

    records = []
    offset = None

    while True:
        if offset:
            params["offset"] = offset

        resp = None
        for attempt in range(1, 4):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                print(f"[airtable-fetch] Attempt {attempt}/3 failed: {e}", flush=True)
                if attempt < 3:
                    time.sleep(5)
                else:
                    raise

        data = resp.json()

        for rec in data.get("records", []):
            sku = rec.get("fields", {}).get("SKU")
            if sku:
                current_cost = rec.get("fields", {}).get("Cost")
                if current_cost is not None:
                    try:
                        current_cost = float(current_cost)
                    except (ValueError, TypeError):
                        current_cost = None
                records.append({
                    "record_id": rec["id"],
                    "sku": sku,
                    "current_cost": current_cost
                })

        offset = data.get("offset")
        if not offset:
            break

    return records


def danabooks_update_airtable_cost(record_id, cost):
    """Update the Cost field in Airtable for a given record ID."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(DANABOOKS_TABLE_NAME)}/{record_id}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"fields": {"Cost": cost}}
    resp = requests.patch(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Core sync job (Section 6) ──────────────────────────────────────────────────

def run_danabooks_auto_sync():
    # NOTE: renamed from run_auto_sync to run_danabooks_auto_sync to avoid
    # ambiguity alongside the other background sync jobs defined earlier.
    """
    Scheduled job:
    1. Fetch ALL SKUs from Airtable French Inventories
    2. For each batch of DANABOOKS_BATCH_SIZE SKUs, query Dana Books for latest purchase prices
    3. Only update Airtable if Cost is empty OR Dana Books price has changed
    """
    try:
        _run_danabooks_auto_sync_inner()
    except Exception as e:
        import traceback
        print(f"[auto-sync] FATAL UNCAUGHT ERROR: {e}", flush=True)
        print(traceback.format_exc(), flush=True)


def _run_danabooks_auto_sync_inner():
    print("[auto-sync] Starting scheduled cost sync...", flush=True)

    if not DANABOOKS_TOKEN:
        print("[auto-sync] ERROR: DANABOOKS_TOKEN is not set. Aborting.", flush=True)
        return

    try:
        all_skus = danabooks_get_all_airtable_skus()
    except Exception as e:
        print(f"[auto-sync] ERROR fetching Airtable SKUs: {e}", flush=True)
        return

    total = len(all_skus)
    print(f"[auto-sync] Total SKUs to check: {total}", flush=True)
    print(f"[auto-sync] Batch size: {DANABOOKS_BATCH_SIZE} SKUs per request", flush=True)

    updated = 0
    skipped_no_purchase = 0
    skipped_no_change = 0
    errors = 0
    done = 0

    # Build a lookup dict for quick access: sku -> item
    sku_map = {item["sku"]: item for item in all_skus}

    # Process in batches
    sku_list = list(sku_map.keys())
    for i in range(0, len(sku_list), DANABOOKS_BATCH_SIZE):
        batch_skus = sku_list[i:i + DANABOOKS_BATCH_SIZE]

        try:
            prices = get_prices_for_batch(batch_skus)
        except Exception as e:
            print(f"[auto-sync] ERROR fetching batch {i//DANABOOKS_BATCH_SIZE + 1}: {type(e).__name__}: {e}", flush=True)
            errors += len(batch_skus)
            done += len(batch_skus)
            continue

        for sku in batch_skus:
            item = sku_map[sku]
            record_id = item["record_id"]
            current_cost = item["current_cost"]
            dana_price = prices.get(sku)

            try:
                if dana_price is None:
                    skipped_no_purchase += 1
                elif current_cost is not None and current_cost == dana_price:
                    skipped_no_change += 1
                else:
                    danabooks_update_airtable_cost(record_id, dana_price)
                    print(
                        f"[auto-sync] UPDATED {sku} | "
                        f"old={current_cost if current_cost is not None else 'empty'} → new={dana_price}",
                        flush=True
                    )
                    updated += 1
            except Exception as e:
                print(f"[auto-sync] ERROR updating {sku}: {type(e).__name__}: {e}", flush=True)
                errors += 1

            done += 1

        if done % 500 == 0 or done == total:
            print(f"[auto-sync] Progress: {done}/{total} checked...", flush=True)

        # Delay between batch requests
        time.sleep(DANABOOKS_BATCH_DELAY)

    print(
        f"[auto-sync] Done. Updated={updated} | "
        f"No purchase in Dana Books={skipped_no_purchase} | "
        f"Price unchanged={skipped_no_change} | "
        f"Errors={errors}",
        flush=True
    )


# ── Scheduler — 9 AM, 2 PM, 8 PM IST (Section 6) ───────────────────────────────

danabooks_scheduler = BackgroundScheduler(timezone=DANABOOKS_IST)

danabooks_scheduler.add_job(
    run_danabooks_auto_sync,
    trigger=CronTrigger(hour=9, minute=0, timezone=DANABOOKS_IST),
    id="danabooks_sync_9am",
    name="Cost sync 9 AM IST"
)
danabooks_scheduler.add_job(
    run_danabooks_auto_sync,
    trigger=CronTrigger(hour=14, minute=0, timezone=DANABOOKS_IST),
    id="danabooks_sync_2pm",
    name="Cost sync 2 PM IST"
)
danabooks_scheduler.add_job(
    run_danabooks_auto_sync,
    trigger=CronTrigger(hour=20, minute=0, timezone=DANABOOKS_IST),
    id="danabooks_sync_8pm",
    name="Cost sync 8 PM IST"
)

danabooks_scheduler.start()
print("[scheduler] Dana Books cost sync scheduled at 9 AM, 2 PM, 8 PM IST", flush=True)


# ── Routes (Section 6) ─────────────────────────────────────────────────────────

@app.route("/sync-cost", methods=["POST"])
def sync_cost_manual():
    """
    Manually trigger cost sync for one or more specific SKUs.
    Body: { "skus": ["DNG1024", "DNG1025"] }
    Or single: { "sku": "DNG1024" }
    """
    body = request.get_json(force=True) or {}

    if "skus" in body:
        skus = body["skus"]
    elif "sku" in body:
        skus = [body["sku"]]
    else:
        return jsonify({"error": "Provide 'sku' or 'skus' in request body"}), 400

    results = []
    try:
        prices = get_prices_for_batch(skus)
    except Exception as e:
        return jsonify({"error": f"Dana Books API error: {type(e).__name__}: {e}"}), 500

    for sku in skus:
        result = {"sku": sku}
        try:
            dana_price = prices.get(sku)
            if dana_price is None:
                result["status"] = "skipped"
                result["reason"] = "No purchase records found in Dana Books"
                results.append(result)
                continue

            result["dana_price"] = dana_price

            url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(DANABOOKS_TABLE_NAME)}"
            headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
            params = {
                "filterByFormula": f"{{SKU}}='{sku}'",
                "maxRecords": 1,
                "fields[]": ["SKU", "Cost"]
            }
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            records = resp.json().get("records", [])

            if not records:
                result["status"] = "skipped"
                result["reason"] = "SKU not found in Airtable French Inventories"
                results.append(result)
                continue

            record_id = records[0]["id"]
            current_cost = records[0].get("fields", {}).get("Cost")

            if current_cost is not None:
                try:
                    current_cost = float(current_cost)
                except (ValueError, TypeError):
                    current_cost = None

            result["previous_cost"] = current_cost

            if current_cost is not None and current_cost == dana_price:
                result["status"] = "skipped"
                result["reason"] = "Price unchanged"
                results.append(result)
                continue

            danabooks_update_airtable_cost(record_id, dana_price)
            result["status"] = "updated"
            result["new_cost"] = dana_price

        except Exception as e:
            result["status"] = "error"
            result["reason"] = f"{type(e).__name__}: {e}"

        results.append(result)
        print(f"[manual-sync] {result}", flush=True)

    return jsonify({"results": results}), 200


@app.route("/danabooks/sync-all", methods=["POST"])
# NOTE: renamed from /sync-all to /danabooks/sync-all to avoid conflict with Section 2's /sync-all
def danabooks_sync_all_now():
    """Manually trigger the full Dana Books auto sync job immediately."""
    threading.Thread(target=run_danabooks_auto_sync, daemon=True).start()
    return jsonify({"message": "Full Dana Books sync started in background"}), 200


@app.route("/danabooks/health", methods=["GET"])
# NOTE: renamed from /health to /danabooks/health to avoid conflict with Section 4
def danabooks_health():
    jobs = [
        {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
        for j in danabooks_scheduler.get_jobs()
    ]
    return jsonify({"status": "ok", "service": "danabooks-airtable-sync", "scheduled_jobs": jobs}), 200