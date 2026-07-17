"""
trendyol.py — Section 3: Trendyol → Airtable Sync
Split out of the original combined app.py. Code below is unchanged from the
merged version; only the file location and these import lines changed.
"""
import os
import base64
import threading
from datetime import datetime
from flask import request, jsonify
import requests

from shared import app, AIRTABLE_URL, REQUEST_TIMEOUT
from amazon_sync import (
    AIRTABLE_TOKEN, BASE_ID, CUSTOMERS_TABLE_ID,
    ORDERS_TABLE_ID, ORDER_LINE_ITEMS_TABLE_ID, FRENCH_INVENTORIES_TABLE_ID,
)


TRENDYOL_SELLER_ID = os.getenv("SELLER_ID")
TRENDYOL_API_KEY   = os.getenv("API_KEY")
TRENDYOL_API_SECRET = os.getenv("API_SECRET")

TRENDYOL_BASE_URL = "https://apigw.trendyol.com"

print("🔐 ENV CHECK:", flush=True)
print("AIRTABLE_TOKEN:", bool(AIRTABLE_TOKEN), flush=True)
print("BASE_ID:", bool(BASE_ID), flush=True)
print("CUSTOMERS_TABLE:", bool(CUSTOMERS_TABLE_ID), flush=True)
print("ORDERS_TABLE:", bool(ORDERS_TABLE_ID), flush=True)
print("ORDER_LINE_ITEMS_TABLE:", bool(ORDER_LINE_ITEMS_TABLE_ID), flush=True)
print("FRENCH_INVENTORIES_TABLE:", bool(FRENCH_INVENTORIES_TABLE_ID), flush=True)
print("SELLER_ID:", bool(TRENDYOL_SELLER_ID), flush=True)
print("API_KEY:", bool(TRENDYOL_API_KEY), flush=True)
print("API_SECRET:", bool(TRENDYOL_API_SECRET), flush=True)
print("--------------------------------------------------", flush=True)

AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

basic_token = base64.b64encode(
    f"{TRENDYOL_API_KEY}:{TRENDYOL_API_SECRET}".encode()
).decode()

TRENDYOL_HEADERS = {
    "Authorization": f"Basic {basic_token}",
    "User-Agent": f"{TRENDYOL_SELLER_ID} - Self Integration",
    "Content-Type": "application/json",
    "storeFrontCode": "AE"
}

sync_lock = threading.Lock()


# ── Airtable helpers (Section 3)
# NOTE: renamed with ty_ prefix to avoid conflict with Section 2 helpers ──────

def ty_airtable_search(table_id, formula):
    print(f"🔍 Airtable search | table={table_id} | formula={formula}", flush=True)
    r = requests.get(
        f"{AIRTABLE_URL}/{BASE_ID}/{table_id}",
        headers=AIRTABLE_HEADERS,
        params={"filterByFormula": formula},
        timeout=REQUEST_TIMEOUT
    )
    r.raise_for_status()
    records = r.json().get("records", [])
    print(f"🔍 Found {len(records)} records", flush=True)
    return records

def ty_airtable_create(table_id, fields):
    print(f"📝 Creating Airtable record in table={table_id}", flush=True)
    print("🧾 Payload:", fields, flush=True)
    r = requests.post(
        f"{AIRTABLE_URL}/{BASE_ID}/{table_id}",
        headers=AIRTABLE_HEADERS,
        json={"fields": fields},
        timeout=REQUEST_TIMEOUT
    )
    if r.status_code >= 400:
        print("❌ Airtable error:", r.text, flush=True)
        r.raise_for_status()
    record = r.json()
    print("✅ Airtable record created:", record["id"], flush=True)
    return record["id"]

def ty_airtable_update(table_id, record_id, fields):
    print(f"✏️ Updating Airtable record {record_id} in table={table_id}", flush=True)
    print("🧾 Update payload:", fields, flush=True)
    r = requests.patch(
        f"{AIRTABLE_URL}/{BASE_ID}/{table_id}/{record_id}",
        headers=AIRTABLE_HEADERS,
        json={"fields": fields},
        timeout=REQUEST_TIMEOUT
    )
    if r.status_code >= 400:
        print("❌ Airtable update error:", r.text, flush=True)
        r.raise_for_status()
    print("✅ Airtable record updated", flush=True)


