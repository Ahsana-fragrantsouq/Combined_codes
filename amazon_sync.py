"""
amazon.py — Section 2: Amazon → Airtable Sync
Split out of the original combined app.py. Code below is unchanged from the
merged version; only the file location and these import lines changed.
"""
import os
import csv
import io
import time
import threading
from datetime import datetime, timedelta
from flask import Response, request, jsonify
import requests
from requests_aws4auth import AWS4Auth

from shared import app, AIRTABLE_URL, REQUEST_TIMEOUT


AIRTABLE_TOKEN              = os.getenv("AIRTABLE_TOKEN")
BASE_ID                     = os.getenv("BASE_ID") or os.getenv("AIRTABLE_BASE_ID")
CUSTOMERS_TABLE_ID          = os.getenv("CUSTOMERS_TABLE")
ORDER_LINE_ITEMS_TABLE_ID   = os.getenv("ORDER_LINE_ITEMS_TABLE")
ORDERS_TABLE_ID             = os.getenv("ORDERS_TABLE")
FRENCH_INVENTORIES_TABLE_ID = os.getenv("FRENCH_INVENTORIES_TABLE")

AMZ_CLIENT_ID     = os.getenv("CLIENT_ID")
AMZ_CLIENT_SECRET = os.getenv("CLIENT_SECRET")
AMZ_REFRESH_TOKEN = os.getenv("AMZ_REFRESH_TOKEN")
AWS_ACCESS_KEY    = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY    = os.getenv("AWS_SECRET_KEY")
AWS_REGION        = os.getenv("AWS_REGION", "eu-west-1")
MARKETPLACE_ID    = "A2VIGQ35RCS4UG"  # UAE

AMZ_PRODUCTION = os.getenv("AMZ_PRODUCTION", "false").lower() == "true"
AMAZON_API_BASE = (
    "https://sellingpartnerapi-eu.amazon.com"
    if AMZ_PRODUCTION else
    "https://sandbox.sellingpartnerapi-eu.amazon.com"
)

def get_airtable_headers():
    return {
        "Authorization": f"Bearer {os.getenv('AIRTABLE_TOKEN')}",
        "Content-Type":  "application/json"
    }

aws_auth          = AWS4Auth(AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, "execute-api")
amazon_lock       = threading.Lock()
last_sync_time    = 0
MIN_SYNC_INTERVAL = 300  # minimum 5 minutes between syncs

# ── Startup log ────────────────────────────────────────────────────────────────
print("🚀 App starting...", flush=True)
print(f"🌍 Amazon mode: {'PRODUCTION' if AMZ_PRODUCTION else 'SANDBOX'}", flush=True)
print("AIRTABLE_TOKEN:",         bool(AIRTABLE_TOKEN), flush=True)
print("BASE_ID:",                bool(BASE_ID), flush=True)
print("CUSTOMERS_TABLE:",        bool(CUSTOMERS_TABLE_ID), flush=True)
print("ORDER_LINE_ITEMS:",       bool(ORDER_LINE_ITEMS_TABLE_ID), flush=True)
print("ORDERS_TABLE:",           bool(ORDERS_TABLE_ID), flush=True)
print("FRENCH_INVENTORIES:",     bool(FRENCH_INVENTORIES_TABLE_ID), flush=True)
print("CLIENT_ID:",              bool(AMZ_CLIENT_ID), flush=True)
print("AWS_ACCESS_KEY:",         bool(AWS_ACCESS_KEY), flush=True)


# ── Airtable helpers (Section 2) ───────────────────────────────────────────────

def airtable_search(table_id, formula):
    r = requests.get(
        f"{AIRTABLE_URL}/{BASE_ID}/{table_id}",
        headers=get_airtable_headers(),
        params={"filterByFormula": formula},
        timeout=REQUEST_TIMEOUT
    )
    r.raise_for_status()
    records = r.json().get("records", [])
    print(f"🔍 Found {len(records)} records", flush=True)
    return records

