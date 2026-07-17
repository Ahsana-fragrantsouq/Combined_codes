"""
abandoned_cart.py — Section 4: Shopify Abandoned Cart → Airtable
Split out of the original combined app.py. Code below is unchanged from the
merged version; only the file location and these import lines changed.
"""
import os
import hmac
import hashlib
import base64
import time
import threading
from datetime import datetime
from flask import request, jsonify
import requests

from shared import app
from delivery_tracker import SHOPIFY_STORE
from amazon_sync import AIRTABLE_TOKEN, BASE_ID


AIRTABLE_BASE_ID    = os.environ.get("AIRTABLE_BASE_ID", BASE_ID)
SHOPIFY_SECRET      = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
SHOPIFY_ADMIN_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")

AT_BASE = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
AT_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type":  "application/json",
}
SHOPIFY_HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_ADMIN_TOKEN,
    "Content-Type": "application/json",
}

TABLE_CUSTOMERS   = "Customers"
TABLE_INVENTORIES = "French Inventories"
TABLE_LEADS       = "Lead table"

print("=" * 60, flush=True)
print("[STARTUP] Shopify → Airtable Abandoned Cart Service", flush=True)
print(f"[STARTUP] Airtable Base ID : {AIRTABLE_BASE_ID}", flush=True)
print(f"[STARTUP] Customers table  : {TABLE_CUSTOMERS}", flush=True)
print(f"[STARTUP] Inventories table: {TABLE_INVENTORIES}", flush=True)
print(f"[STARTUP] Leads table      : {TABLE_LEADS}", flush=True)
print(f"[STARTUP] Webhook secret   : {'SET' if SHOPIFY_SECRET else 'NOT SET (verification skipped)'}", flush=True)
print(f"[STARTUP] Shopify store    : {SHOPIFY_STORE or 'NOT SET'}", flush=True)
print(f"[STARTUP] Shopify token    : {'SET' if SHOPIFY_ADMIN_TOKEN else 'NOT SET (sync disabled)'}", flush=True)
print("=" * 60, flush=True)


# ── Shopify webhook verification (Section 4) ──────────────────────────────────

