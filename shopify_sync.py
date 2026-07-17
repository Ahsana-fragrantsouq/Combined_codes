"""
shopify_airtable.py — Section 7: Shopify Webhooks → Airtable
(orders / fulfillments / cancellations)
Split out of the original combined app.py. Code below is unchanged from the
merged version; only the file location and these import lines changed.

NOTE: the requested filename was "shopify-airtable.py" but Python module names
cannot contain hyphens (import shopify-airtable is invalid syntax), so this is
named shopify_airtable.py instead. Route paths and behavior are unchanged.
"""
import os
import hmac
import hashlib
import base64
import threading
from flask import request, jsonify
import requests

from shared import app
from delivery_tracker import API_VERSION, SHOPIFY_ACCESS_TOKEN, SHOPIFY_STORE
from amazon_sync import (
    CUSTOMERS_TABLE_ID, ORDERS_TABLE_ID,
    ORDER_LINE_ITEMS_TABLE_ID, FRENCH_INVENTORIES_TABLE_ID,
)
from trendyol_sync import AIRTABLE_HEADERS
from abandoned_cart import AIRTABLE_BASE_ID, SHOPIFY_SECRET

# NOTE — three fixes made while merging this section in, flagged for visibility:
#
# 1. BUG FIX: the original standalone script built its Shopify Orders API URL as
#    f"https://{SHOPIFY_STORE}.myshopify.com/..." while treating SHOPIFY_STORE as
#    just the shop name (e.g. "fragrantsouq"). But the SHOPIFY_STORE env var used
#    everywhere else in this combined app is the FULL domain
#    ("fragrantsouq.myshopify.com"), so appending ".myshopify.com" again here
#    would have built a broken double-domain URL. Fixed to reuse SHOPIFY_STORE
#    and API_VERSION exactly like Sections 1/4/5 do.
#
# 2. HARDCODED PRODUCTION IDs REMOVED: the original script hardcoded
#    AIRTABLE_BASE_ID = "app5gOqDt9aZrW5bV" and literal Customers/Orders/Order
#    Line Items/French Inventories table IDs directly in code. That would have
#    silently written to your PRODUCTION base even while testing against a
#    sandbox base via env vars. This section now reuses the shared
#    AIRTABLE_BASE_ID / CUSTOMERS_TABLE_ID / ORDERS_TABLE_ID /
#    ORDER_LINE_ITEMS_TABLE_ID / FRENCH_INVENTORIES_TABLE_ID already defined
#    earlier in this file (same ones Sections 2/3/4 use), so it respects
#    whichever base you've pointed the service at.
#
# 3. SHOPIFY_TOKEN falls back to the shared SHOPIFY_ACCESS_TOKEN if not set
#    separately, same fallback pattern used in Section 5/6.

SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN") or SHOPIFY_ACCESS_TOKEN
# NOTE: reuses AIRTABLE_HEADERS (Section 3), AIRTABLE_BASE_ID (Section 4),
# SHOPIFY_SECRET (Section 4, reads the same SHOPIFY_WEBHOOK_SECRET env var),
# and CUSTOMERS_TABLE_ID / ORDERS_TABLE_ID / ORDER_LINE_ITEMS_TABLE_ID /
# FRENCH_INVENTORIES_TABLE_ID (Section 2) instead of redeclaring them.

print("🚀 Section 7 — Shopify Webhook ↔ Airtable Integration Starting...", flush=True)

# ---------------- BACKGROUND SYNC STATE (Section 7) ----------------
_shopint_sync_running = False
_shopint_sync_lock    = threading.Lock()

# ---------------- SHOPIFY → AIRTABLE PAYMENT STATUS MAP (Section 7) ----------------
SHOPINT_PAYMENT_STATUS_MAP = {
    "paid":               "Paid",
    "pending":            "Pending",
    "partially_paid":     "Pending",
    "refunded":           "Refunded",
    "voided":             "Cancelled",
    "partially_refunded": "Refunded",
    "authorized":         "Pending",
}