def airtable_create(table_id, fields):
    r = requests.post(
        f"{AIRTABLE_URL}/{BASE_ID}/{table_id}",
        headers=get_airtable_headers(),
        json={"fields": fields},
        timeout=REQUEST_TIMEOUT
    )
    if r.status_code >= 400:
        print("❌ Create error:", r.text, flush=True)
        r.raise_for_status()
    print("✅ Record created", flush=True)
    return r.json()

def airtable_update(table_id, record_id, fields):
    print(f"✏️ Updating {record_id}", flush=True)
    print(f"🧾 Fields being sent: {fields}", flush=True)
    r = requests.patch(
        f"{AIRTABLE_URL}/{BASE_ID}/{table_id}/{record_id}",
        headers=get_airtable_headers(),
        json={"fields": fields},
        timeout=REQUEST_TIMEOUT
    )
    print(f"🟡 Update status: {r.status_code}", flush=True)
    print(f"🟡 Update response: {r.text[:300]}", flush=True)
    if r.status_code >= 400:
        print("❌ Update error:", r.text, flush=True)
        r.raise_for_status()
    print("✅ Record updated", flush=True)


# ── Amazon helpers ─────────────────────────────────────────────────────────────

def get_amazon_token():
    print("🔑 Getting Amazon token...", flush=True)
    r = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type":    "refresh_token",
            "refresh_token": AMZ_REFRESH_TOKEN,
            "client_id":     AMZ_CLIENT_ID,
            "client_secret": AMZ_CLIENT_SECRET,
        },
        timeout=REQUEST_TIMEOUT
    )
    r.raise_for_status()
    print("✅ Amazon token received", flush=True)
    return r.json()["access_token"]

def get_amazon_orders(token, days=2):
    print(f"📦 Fetching Amazon orders (last {days} days)...", flush=True)
    if AMZ_PRODUCTION:
        params = {
            "MarketplaceIds": MARKETPLACE_ID,
            "CreatedAfter":   (datetime.utcnow() - timedelta(days=days)).isoformat()
        }
    else:
        params = {
            "MarketplaceIds": "ATVPDKIKX0DER",
            "CreatedAfter":   "TEST_CASE_200"
        }
    r = requests.get(
        f"{AMAZON_API_BASE}/orders/v0/orders",
        headers={"x-amz-access-token": token, "Content-Type": "application/json"},
        params=params,
        auth=aws_auth,
        timeout=REQUEST_TIMEOUT
    )
    print("🟡 Orders status:", r.status_code, flush=True)
    print("🟡 Orders response:", r.text[:300], flush=True)
    r.raise_for_status()
    orders = r.json().get("payload", {}).get("Orders", [])
    print(f"✅ Amazon orders fetched: {len(orders)}", flush=True)
    return orders

def get_amazon_order_items(token, order_id):
    if not AMZ_PRODUCTION:
        print("🧪 Sandbox — using dummy item", flush=True)
        return [{
            "Title":           "Test Product",
            "SellerSKU":       "TEST-SKU-001",
            "QuantityOrdered": 1,
            "ItemPrice":       {"Amount": "99.99", "CurrencyCode": "USD"}
        }]
    print(f"📦 Fetching items for {order_id}", flush=True)
    r = requests.get(
        f"{AMAZON_API_BASE}/orders/v0/orders/{order_id}/orderItems",
        headers={"x-amz-access-token": token, "Content-Type": "application/json"},
        auth=aws_auth,
        timeout=REQUEST_TIMEOUT
    )
    print("🟡 Items status:", r.status_code, flush=True)
    r.raise_for_status()
    items = r.json().get("payload", {}).get("OrderItems", [])
    print(f"✅ Items found: {len(items)}", flush=True)
    return items