# ── Status mappers (Section 3) ─────────────────────────────────────────────────

def map_shipping_status(order):
    s = order.get("status", "").lower()
    if s == "delivered":
        return "Delivered"
    if s in ["shipped", "invoiced", "in_transit"]:
        return "In Transit"
    if s == "cancelled":
        return "Cancelled"
    return "New"

def map_payment_status(order):
    s = order.get("status", "").lower()
    if s in ["paid", "invoiced"]:
        return "Paid"
    if s == "cancelled":
        return "Failed"
    if s == "refunded":
        return "Refunded"
    return "Pending"


# ── Customer (Section 3)
# NOTE: renamed ty_get_or_create_customer to avoid conflict with Section 2 ─────

def ty_get_or_create_customer(c):
    print(f"👤 Processing customer {c['id']} | {c['name']}", flush=True)
    records = ty_airtable_search(
        CUSTOMERS_TABLE_ID,
        f"{{Trendyol Id}}='{c['id']}'"
    )
    if records:
        print("👤 Existing customer found", flush=True)
        return records[0]["id"]

    print("👤 Creating new customer", flush=True)
    record_id = ty_airtable_create(
        CUSTOMERS_TABLE_ID,
        {
            "Customer Name": c["name"],
            "Trendyol Id": c["id"]
        }
    )
    print("👤 Customer created:", record_id, flush=True)
    return record_id


# ── French Inventories — find product by SKU (Section 3) ──────────────────────

def get_french_inventory_record_id(merchant_sku):
    if not merchant_sku:
        print("⚠️ No merchantSku provided — skipping product lookup", flush=True)
        return None

    print(f"🔎 Looking up French Inventories | SKU={merchant_sku}", flush=True)
    records = ty_airtable_search(
        FRENCH_INVENTORIES_TABLE_ID,
        f"{{SKU}}='{merchant_sku}'"
    )
    if records:
        record_id = records[0]["id"]
        print(f"✅ Found French Inventory record: {record_id}", flush=True)
        return record_id

    print(f"⚠️ No French Inventory record found for SKU={merchant_sku}", flush=True)
    return None


# ── Orders table — get or create order (Section 3)
# NOTE: renamed ty_get_or_create_order to avoid conflict with Section 2 ────────

def ty_get_or_create_order(order_id, order_number, customer_id, order_date, pay, ship, ship_by=None):
    print(f"📋 Processing Orders table | Order ID={order_id}", flush=True)
    records = ty_airtable_search(
        ORDERS_TABLE_ID,
        f"{{Order ID}}='{order_id}'"
    )

    if records:
        existing_id = records[0]["id"]
        print(f"📋 Existing order found: {existing_id} — updating statuses", flush=True)
        update_fields = {
            "Payment Status": pay,
            "Shipping Status": ship
        }
        if ship_by:
            update_fields["Ship By"] = ship_by
        ty_airtable_update(ORDERS_TABLE_ID, existing_id, update_fields)
        return existing_id

    print(f"📋 Creating new order in Orders table", flush=True)
    create_fields = {
        "Order ID": order_id,
        "Customer": [customer_id],
        "Order Date": order_date,
        "Sales Channel": "Trendyol",
        "Payment Status": pay,
        "Shipping Status": ship
    }
    if ship_by:
        create_fields["Ship By"] = ship_by
    new_id = ty_airtable_create(ORDERS_TABLE_ID, create_fields)
    print(f"📋 Order created: {new_id}", flush=True)
    return new_id


# ── Order line items — duplicate check (Section 3) ────────────────────────────

def get_existing_order_line(order_id, product_name):
    print(f"🔁 Checking existing line | Order={order_id} | Product={product_name}", flush=True)
    records = ty_airtable_search(
        ORDER_LINE_ITEMS_TABLE_ID,
        f"AND({{Order ID}}='{order_id}', {{Trendyol Product Name}}='{product_name}')"
    )
    if records:
        record_id = records[0]["id"]
        print(f"🔁 Found existing record: {record_id}", flush=True)
        return record_id
    print("🔁 No existing record found", flush=True)
    return None


# ── Order line items — create (Section 3) ─────────────────────────────────────