# ---------------- SHOPIFY → AIRTABLE SHIPPING STATUS LOGIC (Section 7) ----------------
SHOPINT_SHIPPED_STATUSES = {
    "label_printed",
    "label_purchased",
    "attempted_delivery",
    "ready_for_pickup",
    "confirmed",
    "in_transit",
    "out_for_delivery",
}

def shopint_determine_shipping_status_from_order(order):
    # Check if order is cancelled
    if order.get("cancelled_at"):
        return "Cancelled"

    fulfillments = order.get("fulfillments") or []
    fulfillment_status = (order.get("fulfillment_status") or "").lower()

    has_delivered = False
    has_shipped   = False
    has_fulfilled = False

    for f in fulfillments:
        shipment_status = (f.get("shipment_status") or "").lower()
        f_status        = (f.get("status") or "").lower()

        if shipment_status == "delivered":
            has_delivered = True
        elif shipment_status in SHOPINT_SHIPPED_STATUSES:
            has_shipped = True
        elif f_status == "success":
            has_fulfilled = True

    if has_delivered:
        return "Delivered"
    if has_shipped:
        return "Shipped"
    if has_fulfilled or fulfillment_status in ("fulfilled", "partial"):
        return "Fulfilled"
    return "New"


def shopint_determine_shipping_status_from_fulfillment(fulfillment):
    shipment_status = (fulfillment.get("shipment_status") or "").lower()
    f_status        = (fulfillment.get("status") or "").lower()

    if shipment_status == "delivered":
        return "Delivered"
    if shipment_status in SHOPINT_SHIPPED_STATUSES:
        return "Shipped"
    if f_status == "success":
        return "Fulfilled"
    return "Fulfilled"


# ---------------- SECURITY (Section 7) ----------------
def shopint_verify_webhook(data, hmac_header):
    # NOTE: renamed from verify_webhook to shopint_verify_webhook to avoid
    # conflict with Section 4's verify_webhook (different signature/purpose)
    if not hmac_header or not SHOPIFY_SECRET:
        return False
    digest = hmac.new(
        SHOPIFY_SECRET.encode("utf-8"),
        data,
        hashlib.sha256
    ).digest()
    computed_hmac = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed_hmac, hmac_header)


# ---------------- AIRTABLE HELPERS (Section 7) ----------------
def shopint_find_customer(phone, email):
    # NOTE: renamed from find_customer to shopint_find_customer to avoid
    # conflict with Section 4's find_customer (different signature/behavior)
    if phone:
        formula = f"{{Whatsapp number}}='{phone}'"
    elif email:
        formula = f"{{Mail id}}='{email}'"
    else:
        return None

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}"
    r = requests.get(url, headers=AIRTABLE_HEADERS, params={"filterByFormula": formula})
    data = r.json()

    if data.get("records"):
        return data["records"][0]["id"]
    return None


def shopint_create_customer(customer):
    # NOTE: renamed from create_customer to shopint_create_customer to avoid
    # conflict with Section 4's create_customer (different signature)
    payload = {
        "fields": {
            "Customer Name":          customer["name"],
            "Mail id":                customer.get("email"),
            "Whatsapp number":        customer.get("phone"),
            "Address":                customer.get("address"),
            "Acquired sales channel": "Shopify"
        }
    }
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}"
    r = requests.post(url, headers=AIRTABLE_HEADERS, json=payload)
    print(f"👤 Customer create status: {r.status_code}", flush=True)
    print(f"👤 Customer create response: {r.text}", flush=True)
    return r.json().get("id")


def shopint_find_sku_record(sku):
    if not sku:
        return None
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{FRENCH_INVENTORIES_TABLE_ID}"
    r = requests.get(
        url,
        headers=AIRTABLE_HEADERS,
        params={"filterByFormula": f"{{SKU}}='{sku}'"}
    )
    data = r.json()
    if data.get("records"):
        return data["records"][0]["id"]
    return None


# ---------------- DUPLICATE CHECK (Section 7) ----------------
def shopint_order_exists(order_id):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{ORDERS_TABLE_ID}"
    r = requests.get(
        url,
        headers=AIRTABLE_HEADERS,
        params={"filterByFormula": f"{{Order ID}}='{order_id}'"}
    )
    records = r.json().get("records", [])
    if records:
        return records[0]["id"]
    return None