def get_rdt_token(access_token, order_id):
    print(f"🔐 Getting RDT for {order_id}", flush=True)
    r = requests.post(
        f"{AMAZON_API_BASE}/tokens/2021-03-01/restrictedDataToken",
        headers={
            "x-amz-access-token": access_token,
            "Content-Type":       "application/json"
        },
        json={
            "restrictedResources": [{
                "method":       "GET",
                "path":         f"/orders/v0/orders/{order_id}",
                "dataElements": ["buyerInfo"]
            }]
        },
        auth=aws_auth,
        timeout=REQUEST_TIMEOUT
    )
    print(f"🟡 RDT status: {r.status_code}", flush=True)
    if r.status_code == 200:
        return r.json().get("restrictedDataToken")
    print(f"⚠️ RDT failed: {r.text[:200]}", flush=True)
    return None

def get_order_with_pii(access_token, order_id):
    rdt = get_rdt_token(access_token, order_id)
    if not rdt:
        return {}
    r = requests.get(
        f"{AMAZON_API_BASE}/orders/v0/orders/{order_id}",
        headers={"x-amz-access-token": rdt, "Content-Type": "application/json"},
        auth=aws_auth,
        timeout=REQUEST_TIMEOUT
    )
    print(f"🟡 PII order status: {r.status_code}", flush=True)
    if r.status_code == 200:
        payload = r.json().get("payload", {})
        print(f"🔍 BuyerInfo: {payload.get('BuyerInfo', {})}", flush=True)
        print(f"🔍 ShippingAddress: {payload.get('ShippingAddress', {})}", flush=True)
        return payload
    print(f"⚠️ PII order failed: {r.text[:200]}", flush=True)
    return {}


# ── Status mappers (Section 2) ─────────────────────────────────────────────────

def map_shipping(status):
    s = status.lower()
    if s == "shipped":                return "Shipped"
    if s == "delivered":              return "Delivered"
    if s == "canceled":               return "Cancelled"
    if s in ["unshipped", "pending"]: return "New"
    return "New"

def map_payment(status):
    s = status.lower()
    if s in ["shipped", "delivered"]: return "Paid"
    if s == "canceled":               return "Failed"
    return "Pending"


# ── Customer helpers (Section 2) ───────────────────────────────────────────────

def get_or_create_customer(order, access_token=None):
    order_id    = order.get("AmazonOrderId", "")
    buyer_name  = ""
    buyer_email = ""
    buyer_phone = ""

    if AMZ_PRODUCTION and access_token:
        pii_order   = get_order_with_pii(access_token, order_id)
        buyer_info  = pii_order.get("BuyerInfo", {})
        buyer_email = buyer_info.get("BuyerEmail", "").strip()
        buyer_name  = buyer_info.get("BuyerName", "").strip()
        if not buyer_name:
            shipping    = pii_order.get("ShippingAddress", {})
            buyer_phone = shipping.get("Phone", "").strip()
            city        = shipping.get("City", "")
            country     = shipping.get("CountryCode", "")
            if city or country:
                buyer_name = f"Amazon Customer - {city}, {country}".strip(", ")
                print(f"📦 Using city/country: {buyer_name}", flush=True)
    else:
        buyer_info  = order.get("BuyerInfo", {})
        buyer_email = buyer_info.get("BuyerEmail", "").strip()
        buyer_name  = buyer_info.get("BuyerName", "").strip()

    if not buyer_name:
        buyer_name = "Amazon Customer"

    amazon_id = buyer_email if buyer_email else order_id
    print(f"👤 Amazon Id: {amazon_id} | name: {buyer_name}", flush=True)

    # Step 0: Search by Amazon Id first (prevents duplicates)
    records = airtable_search(CUSTOMERS_TABLE_ID, f"{{Amazon Id}}='{amazon_id}'")
    if records:
        print(f"👤 Found by Amazon Id", flush=True)
        return records[0]["id"]

    # Step 1: Search by email (Mail id)
    if buyer_email:
        records = airtable_search(CUSTOMERS_TABLE_ID, f"{{Mail id}}='{buyer_email}'")
        if records:
            print(f"👤 Found by email: {records[0]['fields'].get('Customer Name','')}", flush=True)
            airtable_update(CUSTOMERS_TABLE_ID, records[0]["id"], {"Amazon Id": amazon_id})
            return records[0]["id"]

    # Step 2: Search by phone (Contact Number)
    if buyer_phone:
        records = airtable_search(CUSTOMERS_TABLE_ID, f"{{Contact Number}}='{buyer_phone}'")
        if records:
            print(f"👤 Found by phone: {records[0]['fields'].get('Customer Name','')}", flush=True)
            airtable_update(CUSTOMERS_TABLE_ID, records[0]["id"], {"Amazon Id": amazon_id})
            return records[0]["id"]

    # Step 3: Create new customer
    print(f"👤 Creating new customer: {buyer_name}", flush=True)
    fields = {
        "Customer Name":          buyer_name,
        "Amazon Id":              amazon_id,
        "Acquired sales channel": "Amazon",
    }
    if buyer_email:
        fields["Mail id"] = buyer_email
    if buyer_phone:
        fields["Contact Number"] = buyer_phone
    result = airtable_create(CUSTOMERS_TABLE_ID, fields)
    return result["id"]