def create_order_line(
    order_id, order_number, order_record_id,
    customer_id, date, pay, ship,
    product, qty, price,
    french_inventory_record_id
):
    print(f"🛒 Creating line item | {order_number} | {product}", flush=True)

    fields = {
        "Order ID": order_id,
        "Order Number": order_number,
        "Order Date": date,
        "Rate": price,
        "Qty": qty,
        "Trendyol Product Name": product,
        "Sales Channel": "Trendyol",
        "Payment Status": pay,
        "Shipping Status": ship,
        "Customer": [customer_id],
        "Order": [order_record_id],
    }

    if french_inventory_record_id:
        fields["Product"] = [french_inventory_record_id]

    ty_airtable_create(ORDER_LINE_ITEMS_TABLE_ID, fields)


# ── Order line items — update statuses (Section 3) ────────────────────────────

def update_order_line_statuses(record_id, pay, ship):
    print(f"🔄 Updating statuses for record {record_id} | Pay={pay} | Ship={ship}", flush=True)
    ty_airtable_update(
        ORDER_LINE_ITEMS_TABLE_ID,
        record_id,
        {
            "Payment Status": pay,
            "Shipping Status": ship
        }
    )


# ── Main sync logic (Section 3) ───────────────────────────────────────────────

def sync_trendyol_orders_job():
    if not sync_lock.acquire(blocking=False):
        print("⏳ Sync already running — skipped", flush=True)
        return

    print("⏰ Trendyol sync started", flush=True)

    try:
        r = requests.get(
            f"{TRENDYOL_BASE_URL}/integration/order/sellers/{TRENDYOL_SELLER_ID}/orders",
            headers=TRENDYOL_HEADERS,
            params={"page": 0, "size": 50},
            timeout=REQUEST_TIMEOUT
        )
        r.raise_for_status()

        orders = r.json().get("content", [])
        print(f"📦 Orders fetched: {len(orders)}", flush=True)

        for o in orders:
            print(f"\n{'='*50}", flush=True)
            print(f"📦 Processing order {o['orderNumber']}", flush=True)

            order_id     = str(o["id"])
            order_number = str(o["orderNumber"])

            order_date = datetime.utcfromtimestamp(
                o["orderDate"] / 1000
            ).strftime("%Y-%m-%d")

            pay  = map_payment_status(o)
            ship = map_shipping_status(o)

            # ── Extract Ship By from estimatedDeliveryStartDate ──
            ship_by = None
            est_ts = o.get("estimatedDeliveryStartDate")
            if est_ts:
                try:
                    ship_by = datetime.utcfromtimestamp(est_ts / 1000).strftime("%Y-%m-%d")
                except Exception:
                    ship_by = None
            print(f"📅 Ship By: {ship_by}", flush=True)

            # ── STEP 1: Get or create Customer ──────────────────
            customer_id = ty_get_or_create_customer({
                "id":   str(o["customerId"]),
                "name": f"{o.get('customerFirstName', '')} {o.get('customerLastName', '')}".strip()
            })

            # ── STEP 2: Get or create/update Order in Orders table ──
            order_record_id = ty_get_or_create_order(
                order_id, order_number,
                customer_id, order_date,
                pay, ship, ship_by
            )

            # ── STEP 3: Process each line item ──────────────────
            for line in o.get("lines", []):
                product      = line.get("productName", "")
                qty          = line.get("quantity", 1)
                price        = line.get("price", 0)
                merchant_sku = line.get("merchantSku", "")

                french_inventory_record_id = get_french_inventory_record_id(merchant_sku)
                existing_record_id = get_existing_order_line(order_id, product)

                if existing_record_id:
                    update_order_line_statuses(existing_record_id, pay, ship)
                    print(f"🔄 Updated statuses for {order_number} → {product}", flush=True)
                else:
                    create_order_line(
                        order_id, order_number, order_record_id,
                        customer_id, order_date, pay, ship,
                        product, qty, price,
                        french_inventory_record_id
                    )
                    print(f"✅ Created line item for {order_number} → {product}", flush=True)

    except Exception as e:
        print("❌ Sync error:", e, flush=True)

    finally:
        sync_lock.release()
        print("🎉 Trendyol sync finished", flush=True)