def verify_webhook(raw_body: bytes, hmac_header: str) -> bool:
    if not SHOPIFY_SECRET:
        print("[WEBHOOK] No secret configured — skipping HMAC verification", flush=True)
        return True
    digest = hmac.new(SHOPIFY_SECRET.encode(), raw_body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    result = hmac.compare_digest(computed, hmac_header or "")
    print(f"[WEBHOOK] HMAC verification: {'PASSED' if result else 'FAILED'}", flush=True)
    return result


# ── Airtable helpers (Section 4) ──────────────────────────────────────────────

def at_get(table: str, formula: str) -> list:
    url = f"{AT_BASE}/{requests.utils.quote(table)}"
    print(f"[AIRTABLE GET] Table: {table}", flush=True)
    print(f"[AIRTABLE GET] Formula: {formula}", flush=True)
    resp = requests.get(url, headers=AT_HEADERS, params={"filterByFormula": formula})
    print(f"[AIRTABLE GET] Status: {resp.status_code}", flush=True)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    print(f"[AIRTABLE GET] Records found: {len(records)}", flush=True)
    return records


def at_create(table: str, fields: dict) -> dict:
    url = f"{AT_BASE}/{requests.utils.quote(table)}"
    print(f"[AIRTABLE CREATE] Table: {table}", flush=True)
    print(f"[AIRTABLE CREATE] Fields: {fields}", flush=True)
    resp = requests.post(url, headers=AT_HEADERS, json={"fields": fields})
    print(f"[AIRTABLE CREATE] Status: {resp.status_code}", flush=True)
    if not resp.ok:
        print(f"[AIRTABLE CREATE] Error: {resp.text}", flush=True)
    resp.raise_for_status()
    record = resp.json()
    print(f"[AIRTABLE CREATE] Created record ID: {record.get('id')}", flush=True)
    return record


# ── Business logic (Section 4) ────────────────────────────────────────────────

def find_customer(phone: str, email: str) -> dict | None:
    """
    Name/Mobile/Mail is a FORMULA field (read-only):
      CONCATENATE({Customer Name},"/",{Contact Number},"/",{Mail id})
    Search by actual underlying fields: Contact Number and Mail id.
    """
    print(f"\n[CUSTOMER SEARCH] phone={phone!r}  email={email!r}", flush=True)
    parts = []
    if phone:
        stripped = ''.join(c for c in phone if c.isdigit())
        if stripped:
            parts.append(f"{{Whatsapp number}}='{stripped}'")
    if email:
        parts.append(f"LOWER({{Mail id}})='{email.lower()}'")

    if not parts:
        print("[CUSTOMER SEARCH] No contact info — cannot search", flush=True)
        return None

    formula = f"OR({','.join(parts)})"
    records = at_get(TABLE_CUSTOMERS, formula)

    if records:
        rec = records[0]
        print(f"[CUSTOMER SEARCH] Found: ID={rec['id']}  Name={rec.get('fields',{}).get('Customer Name')}", flush=True)
        return rec
    print("[CUSTOMER SEARCH] Not found", flush=True)
    return None


def create_customer(name: str, phone: str, email: str) -> dict:
    """
    Write only editable fields. Name/Mobile/Mail auto-computes.
    """
    print(f"\n[CUSTOMER CREATE] name={name!r}  phone={phone!r}  email={email!r}", flush=True)
    fields: dict = {}
    if name:  fields["Customer Name"]  = name
    if phone: fields["Whatsapp number"] = phone
    if email: fields["Mail id"]        = email
    record = at_create(TABLE_CUSTOMERS, fields)
    print(f"[CUSTOMER CREATE] New customer ID: {record.get('id')}", flush=True)
    return record


def cart_find_product_by_sku(sku: str) -> dict | None:
    # NOTE: renamed from find_product_by_sku to cart_find_product_by_sku
    # to avoid conflict with Section 2's find_product_by_sku
    print(f"\n[PRODUCT SEARCH] SKU={sku!r}", flush=True)
    if not sku:
        print("[PRODUCT SEARCH] Empty SKU — skip", flush=True)
        return None
    records = at_get(TABLE_INVENTORIES, f"{{SKU}}='{sku}'")
    if records:
        rec = records[0]
        print(f"[PRODUCT SEARCH] Found: ID={rec['id']}  Name={rec.get('fields',{}).get('Product Name','?')}", flush=True)
        return rec
    print(f"[PRODUCT SEARCH] SKU '{sku}' NOT found in {TABLE_INVENTORIES}", flush=True)
    return None


def lead_exists_for_customer(customer_id: str) -> bool:
    """Check if a lead already exists for this customer to avoid duplicates."""
    formula = f"FIND('{customer_id}', ARRAYJOIN(Customers, ','))"
    records = at_get(TABLE_LEADS, formula)
    return len(records) > 0


def create_lead(customer_id: str, product_ids: list[str], abandoned_date: str) -> dict:
    print(f"\n[LEAD CREATE] customer_id={customer_id}", flush=True)
    print(f"[LEAD CREATE] product_ids={product_ids}", flush=True)
    print(f"[LEAD CREATE] date={abandoned_date}", flush=True)
    fields = {
        "Customers":           [customer_id],
        "Interested products": product_ids,
        "Lead created date":   abandoned_date,
        "Lead Source":         "Abandoned cart",
    }
    record = at_create(TABLE_LEADS, fields)
    print(f"[LEAD CREATE] Lead ID: {record.get('id')}", flush=True)
    return record


def process_single_checkout(checkout: dict) -> dict:
    """
    Shared logic used by both the webhook and the sync route.
    Returns a result dict describing what happened.
    """
    checkout_id = checkout.get("id")

    # Skip completed checkouts
    if checkout.get("completed_at"):
        print(f"[PROCESS] Checkout {checkout_id} already completed — skipping", flush=True)
        return {"status": "skipped", "reason": "already completed", "checkout_id": checkout_id}

    # Extract contact info
    cust    = checkout.get("customer") or {}
    billing = checkout.get("billing_address") or {}
    first = (cust.get("first_name") or billing.get("first_name") or "").strip()
    last  = (cust.get("last_name")  or billing.get("last_name")  or "").strip()
    name  = f"{first} {last}".strip() or billing.get("name", "Unknown")
    email = (cust.get("email") or checkout.get("email") or "").strip().lower()
    phone = (cust.get("phone") or billing.get("phone") or checkout.get("phone") or "").strip()

    print(f"[EXTRACT] Name : {name!r}", flush=True)
    print(f"[EXTRACT] Email: {email!r}", flush=True)
    print(f"[EXTRACT] Phone: {phone!r}", flush=True)

    if not email and not phone:
        print("[EXTRACT] No contact info — skipping", flush=True)
        return {"status": "skipped", "reason": "no contact info", "checkout_id": checkout_id}

    # Parse date
    raw_date = checkout.get("created_at", "")
    try:
        abandoned_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        abandoned_date = datetime.utcnow().strftime("%Y-%m-%d")
    print(f"[EXTRACT] Abandoned date: {abandoned_date}", flush=True)

    # Line items
    line_items = checkout.get("line_items", [])
    print(f"[EXTRACT] Line items ({len(line_items)}):", flush=True)
    for i, item in enumerate(line_items, 1):
        print(f"  [{i}] title={item.get('title')!r}  sku={item.get('sku')!r}  qty={item.get('quantity')}", flush=True)

    # STEP 1 — Find or create customer
    print("\n[STEP 1] Customer lookup...", flush=True)
    customer_record = find_customer(phone, email)
    customer_action = "found"
    if customer_record:
        print(f"[STEP 1] Existing customer: {customer_record['id']}", flush=True)
    else:
        print("[STEP 1] Not found — creating new customer", flush=True)
        customer_record = create_customer(name, phone, email)
        customer_action = "created"
        print(f"[STEP 1] New customer: {customer_record['id']}", flush=True)
    customer_id = customer_record["id"]

    # STEP 2 — Match SKUs
    print("\n[STEP 2] SKU matching...", flush=True)
    product_ids: list[str] = []
    unmatched_skus: list[str] = []
    for item in line_items:
        sku = (item.get("sku") or "").strip()
        if not sku:
            print(f"  [SKIP] '{item.get('title')}' has no SKU", flush=True)
            continue
        prod = cart_find_product_by_sku(sku)
        if prod:
            product_ids.append(prod["id"])
            print(f"  [OK] {sku} -> {prod['id']}", flush=True)
        else:
            unmatched_skus.append(sku)
            print(f"  [MISS] {sku} not found", flush=True)
    print(f"[STEP 2] Matched={len(product_ids)}  Unmatched={unmatched_skus}", flush=True)

    if not product_ids:
        print("[STEP 2] No matched products — lead NOT created", flush=True)
        return {
            "status":         "skipped",
            "reason":         "no matching SKUs",
            "checkout_id":    checkout_id,
            "customer_id":    customer_id,
            "customer_action": customer_action,
            "unmatched_skus": unmatched_skus,
        }

    # STEP 3 — Create lead
    print("\n[STEP 3] Creating lead...", flush=True)
    lead = create_lead(customer_id, product_ids, abandoned_date)
    lead_id = lead.get("id")
    print(f"\n[DONE] customer_id={customer_id}  lead_id={lead_id}  products={len(product_ids)}  unmatched={unmatched_skus}", flush=True)

    return {
        "status":          "success",
        "checkout_id":     checkout_id,
        "customer_id":     customer_id,
        "customer_action": customer_action,
        "lead_id":         lead_id,
        "products_linked": len(product_ids),
        "unmatched_skus":  unmatched_skus,
    }


# ── Webhook route (Section 4) ─────────────────────────────────────────────────

@app.route("/webhook/abandoned-checkout", methods=["POST"])
def abandoned_checkout():
    print("\n" + "=" * 60, flush=True)
    print(f"[WEBHOOK] Received at {datetime.utcnow().isoformat()}Z", flush=True)

    if not verify_webhook(request.data, request.headers.get("X-Shopify-Hmac-SHA256", "")):
        print("[WEBHOOK] Rejected — HMAC mismatch", flush=True)
        return jsonify({"error": "Unauthorized"}), 401

    checkout = request.get_json(force=True)
    if not checkout:
        print("[WEBHOOK] No JSON payload", flush=True)
        return jsonify({"error": "No payload"}), 400

    print(f"[WEBHOOK] Checkout ID   : {checkout.get('id')}", flush=True)
    print(f"[WEBHOOK] Checkout token: {checkout.get('token', 'N/A')}", flush=True)

    result = process_single_checkout(checkout)
    print("=" * 60, flush=True)

    if result["status"] == "skipped":
        return jsonify(result), 200
    return jsonify(result), 200


# ── Global sync state (Section 4) ─────────────────────────────────────────────
sync_state = {
    "running": False,
    "started_at": None,
    "stats": {},
    "last_error": None,
}


def run_sync_in_background(max_limit, since_date):
    """Runs the full Shopify → Airtable sync in a background thread."""
    global sync_state
    sync_state["running"]    = True
    sync_state["started_at"] = datetime.utcnow().isoformat() + "Z"
    sync_state["last_error"] = None
    stats = sync_state["stats"] = {
        "fetched": 0, "success": 0,
        "skipped_completed": 0, "skipped_no_contact": 0,
        "skipped_no_sku": 0, "duplicate_lead": 0, "errors": 0,
    }

    try:
        url    = f"https://{SHOPIFY_STORE}/admin/api/2024-04/checkouts.json"
        params = {"limit": 250, "status": "open"}
        if since_date:
            params["created_at_min"] = since_date

        page = 1
        done = False

        while url and not done:
            print(f"\n[SYNC] Fetching Shopify page {page}...", flush=True)
            resp = requests.get(url, headers=SHOPIFY_HEADERS, params=params)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2))
                print(f"[SYNC] Rate limited — waiting {wait}s", flush=True)
                time.sleep(wait)
                continue

            if not resp.ok:
                msg = f"Shopify API error: {resp.status_code} {resp.text}"
                print(f"[SYNC] {msg}", flush=True)
                sync_state["last_error"] = msg
                break

            checkouts = resp.json().get("checkouts", [])
            print(f"[SYNC] Page {page}: {len(checkouts)} checkouts received", flush=True)

            for checkout in checkouts:
                stats["fetched"] += 1
                print(f"\n[SYNC] ── Checkout #{checkout.get('id')} ({stats['fetched']}) ──", flush=True)

                cust  = checkout.get("customer") or {}
                phone = (cust.get("phone") or checkout.get("phone") or "").strip()
                email = (cust.get("email") or checkout.get("email") or "").strip().lower()
                try:
                    existing_customer = find_customer(phone, email) if (phone or email) else None
                except Exception:
                    existing_customer = None
                if existing_customer and lead_exists_for_customer(existing_customer["id"]):
                    print(f"[SYNC] Lead already exists for customer {existing_customer['id']} — skipping", flush=True)
                    stats["duplicate_lead"] += 1
                    continue

                try:
                    result = process_single_checkout(checkout)
                    if result["status"] == "success":
                        stats["success"] += 1
                    elif result.get("reason") == "already completed":
                        stats["skipped_completed"] += 1
                    elif result.get("reason") == "no contact info":
                        stats["skipped_no_contact"] += 1
                    elif result.get("reason") == "no matching SKUs":
                        stats["skipped_no_sku"] += 1
                except Exception as e:
                    print(f"[SYNC] ERROR on checkout {checkout.get('id')}: {e}", flush=True)
                    stats["errors"] += 1

                time.sleep(0.3)

                if max_limit and stats["fetched"] >= max_limit:
                    print(f"[SYNC] Reached limit of {max_limit} — stopping", flush=True)
                    done = True
                    break

            # Pagination
            link = resp.headers.get("Link", "")
            url  = None
            params = {}
            if 'rel="next"' in link:
                for part in link.split(","):
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip().strip("<>")
                        break
            page += 1

    except Exception as e:
        print(f"[SYNC] Fatal error: {e}", flush=True)
        sync_state["last_error"] = str(e)
    finally:
        sync_state["running"] = False
        print(f"\n[SYNC] Background thread complete — {stats}", flush=True)
        print("=" * 60, flush=True)