# ── Orders table helpers (Section 2) ──────────────────────────────────────────

def get_or_create_order(order_id, customer_id, order_date, pay, ship, ship_by=None):
    print(f"📋 Orders table lookup | {order_id}", flush=True)
    records = airtable_search(ORDERS_TABLE_ID, f"{{Order ID}}='{order_id}'")
    if records:
        existing_id   = records[0]["id"]
        print(f"📋 Existing order found — updating", flush=True)
        update_fields = {
            "Payment Status":  pay,
            "Shipping Status": ship,
        }
        if ship_by:
            update_fields["Ship By"] = ship_by
        airtable_update(ORDERS_TABLE_ID, existing_id, update_fields)
        return existing_id
    print(f"📋 Creating new order record", flush=True)
    fields = {
        "Order ID":        order_id,
        "Sales Channel":   "Amazon",
        "Order Date":      order_date,
        "Payment Status":  pay,
        "Shipping Status": ship,
    }
    if ship_by:
        fields["Ship By"] = ship_by
    if customer_id:
        fields["Customer"] = [customer_id]
    result = airtable_create(ORDERS_TABLE_ID, fields)
    return result["id"]


# ── French Inventories helpers (Section 2) ─────────────────────────────────────

def find_product_by_sku(sku):
    if not sku:
        print("⚠️ No SKU provided", flush=True)
        return None
    print(f"🔎 Looking up SKU: {sku}", flush=True)
    records = airtable_search(FRENCH_INVENTORIES_TABLE_ID, f"{{SKU}}='{sku}'")
    if records:
        print(f"✅ Product found for SKU: {sku}", flush=True)
        return records[0]["id"]
    print(f"⚠️ No product found for SKU: {sku}", flush=True)
    return None


# ── Order line items helpers (Section 2) ──────────────────────────────────────

def get_existing_line(order_id):
    records = airtable_search(
        ORDER_LINE_ITEMS_TABLE_ID,
        f"{{Order ID}}='{order_id}'"
    )
    return records[0]["id"] if records else None

def build_line_fields(order_id, product_title, order_date, qty, price,
                      pay, ship, customer_id, orders_record_id, product_record_id):
    fields = {
        "Order ID":            order_id,
        "Order Number":        order_id,
        "Amazon Product Name": product_title,
        "Order Date":          order_date,
        "Qty":                 qty,
        "Rate":                price,
        "Tax Type":            "5%",
        "Sales Channel":       "Amazon",
        "Payment Status":      pay,
        "Shipping Status":     ship,
    }
    if customer_id:
        fields["Customer"] = [customer_id]
    if orders_record_id:
        fields["Order"] = [orders_record_id]
    if product_record_id:
        fields["Product"] = [product_record_id]
    return fields


# ── Process one Amazon order ───────────────────────────────────────────────────