# ---------------- UPDATE EXISTING ORDER STATUSES (Section 7) ----------------
def shopint_refresh_existing_order_statuses(order):
    order_id     = str(order["id"])
    order_number = order.get("name", "?")

    shipping_status = shopint_determine_shipping_status_from_order(order)
    shopify_payment = (order.get("financial_status") or "pending").lower()
    payment_status  = SHOPINT_PAYMENT_STATUS_MAP.get(shopify_payment, "Pending")

    # ── Extract Ship By ──
    ship_by = None
    fulfillments = order.get("fulfillments") or []
    if fulfillments:
        f = fulfillments[0]
        raw = (
            f.get("created_at") or
            f.get("updated_at") or
            f.get("shipped_at") or
            order.get("updated_at") or
            ""
        )
        if raw:
            ship_by = raw[:10]

    # --- Update Orders table ---
    orders_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{ORDERS_TABLE_ID}"
    r = requests.get(orders_url, headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": f"{{Order ID}}='{order_id}'"})
    for record in r.json().get("records", []):
        fields = {"Shipping Status": shipping_status, "Payment Status": payment_status}
        if ship_by:
            fields["Ship By"] = ship_by
        requests.patch(f"{orders_url}/{record['id']}", headers=AIRTABLE_HEADERS, json={"fields": fields})

    # --- Update Order Line Items table ---
    line_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{ORDER_LINE_ITEMS_TABLE_ID}"
    r = requests.get(line_url, headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": f"{{Order ID}}='{order_id}'"})
    line_records = r.json().get("records", [])
    for record in line_records:
        fields = {"Shipping Status": shipping_status, "Payment Status": payment_status}
        if ship_by:
            fields["Ship By"] = ship_by
        requests.patch(f"{line_url}/{record['id']}", headers=AIRTABLE_HEADERS, json={"fields": fields})

    print(f"🔄 {order_number} refreshed → Shipping: {shipping_status}, "
          f"Payment: {payment_status}, Ship By: {ship_by or 'N/A'} ({len(line_records)} line item(s))", flush=True)

# ---------------- ORDERS TABLE (Section 7) ----------------
def shopint_create_order_record(order, customer_id):
    order_date      = order["created_at"].split("T")[0]
    order_id        = str(order["id"])
    order_number    = order.get("name", "").replace("#", "")
    shopify_payment = (order.get("financial_status") or "pending").lower()
    payment_status  = SHOPINT_PAYMENT_STATUS_MAP.get(shopify_payment, "Pending")

    # add ship by
    ship_by = None
    if order.get("fulfillments"):
        fulfilled_at = order["fulfillments"][0].get("created_at", "")
        if fulfilled_at:
            ship_by = fulfilled_at[:10]

    fields = {
        "Order ID":        order_id,
        "Customer":        [customer_id],
        "Order Date":      order_date,
        "Sales Channel":   "Shopify",
        "Shipping Status": shopint_determine_shipping_status_from_order(order),
        "Payment Status":  payment_status,
    }
    if ship_by:
        fields["Ship By"] = ship_by

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{ORDERS_TABLE_ID}"
    r = requests.post(url, headers=AIRTABLE_HEADERS, json={"fields": fields})

    if r.status_code in (200, 201):
        order_record_id = r.json().get("id")
        print(f"✅ Created Orders record: {order_number} (Airtable ID: {order_record_id})", flush=True)
        return order_record_id
    else:
        print(f"❌ Failed to create Orders record: {r.status_code} — {r.text}", flush=True)
        return None


# ---------------- SHIPPING STATUS UPDATE (Section 7) ----------------
def shopint_update_shipping_status(order_id, status, ship_by=None):
    # --- Update Orders table ---
    orders_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{ORDERS_TABLE_ID}"
    r = requests.get(
        orders_url,
        headers=AIRTABLE_HEADERS,
        params={"filterByFormula": f"{{Order ID}}='{order_id}'"}
    )
    order_records = r.json().get("records", [])
    for record in order_records:
        fields = {"Shipping Status": status}
        if ship_by:
            fields["Ship By"] = ship_by
        requests.patch(
            f"{orders_url}/{record['id']}",
            headers=AIRTABLE_HEADERS,
            json={"fields": fields}
        )
    print(f"🚚 Orders table Shipping Status → '{status}'", flush=True)

    # --- Update Order Line Items table ---
    line_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{ORDER_LINE_ITEMS_TABLE_ID}"
    r = requests.get(
        line_url,
        headers=AIRTABLE_HEADERS,
        params={"filterByFormula": f"{{Order ID}}='{order_id}'"}
    )
    line_records = r.json().get("records", [])
    if not line_records:
        print(f"⚠️ No line items found for Order ID {order_id}", flush=True)
    for record in line_records:
        fields = {"Shipping Status": status}
        if ship_by:
            fields["Ship By"] = ship_by
        requests.patch(
            f"{line_url}/{record['id']}",
            headers=AIRTABLE_HEADERS,
            json={"fields": fields}
        )
    print(f"🚚 Order Line Items Shipping Status → '{status}' on {len(line_records)} row(s)", flush=True)

# ---------------- ORDER LINE ITEM CREATION (Section 7) ----------------
def shopint_create_order_line_items(order, customer_id, order_record_id):
    print("🧾 Creating order line item records...", flush=True)

    order_date     = order["created_at"].split("T")[0]
    order_id       = str(order["id"])
    order_number   = order.get("name", "").replace("#", "")
    shopify_status = order.get("financial_status", "pending").lower()
    payment_status = SHOPINT_PAYMENT_STATUS_MAP.get(shopify_status, "Pending")
    shipping_status = shopint_determine_shipping_status_from_order(order)

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{ORDER_LINE_ITEMS_TABLE_ID}"

    for line in order.get("line_items", []):
        sku        = line.get("sku")
        product_id = shopint_find_sku_record(sku)

        price     = float(line.get("price", 0))
        qty       = int(line.get("quantity", 1))

        tax_lines = line.get("tax_lines", [])
        tax_rate  = tax_lines[0].get("rate", 0) if tax_lines else 0
        tax_pct   = f"{int(tax_rate * 100)}%"

        fields = {
            "Order ID":        order_id,
            "Order Number":    order_number,
            "Customer":        [customer_id],
            "Order Date":      order_date,
            "Rate":            price,
            "Qty":             qty,
            "Tax Type":        "5%",
            "Payment Status":  payment_status,
            "Shipping Status": shipping_status,
            "Sales Channel":   "Shopify",
        }

        if order_record_id:
            fields["Order"] = [order_record_id]

        if product_id:
            fields["Product"] = [product_id]
        else:
            print(f"⚠️ SKU '{sku}' not found in French Inventories — Product field left empty", flush=True)

        r = requests.post(url, headers=AIRTABLE_HEADERS, json={"fields": fields})

        if r.status_code in (200, 201):
            print(f"✅ Created line item: '{line.get('title')}' (SKU: {sku})", flush=True)
        else:
            print(f"❌ Failed line item '{line.get('title')}': {r.status_code} — {r.text}", flush=True)


# ---------------- MAIN LOGIC (Section 7) ----------------
def shopint_process_order(order):
    # NOTE: renamed from process_order to shopint_process_order to avoid
    # conflict with Section 2's process_order (Amazon orders, different signature)
    order_id = str(order["id"])

    existing_order_record_id = shopint_order_exists(order_id)
    if existing_order_record_id:
        print(f"⏭️ Order {order_id} already exists in Orders table — skipping", flush=True)
        return

    customer    = order.get("customer") or {}
    customer_id = shopint_find_customer(customer.get("phone"), customer.get("email"))

    if not customer_id:
        customer_id = shopint_create_customer({
            "name":    f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
            "email":   customer.get("email"),
            "phone":   customer.get("phone"),
            "address": (order.get("shipping_address") or {}).get("address1")
        })

    if not customer_id:
        print("❌ Could not find or create customer — aborting order", flush=True)
        return

    order_record_id = shopint_create_order_record(order, customer_id)
    if not order_record_id:
        print("❌ Could not create Orders record — aborting line items", flush=True)
        return

    shopint_create_order_line_items(order, customer_id, order_record_id)


# ---------------- WEBHOOK : ORDERS (Section 7) ----------------
@app.route("/shopify/webhook/orders", methods=["POST"])
def shopify_orders():
    data        = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")

    if not shopint_verify_webhook(data, hmac_header):
        return "Unauthorized", 401

    shopint_process_order(request.json)
    return jsonify({"status": "ok"})


# ---------------- WEBHOOK : FULFILLMENTS (Section 7) ----------------
@app.route("/shopify/webhook/fulfillments", methods=["POST"])
def shopify_fulfillments():
    data        = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")

    if not shopint_verify_webhook(data, hmac_header):
        return "Unauthorized", 401

    payload  = request.json
    order_id = payload.get("order_id")

    if not order_id:
        return jsonify({"status": "no order id"}), 200

    new_status   = shopint_determine_shipping_status_from_fulfillment(payload)
    fulfilled_at = (payload.get("created_at") or "")[:10]
    shopint_update_shipping_status(str(order_id), new_status, fulfilled_at)
    return jsonify({"status": new_status.lower()})


# ---------------- WEBHOOK : CANCELLATIONS (Section 7) ----------------
@app.route("/shopify/webhook/cancellations", methods=["POST"])
def shopify_cancellations():
    data        = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")

    if not shopint_verify_webhook(data, hmac_header):
        return "Unauthorized", 401

    payload    = request.json
    order_id   = str(payload.get("id", ""))
    order_name = payload.get("name", "?")

    if not order_id:
        return jsonify({"status": "no order id"}), 200

    shopint_update_shipping_status(order_id, "Cancelled")
    print(f"🚫 Order {order_name} marked as Cancelled", flush=True)
    return jsonify({"status": "cancelled"})


# ---------------- SYNC ALL SHOPIFY ORDERS (Section 7) ----------------
def shopint_fetch_all_shopify_orders():
    # NOTE: fixed to use SHOPIFY_STORE (already the full *.myshopify.com
    # domain) and the shared API_VERSION directly — see the fix note at the
    # top of this section for why the original ".myshopify.com" suffix was removed.
    orders = []
    url    = f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}/orders.json"
    params = {"limit": 250, "status": "any"}

    while url:
        r     = requests.get(url, headers={"X-Shopify-Access-Token": SHOPIFY_TOKEN}, params=params)
        batch = r.json().get("orders", [])
        orders.extend(batch)
        print(f"📦 Fetched {len(batch)} orders (total: {len(orders)})", flush=True)

        link   = r.headers.get("Link", "")
        url    = None
        params = {}
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break

    return orders


def _shopint_do_full_sync():
    global _shopint_sync_running

    try:
        all_orders = shopint_fetch_all_shopify_orders()
        print(f"✅ Total orders from Shopify: {len(all_orders)}", flush=True)

        synced  = 0
        updated = 0
        failed  = 0

        for order in all_orders:
            order_name = order.get("name", "?")
            try:
                order_id = str(order["id"])
                if shopint_order_exists(order_id):
                    shopint_refresh_existing_order_statuses(order)
                    updated += 1
                    continue

                customer    = order.get("customer") or {}
                customer_id = shopint_find_customer(customer.get("phone"), customer.get("email"))

                if not customer_id:
                    customer_id = shopint_create_customer({
                        "name":    f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
                        "email":   customer.get("email"),
                        "phone":   customer.get("phone"),
                        "address": (order.get("shipping_address") or {}).get("address1")
                    })

                if not customer_id:
                    print(f"❌ {order_name} — could not find/create customer", flush=True)
                    failed += 1
                    continue

                order_record_id = shopint_create_order_record(order, customer_id)
                if not order_record_id:
                    failed += 1
                    continue

                shopint_create_order_line_items(order, customer_id, order_record_id)
                print(f"✅ {order_name} synced", flush=True)
                synced += 1

            except Exception as e:
                print(f"❌ {order_name} — error: {e}", flush=True)
                failed += 1

        print(
            f"🎉 Sync complete: total={len(all_orders)} synced={synced} "
            f"updated={updated} failed={failed}",
            flush=True
        )

    except Exception as e:
        print(f"❌ Background sync crashed: {e}", flush=True)

    finally:
        with _shopint_sync_lock:
            _shopint_sync_running = False


# ---------------- WEBHOOK : ORDER UPDATED (Section 7) ----------------
@app.route("/shopify/webhook/order-updated", methods=["POST"])
def shopify_order_updated():
    data        = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")

    if not shopint_verify_webhook(data, hmac_header):
        return "Unauthorized", 401

    order    = request.json
    order_id = str(order.get("id", ""))

    if not order_id:
        return jsonify({"status": "no order id"}), 200

    # Update payment status
    shopify_payment = (order.get("financial_status") or "pending").lower()
    payment_status  = SHOPINT_PAYMENT_STATUS_MAP.get(shopify_payment, "Pending")

    # Update shipping status
    shipping_status = shopint_determine_shipping_status_from_order(order)

    # ── Extract Ship By ──
    ship_by = None
    fulfillments = order.get("fulfillments") or []
    if fulfillments:
        f = fulfillments[0]
        raw = (
            f.get("created_at") or
            f.get("updated_at") or
            order.get("updated_at") or
            ""
        )
        if raw:
            ship_by = raw[:10]

    # --- Update Orders table ---
    orders_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{ORDERS_TABLE_ID}"
    r = requests.get(
        orders_url,
        headers=AIRTABLE_HEADERS,
        params={"filterByFormula": f"{{Order ID}}='{order_id}'"}
    )
    for record in r.json().get("records", []):
        requests.patch(
            f"{orders_url}/{record['id']}",
            headers=AIRTABLE_HEADERS,
            json={"fields": {
                "Shipping Status": shipping_status,
                "Payment Status":  payment_status,
                **( {"Ship By": ship_by} if ship_by else {} )
            }}
        )

    # --- Update Order Line Items table ---
    line_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{ORDER_LINE_ITEMS_TABLE_ID}"
    r = requests.get(
        line_url,
        headers=AIRTABLE_HEADERS,
        params={"filterByFormula": f"{{Order ID}}='{order_id}'"}
    )
    for record in r.json().get("records", []):
        requests.patch(
            f"{line_url}/{record['id']}",
            headers=AIRTABLE_HEADERS,
            json={"fields": {
                "Shipping Status": shipping_status,
                "Payment Status":  payment_status,
                **( {"Ship By": ship_by} if ship_by else {} )
            }}
        )

    print(f"🔄 Order {order.get('name','?')} updated → Payment: {payment_status}, Shipping: {shipping_status}", flush=True)
    return jsonify({"status": "ok", "payment": payment_status, "shipping": shipping_status})


@app.route("/shopify-integration/sync", methods=["GET"])
# NOTE: namespaced from /sync to /shopify-integration/sync to keep it distinct
# from the other sync routes already defined (/sync-all, /sync-cost, etc.)
def shopint_sync_all_orders():
    global _shopint_sync_running

    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        return jsonify({
            "status":  "error",
            "message": "SHOPIFY_STORE or SHOPIFY_TOKEN env variable not set in Render"
        }), 500

    with _shopint_sync_lock:
        if _shopint_sync_running:
            return jsonify({
                "status":  "already_running",
                "message": "A sync is already running. Watch Render logs for progress."
            }), 409
        _shopint_sync_running = True

    print("🔄 Manual sync triggered — running in background...", flush=True)
    threading.Thread(target=_shopint_do_full_sync, daemon=True).start()

    return jsonify({
        "status":  "started",
        "message": "Sync started in background. Watch Render logs for progress. Look for '🎉 Sync complete' when done."
    }), 202


# ---------------- HEALTH CHECK (Section 7) ----------------
@app.route("/shopify-integration/health", methods=["GET"])
# NOTE: renamed from /health to /shopify-integration/health to avoid conflict with Section 4
def shopify_integration_health():
    return jsonify({"status": "ok", "service": "shopify-airtable-integration"}), 200