# ── Trendyol Ship By backfill (Section 3) ────────────────────────────────────

def backfill_trendyol_ship_by_job():
    print("🔄 Starting Trendyol Ship By backfill (all months)...", flush=True)
    try:
        updated = 0
        skipped = 0

        # Loop month by month from Jan 2026 to today
        from datetime import date
        start_month = date(2026, 1, 1)
        today       = datetime.utcnow().date()

        current = start_month
        while current <= today:
            # Build start/end timestamps for the month
            month_start = int(datetime(current.year, current.month, 1).timestamp() * 1000)
            if current.month == 12:
                next_month = date(current.year + 1, 1, 1)
            else:
                next_month = date(current.year, current.month + 1, 1)
            month_end = int(datetime(next_month.year, next_month.month, 1).timestamp() * 1000) - 1

            print(f"📅 Fetching {current.strftime('%Y-%m')}...", flush=True)
            page = 0
            while True:
                r = requests.get(
                    f"{TRENDYOL_BASE_URL}/integration/order/sellers/{TRENDYOL_SELLER_ID}/orders",
                    headers=TRENDYOL_HEADERS,
                    params={"page": page, "size": 50, "startDate": month_start, "endDate": month_end},
                    timeout=REQUEST_TIMEOUT
                )
                r.raise_for_status()
                data        = r.json()
                orders      = data.get("content", [])
                total_pages = data.get("totalPages", 1)
                print(f"  Page {page+1}/{total_pages} — {len(orders)} orders", flush=True)

                for o in orders:
                    order_id = str(o["id"])
                    est_ts   = o.get("estimatedDeliveryStartDate")
                    if not est_ts:
                        skipped += 1
                        continue
                    try:
                        ship_by = datetime.utcfromtimestamp(est_ts / 1000).strftime("%Y-%m-%d")
                    except Exception:
                        skipped += 1
                        continue

                    records = ty_airtable_search(ORDERS_TABLE_ID, f"{{Order ID}}='{order_id}'")
                    if records:
                        ty_airtable_update(ORDERS_TABLE_ID, records[0]["id"], {"Ship By": ship_by})
                        print(f"  ✅ {order_id} → Ship By: {ship_by}", flush=True)
                        updated += 1
                    else:
                        skipped += 1

                page += 1
                if page >= total_pages:
                    break

            current = next_month

        print(f"🎉 Trendyol backfill complete: {updated} updated, {skipped} skipped", flush=True)

    except Exception as e:
        print(f"❌ Trendyol backfill error: {e}", flush=True)


# ── Routes (Section 3) ────────────────────────────────────────────────────────

@app.route("/ping/trendyol", methods=["GET"])
# NOTE: renamed from /ping to /ping/trendyol to avoid conflict with Section 2
def trendyol_ping():
    print("🔥 /ping/trendyol endpoint HIT", flush=True)

    received_secret = request.headers.get("X-Update-Secret")
    expected_secret = os.getenv("UPDATE_SECRET")

    if received_secret != expected_secret:
        print("⛔ Unauthorized", flush=True)
        return jsonify({"error": "Unauthorized"}), 401

    print("🚀 Starting background sync", flush=True)
    thread = threading.Thread(target=sync_trendyol_orders_job)
    thread.daemon = True
    thread.start()

    return jsonify({"status": "Sync started in background"}), 200

@app.route("/backfill-ship-by/trendyol", methods=["GET"])
def backfill_trendyol_ship_by():
    print("🔥 /backfill-ship-by/trendyol HIT", flush=True)
    thread = threading.Thread(target=backfill_trendyol_ship_by_job)
    thread.daemon = True
    thread.start()
    return jsonify({
        "status":  "Trendyol Ship By backfill started",
        "message": "Watch Render logs for progress"
    }), 200

@app.route("/wake/trendyol", methods=["GET"])
# NOTE: renamed from /wake to /wake/trendyol to avoid conflict with Section 2
def trendyol_wake():
    print("🌅 Server woken up", flush=True)
    return "awake", 200

@app.route("/trendyol", methods=["GET", "HEAD"])
# NOTE: renamed from / to /trendyol to avoid conflict with Section 1
def trendyol_health():
    return "OK", 200