def process_order(order, token):
    order_id     = order.get("AmazonOrderId", "")
    order_status = order.get("OrderStatus", "")
    order_date   = order.get("PurchaseDate", "")[:10]
    pay          = map_payment(order_status)
    ship         = map_shipping(order_status)

    print(f"\n📦 Processing {order_id} | {order_status}", flush=True)

    # Extract Ship By date from Amazon order
    ship_by_raw = order.get("LatestShipDate", "") or order.get("EarliestShipDate", "")
    ship_by     = ship_by_raw[:10] if ship_by_raw else None
    if ship_by:
        print(f"📅 Ship By: {ship_by}", flush=True)

    # Step 1: Get or create customer
    customer_id = get_or_create_customer(order, token)

    # Step 2: Get or create order in Orders table
    orders_record_id = get_or_create_order(
        order_id, customer_id, order_date, pay, ship, ship_by
    )

    # Step 3: Get order items from Amazon
    try:
        items = get_amazon_order_items(token, order_id)
    except Exception as e:
        print(f"❌ Items fetch failed: {e}", flush=True)
        return

    # Step 4: Create/update Order Line Items
    for item in items:
        product_title     = item.get("Title", "")
        sku               = item.get("SellerSKU", "")
        qty               = int(item.get("QuantityOrdered", 1))
        price             = float(item.get("ItemPrice", {}).get("Amount", 0))
        product_record_id = find_product_by_sku(sku)
        existing_id       = get_existing_line(order_id)

        fields = build_line_fields(
            order_id, product_title, order_date, qty, price,
            pay, ship, customer_id, orders_record_id, product_record_id
        )

        if existing_id:
            airtable_update(ORDER_LINE_ITEMS_TABLE_ID, existing_id, fields)
            print(f"🔄 Updated line item for {order_id}", flush=True)
        else:
            airtable_create(ORDER_LINE_ITEMS_TABLE_ID, fields)
            print(f"✅ Created: {order_id} → {product_title}", flush=True)


# ── Main sync jobs (Section 2) ────────────────────────────────────────────────

def sync_amazon_orders_job():
    if not amazon_lock.acquire(blocking=False):
        print("⏳ Sync already running — skipped", flush=True)
        return

    print(f"⏰ Amazon sync started ({'PRODUCTION' if AMZ_PRODUCTION else 'SANDBOX'})", flush=True)

    try:
        token  = get_amazon_token()
        orders = get_amazon_orders(token, days=2)
        for order in orders:
            process_order(order, token)
    except Exception as e:
        print("❌ Sync error:", e, flush=True)
    finally:
        amazon_lock.release()
        print("🎉 Amazon sync finished", flush=True)


def sync_all_orders_job():
    if not amazon_lock.acquire(blocking=False):
        print("⏳ Sync already running — skipped", flush=True)
        return

    print("⏰ SYNC ALL started", flush=True)

    try:
        token         = get_amazon_token()
        all_orders    = []
        created_after = (datetime.utcnow() - timedelta(days=500)).isoformat()
        next_token    = None

        while True:
            if AMZ_PRODUCTION:
                params = {"MarketplaceIds": MARKETPLACE_ID, "CreatedAfter": created_after}
            else:
                params = {"MarketplaceIds": "ATVPDKIKX0DER", "CreatedAfter": "TEST_CASE_200"}

            if next_token:
                params["NextToken"] = next_token

            r = requests.get(
                f"{AMAZON_API_BASE}/orders/v0/orders",
                headers={"x-amz-access-token": token, "Content-Type": "application/json"},
                params=params,
                auth=aws_auth,
                timeout=REQUEST_TIMEOUT
            )
            r.raise_for_status()
            payload    = r.json().get("payload", {})
            orders     = payload.get("Orders", [])
            next_token = payload.get("NextToken")
            all_orders.extend(orders)
            print(f"📦 Fetched {len(orders)} | Total: {len(all_orders)}", flush=True)
            if not next_token:
                break

        print(f"✅ Total orders to sync: {len(all_orders)}", flush=True)
        for order in all_orders:
            process_order(order, token)

    except Exception as e:
        print("❌ Sync all error:", e, flush=True)
    finally:
        amazon_lock.release()
        print("🎉 SYNC ALL finished", flush=True)