@app.route("/sync/abandoned-checkouts", methods=["GET", "POST"])
def sync_abandoned_checkouts():
    print("\n" + "=" * 60, flush=True)
    print(f"[SYNC] Request received at {datetime.utcnow().isoformat()}Z", flush=True)

    if not SHOPIFY_STORE or not SHOPIFY_ADMIN_TOKEN:
        print("[SYNC] SHOPIFY_STORE or SHOPIFY_ADMIN_TOKEN not set", flush=True)
        return jsonify({"error": "SHOPIFY_STORE and SHOPIFY_ADMIN_TOKEN env vars required"}), 500

    # Check status only — don't start a new sync if one is running
    if request.args.get("status") == "1":
        return jsonify({"sync_state": sync_state}), 200

    if sync_state["running"]:
        print("[SYNC] Already running — returning current progress", flush=True)
        return jsonify({
            "message":    "Sync already in progress",
            "sync_state": sync_state,
        }), 200

    body       = request.get_json(force=True, silent=True) or {}
    max_limit  = body.get("limit", None)
    since_date = body.get("since", None)
    print(f"[SYNC] Starting background thread: max_limit={max_limit}  since_date={since_date}", flush=True)

    thread = threading.Thread(
        target=run_sync_in_background,
        args=(max_limit, since_date),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "message":    "Sync started in background. Check Render logs for progress.",
        "status_url": "/sync/abandoned-checkouts?status=1",
        "params":     {"limit": max_limit, "since": since_date},
    }), 200


# ── Health check (Section 4) ──────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    print("[HEALTH] Health check", flush=True)
    return jsonify({"status": "ok", "service": "shopify-airtable-abandoned-cart"}), 200