def backfill_ship_by_job():
    print("🔄 Starting Amazon Ship By backfill...", flush=True)
    try:
        token         = get_amazon_token()
        all_orders    = []
        created_after = (datetime.utcnow() - timedelta(days=500)).isoformat()
        next_token    = None

        while True:
            params = {"MarketplaceIds": MARKETPLACE_ID, "CreatedAfter": created_after}
            if next_token:
                params["NextToken"] = next_token
            r = requests.get(
                f"{AMAZON_API_BASE}/orders/v0/orders",
                headers={"x-amz-access-token": token, "Content-Type": "application/json"},
                params=params,
                auth=aws_auth,
                timeout=REQUEST_TIMEOUT
            )
            r.raise_for_status()
            payload    = r.json().get("payload", {})
            orders     = payload.get("Orders", [])
            next_token = payload.get("NextToken")
            all_orders.extend(orders)
            print(f"📦 Fetched {len(orders)} | Total: {len(all_orders)}", flush=True)
            if not next_token:
                break

        print(f"✅ Total orders to backfill: {len(all_orders)}", flush=True)
        updated = 0
        skipped = 0

        for order in all_orders:
            order_id    = order.get("AmazonOrderId", "")
            ship_by_raw = order.get("LatestShipDate", "") or order.get("EarliestShipDate", "")
            ship_by     = ship_by_raw[:10] if ship_by_raw else None

            if not ship_by:
                skipped += 1
                continue

            records = airtable_search(ORDERS_TABLE_ID, f"{{Order ID}}='{order_id}'")
            if records:
                airtable_update(ORDERS_TABLE_ID, records[0]["id"], {"Ship By": ship_by})
                print(f"✅ {order_id} → Ship By: {ship_by}", flush=True)
                updated += 1
            else:
                print(f"⚠️ Not found in Airtable: {order_id}", flush=True)
                skipped += 1

        print(f"🎉 Backfill complete: {updated} updated, {skipped} skipped", flush=True)

    except Exception as e:
        print(f"❌ Backfill error: {e}", flush=True)


# ── Routes (Section 2) ────────────────────────────────────────────────────────

@app.route("/amazon", methods=["GET", "HEAD"])
# NOTE: renamed from / to /amazon to avoid conflict with Section 1
def amazon_health():
    return "OK", 200

@app.route("/wake", methods=["GET"])
def wake():
    return "awake", 200

@app.route("/ping", methods=["GET"])
def ping():
    print("🔥 /ping HIT", flush=True)
    received_secret = request.headers.get("X-Update-Secret")
    expected_secret = os.getenv("UPDATE_SECRET")
    if received_secret != expected_secret:
        print("⛔ Unauthorized", flush=True)
        return jsonify({"error": "Unauthorized"}), 401
    thread = threading.Thread(target=sync_amazon_orders_job)
    thread.daemon = True
    thread.start()
    return jsonify({
        "status": "Sync started",
        "mode":   "PRODUCTION" if AMZ_PRODUCTION else "SANDBOX"
    }), 200

@app.route("/sync-all", methods=["GET"])
def sync_all():
    print("🔥 /sync-all HIT", flush=True)
    thread = threading.Thread(target=sync_all_orders_job)
    thread.daemon = True
    thread.start()
    return jsonify({
        "status": "Full sync started — last 60 days",
        "mode":   "PRODUCTION" if AMZ_PRODUCTION else "SANDBOX"
    }), 200

@app.route("/backfill-ship-by", methods=["GET"])
def backfill_ship_by():
    print("🔥 /backfill-ship-by HIT", flush=True)
    thread = threading.Thread(target=backfill_ship_by_job)
    thread.daemon = True
    thread.start()
    return jsonify({
        "status":  "Backfill started — last 500 days",
        "message": "Watch Render logs for progress"
    }), 200

@app.route("/callback")
def callback():
    code = request.args.get("spapi_oauth_code")
    if not code:
        return jsonify({"error": "No code received", "args": dict(request.args)}), 400
    print(f"📥 OAuth code received: {code[:20]}...", flush=True)
    r = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     AMZ_CLIENT_ID,
            "client_secret": AMZ_CLIENT_SECRET,
        },
        timeout=REQUEST_TIMEOUT
    )
    print(f"🟡 Token exchange status: {r.status_code}", flush=True)
    if r.status_code != 200:
        return jsonify({"error": "Token exchange failed", "detail": r.json()}), 400
    refresh_token = r.json().get("refresh_token", "")
    print(f"✅ Refresh token received: {refresh_token[:30]}...", flush=True)
    return f"""
    <html><body style="font-family:monospace;padding:40px;background:#f0fff0">
    <h2 style="color:green">✅ Authorization successful!</h2>
    <p><b>Copy your Refresh Token and save it in Render as AMZ_REFRESH_TOKEN:</b></p>
    <div style="background:#fff;border:2px solid green;padding:20px;
                word-break:break-all;border-radius:8px;margin:20px 0">
        {refresh_token}
    </div>
    <p>Then set AMZ_PRODUCTION=true and redeploy.</p>
    </body></html>
    """, 200

@app.route("/download-orders", methods=["GET"])
def download_orders():
    print("🔥 /download-orders HIT", flush=True)
    try:
        token      = get_amazon_token()
        all_orders = []
        if AMZ_PRODUCTION:
            params = {
                "MarketplaceIds": MARKETPLACE_ID,
                "CreatedAfter":   (datetime.utcnow() - timedelta(days=500)).isoformat()
            }
        else:
            params = {"MarketplaceIds": "ATVPDKIKX0DER", "CreatedAfter": "TEST_CASE_200"}

        next_token = None
        while True:
            if next_token:
                params["NextToken"] = next_token
            r = requests.get(
                f"{AMAZON_API_BASE}/orders/v0/orders",
                headers={"x-amz-access-token": token, "Content-Type": "application/json"},
                params=params,
                auth=aws_auth,
                timeout=REQUEST_TIMEOUT
            )
            r.raise_for_status()
            payload    = r.json().get("payload", {})
            orders     = payload.get("Orders", [])
            next_token = payload.get("NextToken")
            all_orders.extend(orders)
            if not next_token:
                break

        print(f"✅ Total orders: {len(all_orders)}", flush=True)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Order ID", "Order Status", "Purchase Date",
            "Buyer Name", "Buyer Email", "Sales Channel",
            "Order Total", "Currency", "Fulfillment Channel",
            "Ship Service Level", "Product Name", "SKU",
            "Quantity", "Item Price",
        ])
        for order in all_orders:
            order_id      = order.get("AmazonOrderId", "")
            order_status  = order.get("OrderStatus", "")
            purchase_date = order.get("PurchaseDate", "")[:10]
            buyer_name    = order.get("BuyerInfo", {}).get("BuyerName", "")
            buyer_email   = order.get("BuyerInfo", {}).get("BuyerEmail", "")
            sales_channel = order.get("SalesChannel", "")
            order_total   = order.get("OrderTotal", {}).get("Amount", "")
            currency      = order.get("OrderTotal", {}).get("CurrencyCode", "")
            fulfillment   = order.get("FulfillmentChannel", "")
            ship_level    = order.get("ShipServiceLevel", "")
            try:
                items = get_amazon_order_items(token, order_id)
            except:
                items = []
            if items:
                for item in items:
                    writer.writerow([
                        order_id, order_status, purchase_date,
                        buyer_name, buyer_email, sales_channel,
                        order_total, currency, fulfillment, ship_level,
                        item.get("Title", ""), item.get("SellerSKU", ""),
                        item.get("QuantityOrdered", ""),
                        item.get("ItemPrice", {}).get("Amount", ""),
                    ])
            else:
                writer.writerow([
                    order_id, order_status, purchase_date,
                    buyer_name, buyer_email, sales_channel,
                    order_total, currency, fulfillment, ship_level,
                    "", "", "", ""
                ])
        output.seek(0)
        filename = f"amazon_orders_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        print("❌ Download error:", e, flush=True)
        return jsonify({"error": str(e)}), 500

@app.route("/debug")
def debug():
    token = AIRTABLE_TOKEN or ""
    return jsonify({
        "token_length":             len(token),
        "token_start":              token[:10] if token else "EMPTY",
        "token_starts_with_pat":    token.startswith("pat"),
        "base_id":                  BASE_ID,
        "customers_table":          bool(CUSTOMERS_TABLE_ID),
        "order_line_items_table":   bool(ORDER_LINE_ITEMS_TABLE_ID),
        "orders_table":             bool(ORDERS_TABLE_ID),
        "french_inventories_table": bool(FRENCH_INVENTORIES_TABLE_ID),
        "amazon_mode":              "PRODUCTION" if AMZ_PRODUCTION else "SANDBOX",
    })

@app.route("/test-customer-search")
def test_customer_search():
    email = request.args.get("email", "")
    phone = request.args.get("phone", "")
    results = {}
    if email:
        records = airtable_search(CUSTOMERS_TABLE_ID, f"{{Mail id}}='{email}'")
        results["email_search"] = [r["fields"].get("Customer Name", "") for r in records]
    if phone:
        records = airtable_search(CUSTOMERS_TABLE_ID, f"{{Contact Number}}='{phone}'")
        results["phone_search"] = [r["fields"].get("Customer Name", "") for r in records]
    if not email and not phone:
        return jsonify({"error": "Provide ?email= or ?phone= parameter"}), 400
    return jsonify(results)

@app.route("/test-airtable-direct")
def test_airtable_direct():
    token = os.getenv("AIRTABLE_TOKEN")
    r = requests.get(
        f"https://api.airtable.com/v0/{BASE_ID}/{CUSTOMERS_TABLE_ID}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"maxRecords": 1}
    )
    return jsonify({
        "status":     r.status_code,
        "response":   r.json(),
        "token_used": token[:15] + "..."
    })

@app.route("/auto-sync", methods=["GET"])
def auto_sync():
    global last_sync_time
    now = time.time()
    if now - last_sync_time < MIN_SYNC_INTERVAL:
        remaining = int(MIN_SYNC_INTERVAL - (now - last_sync_time))
        print(f"⏳ Auto-sync skipped — wait {remaining}s", flush=True)
        return jsonify({"status": "skipped", "next_sync_in": remaining}), 200
    last_sync_time = now
    print("🔔 Auto-sync triggered by UptimeRobot", flush=True)
    thread = threading.Thread(target=sync_amazon_orders_job)
    thread.daemon = True
    thread.start()
    return jsonify({
        "status": "Sync started",
        "mode":   "PRODUCTION" if AMZ_PRODUCTION else "SANDBOX"
    }), 200

@app.route("/shopify-fulfillment", methods=["POST"])
def shopify_fulfillment():
    print("🛍️ Shopify fulfillment webhook received", flush=True)
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400

    order_id     = str(data.get("id", ""))
    order_number = str(data.get("order_number", ""))
    fulfillments = data.get("fulfillments", [])

    if not fulfillments:
        print("⚠️ No fulfillments in payload", flush=True)
        return jsonify({"status": "no fulfillments"}), 200

    fulfilled_at   = fulfillments[0].get("created_at", "")
    fulfilled_date = fulfilled_at[:10] if fulfilled_at else datetime.utcnow().strftime("%Y-%m-%d")
    print(f"🛍️ Order {order_id} | #{order_number} fulfilled at {fulfilled_date}", flush=True)

    records = airtable_search(ORDERS_TABLE_ID, f"{{Order ID}}='{order_id}'")
    if not records:
        records = airtable_search(ORDERS_TABLE_ID, f"{{Order ID}}='{order_number}'")

    if records:
        airtable_update(ORDERS_TABLE_ID, records[0]["id"], {"Ship By": fulfilled_date})
        print(f"✅ Ship By updated to {fulfilled_date}", flush=True)
    else:
        print(f"⚠️ Order not found: {order_id} / #{order_number}", flush=True)

    return jsonify({"status": "ok"}), 200