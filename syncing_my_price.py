"""
syncing_my_price.py — Section 8: Syncing My Price
(Airtable → Shopify: price, inventory, metafields)
Split out of the original combined app.py. Code below is unchanged from the
merged version; only the file location and these import lines changed.

NOTE: the requested filename was "syncing-my-price.py" but Python module names
cannot contain hyphens (import syncing-my-price is invalid syntax), so this is
named syncing_my_price.py instead. Route paths and behavior are unchanged.
"""
import os
import time
from flask import request, jsonify
import requests

from shared import app
from delivery_tracker import SHOPIFY_STORE, CLIENT_ID, CLIENT_SECRET

# NOTE — collisions resolved while merging this section in:
#
# 1. CORRECTED: originally this section reused Section 1's CLIENT_ID /
#    CLIENT_SECRET globals on the assumption they held the same value (since
#    both scripts read from env vars of the same name: SHOPIFY_CLIENT_ID /
#    SHOPIFY_CLIENT_SECRET). That assumption was WRONG — each old standalone
#    Render service had its own independently-configured values for those
#    names. Testing showed the token minted via Section 1's credentials only
#    carries read_analytics/read_orders/read_reports scope — no product or
#    inventory access — which crashed the GraphQL calls below. Section 8 now
#    uses its own dedicated PRICE_SYNC_CLIENT_ID / PRICE_SYNC_CLIENT_SECRET env
#    vars so it authenticates as whichever Shopify custom app actually has
#    product write access, independent of Section 1's app.
#
# 2. API_VERSION: this script also defined a global named API_VERSION, but with
#    a DIFFERENT default ("2024-07") than Section 1's API_VERSION ("2024-04"),
#    which Section 1 and Section 7 both rely on. Redeclaring it here would have
#    silently changed the Shopify API version used elsewhere in the file. Kept
#    as a separate PRICE_SYNC_API_VERSION global, with its OWN env var
#    (PRICE_SYNC_SHOPIFY_API_VERSION) after discovering the shared
#    SHOPIFY_API_VERSION var was already set to 2024-04 on this service, which
#    would have silently overridden Section 8's needed 2024-07 default too.
#
# 3. SHOP: reuses the existing SHOPIFY_STORE global (same full-domain value)
#    instead of introducing a third differently-named "shop domain" variable,
#    falling back to a dedicated SHOPIFY_SHOP env var only if set.
#
# 4. Root route "/" collided with Section 1's index() at "/" — moved to
#    /price-sync/health.

PRICE_SYNC_SHOP         = os.getenv("SHOPIFY_SHOP") or SHOPIFY_STORE
PRICE_SYNC_WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PRICE_SYNC_CLIENT_ID     = os.getenv("PRICE_SYNC_CLIENT_ID") or CLIENT_ID
PRICE_SYNC_CLIENT_SECRET = os.getenv("PRICE_SYNC_CLIENT_SECRET") or CLIENT_SECRET
# NOTE: falls back to the shared SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET (same
# ones Section 1 uses) if PRICE_SYNC_CLIENT_ID / PRICE_SYNC_CLIENT_SECRET aren't
# set separately. IMPORTANT — this fallback only works correctly if whichever
# Shopify app SHOPIFY_CLIENT_ID points to actually has product/inventory write
# scopes. Testing earlier showed the original Section 1 app (airtable-sync-4,
# client id 3b558a7f...) only carries read_analytics/read_orders/read_reports —
# no product access — while the reinstalled "session in slack" app (client id
# 68607b4b...) has the broader scopes this section needs. If SHOPIFY_CLIENT_ID
# still points at the narrow-scope app, this will fail again with a token that
# lacks product/inventory permissions (a different error than "app not found",
# but still broken) — set PRICE_SYNC_CLIENT_ID/PRICE_SYNC_CLIENT_SECRET
# explicitly to override if so.
# NOTE: intentionally NOT reading the shared "SHOPIFY_API_VERSION" env var here.
# Combined_codes already has SHOPIFY_API_VERSION=2024-04 set for Section 1, and
# since env vars are shared process-wide, Section 8 would have silently received
# "2024-04" too instead of the "2024-07" it needs for catalogs/price-list/
# metafieldsSet GraphQL support. Uses its own dedicated env var instead.
PRICE_SYNC_API_VERSION  = os.getenv("PRICE_SYNC_SHOPIFY_API_VERSION", "2024-07")

print("🚀 Section 8 — Syncing My Price starting...", flush=True)

# ---------- TOKEN CACHE (Section 8) ----------
_price_sync_token = None
_price_sync_token_time = 0

def get_shopify_access_token():
    global _price_sync_token, _price_sync_token_time

    if _price_sync_token and time.time() - _price_sync_token_time < 3000:
        print("🔁 Using cached Shopify token", flush=True)
        return _price_sync_token

    print("🔐 Requesting Shopify access token...", flush=True)

    url = f"https://{PRICE_SYNC_SHOP}/admin/oauth/access_token"

    payload = {
        "client_id": PRICE_SYNC_CLIENT_ID,
        "client_secret": PRICE_SYNC_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }

    res = requests.post(url, json=payload)
    print("🔁 Token raw response:", res.text[:500], flush=True)

    try:
        data = res.json()
    except ValueError:
        raise Exception(
            f"❌ Shopify token endpoint returned non-JSON (status {res.status_code}). "
            f"This usually means PRICE_SYNC_CLIENT_ID/PRICE_SYNC_CLIENT_SECRET are wrong "
            f"or the app no longer exists. First 300 chars: {res.text[:300]!r}"
        )
    if not data.get("access_token"):
        raise Exception("❌ Token failed")

    _price_sync_token = data["access_token"]
    _price_sync_token_time = time.time()

    print("✅ Token received", flush=True)
    return _price_sync_token

# ---------- HELPERS (Section 8) ----------
def _json_headers():
    return {
        "X-Shopify-Access-Token": get_shopify_access_token(),
        "Content-Type": "application/json",
    }

def _graphql_url():
    return f"https://{PRICE_SYNC_SHOP}/admin/api/{PRICE_SYNC_API_VERSION}/graphql.json"

def _rest_url(path):
    return f"https://{PRICE_SYNC_SHOP}/admin/api/{PRICE_SYNC_API_VERSION}/{path}"

def _to_number(x):
    try:
        return float(x) if x not in (None, "") else None
    except:
        return None

# ---------- MARKET (Section 8) ----------
MARKET_NAMES = {
    "UAE": "United Arab Emirates",
    "Asia": "Asia Market with 55 rate",
    "America": "America catlog",
}

# ---------- GRAPHQL (Section 8) ----------
def shopify_graphql(query, variables=None):
    resp = requests.post(
        _graphql_url(),
        headers=_json_headers(),
        json={"query": query, "variables": variables},
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("errors"):
        print("❌ GraphQL errors:", result["errors"], flush=True)
        raise RuntimeError(f"Shopify GraphQL error: {result['errors']}")
    if result.get("data") is None:
        print("❌ GraphQL returned no data:", result, flush=True)
        raise RuntimeError(f"Shopify GraphQL returned no data: {result}")
    return result

# ---------- PRICE LIST CACHE (Section 8) ----------
CACHED_PRICE_LISTS = None

def get_market_price_lists():
    global CACHED_PRICE_LISTS

    if CACHED_PRICE_LISTS:
        return CACHED_PRICE_LISTS

    QUERY = """
    query {
      catalogs(first: 20, type: MARKET) {
        nodes {
          title
          status
          priceList { id currency }
        }
      }
    }
    """

    res = shopify_graphql(QUERY)
    price_lists = {}

    for c in res.get("data", {}).get("catalogs", {}).get("nodes", []):
        if c.get("status") == "ACTIVE" and c.get("priceList"):
            price_lists[c["title"]] = {
                "id": c["priceList"]["id"],
                "currency": c["priceList"]["currency"],
            }

    print("📊 Price lists:", price_lists, flush=True)
    CACHED_PRICE_LISTS = price_lists
    return price_lists

# ---------- VARIANT (Section 8) ----------
def get_variant_product_and_inventory_by_sku(sku):
    QUERY = """
    query ($q: String!) {
      productVariants(first: 1, query: $q) {
        nodes { id product { id } }
      }
    }
    """

    res = shopify_graphql(QUERY, {"q": f"sku:{sku}"})
    nodes = res.get("data", {}).get("productVariants", {}).get("nodes", [])

    if not nodes:
        return None, None, None, None

    variant_gid = nodes[0]["id"]
    product_gid = nodes[0]["product"]["id"]
    variant_id = variant_gid.split("/")[-1]

    r = requests.get(_rest_url(f"variants/{variant_id}.json"), headers=_json_headers())
    r.raise_for_status()

    inventory_item_id = r.json()["variant"]["inventory_item_id"]

    return variant_gid, product_gid, variant_id, inventory_item_id

# ---------- PRICE (Section 8) ----------
def update_variant_default_price(variant_id, price, compare_price=None):
    payload = {"variant": {"id": int(variant_id), "price": str(price)}}

    if compare_price is not None:
        payload["variant"]["compare_at_price"] = str(compare_price)

    print("💲 Updating default price →", payload, flush=True)

    requests.put(
        _rest_url(f"variants/{variant_id}.json"),
        headers=_json_headers(),
        json=payload,
    ).raise_for_status()

def update_price_list(price_list_id, variant_gid, price, currency, compare_price=None):
    price_input = {
        "variantId": variant_gid,
        "price": {"amount": str(price), "currencyCode": currency},
    }

    if compare_price is not None:
        price_input["compareAtPrice"] = {
            "amount": str(compare_price),
            "currencyCode": currency,
        }

    MUTATION = """
    mutation ($pl: ID!, $prices: [PriceListPriceInput!]!) {
      priceListFixedPricesAdd(priceListId: $pl, prices: $prices) {
        userErrors { message }
      }
    }
    """

    shopify_graphql(MUTATION, {"pl": price_list_id, "prices": [price_input]})

# ---------- INVENTORY (Section 8) ----------
def get_primary_location_id():
    r = requests.get(_rest_url("locations.json"), headers=_json_headers())
    r.raise_for_status()
    return r.json()["locations"][0]["id"]

def set_inventory_absolute(inventory_item_id, location_id, quantity):
    print("📦 Updating inventory:", quantity, flush=True)
    requests.post(
        _rest_url("inventory_levels/set.json"),
        headers=_json_headers(),
        json={
            "inventory_item_id": int(inventory_item_id),
            "location_id": int(location_id),
            "available": int(quantity),
        },
    ).raise_for_status()

# ---------- TITLE / BARCODE (Section 8) ----------
def update_variant_details(variant_gid, title=None, barcode=None):
    if not (title or barcode):
        return

    var_num = variant_gid.split("/")[-1]
    url = _rest_url(f"variants/{var_num}.json")

    payload = {"variant": {"id": int(var_num)}}
    if title:
        payload["variant"]["title"] = title
    if barcode:
        payload["variant"]["barcode"] = barcode

    print("✏ Updating variant details:", payload, flush=True)
    requests.put(url, headers=_json_headers(), json=payload).raise_for_status()

def update_product_title(product_gid, title):
    pid = product_gid.split("/")[-1]
    url = _rest_url(f"products/{pid}.json")

    payload = {"product": {"id": int(pid), "title": title}}

    print("✏ Updating product title:", payload, flush=True)
    requests.put(url, headers=_json_headers(), json=payload).raise_for_status()

# ---------- METAFIELD (Section 8) ----------
def set_metafield(owner_id_gid, namespace, key, mtype, value):
    MUT = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        userErrors { message }
      }
    }
    """

    variables = {
        "metafields": [{
            "ownerId": owner_id_gid,
            "namespace": namespace,
            "key": key,
            "type": mtype,
            "value": str(value)
        }]
    }

    print("🧩 Setting metafield:", namespace, key, value, flush=True)
    shopify_graphql(MUT, variables)

# ---------- ROUTES (Section 8) ----------
@app.route("/price-sync/health", methods=["GET"])
# NOTE: renamed from / to /price-sync/health to avoid conflict with Section 1's index route
def price_sync_home():
    return "✅ Airtable → Shopify Sync is running", 200

@app.route("/airtable-webhook", methods=["POST"])
def airtable_webhook():

    if (request.headers.get("X-Secret-Token") or "").strip() != PRICE_SYNC_WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    print("📦 Payload:", data, flush=True)

    sku = data.get("SKU")
    title = data.get("Title")
    barcode = data.get("Barcode")
    size_value = data.get("Size")

    prices = {
        "UAE": _to_number(data.get("UAE price")),
        "Asia": _to_number(data.get("Asia Price")),
        "America": _to_number(data.get("America Price")),
    }

    compare_prices = {
        "UAE": _to_number(data.get("UAE Comparison Price")),
        "Asia": _to_number(data.get("Asia Comparison Price")),
        "America": _to_number(data.get("America Comparison Price")),
    }

    qty = _to_number(data.get("Qty given in shopify"))

    if not sku:
        return jsonify({"error": "SKU missing"}), 400

    variant_gid, product_gid, variant_id, inventory_item_id = get_variant_product_and_inventory_by_sku(sku)

    if not variant_gid:
        return jsonify({"error": "Variant not found"}), 404

    # ---- TITLE / BARCODE ----
    if title or barcode:
        update_variant_details(variant_gid, title, barcode)

    if title:
        update_product_title(product_gid, title)

    # ---- SIZE (PRODUCT METAFIELD) ----
    if size_value is not None and str(size_value).strip():
        set_metafield(
            product_gid,        # ✅ PRODUCT metafield
            "custom",
            "size",
            "single_line_text_field",
            size_value
        )

    # ---- PRICE ----
    if prices["UAE"] is not None:
        update_variant_default_price(variant_id, prices["UAE"], compare_prices["UAE"])

    # ---- INVENTORY ----
    if qty is not None:
        loc = get_primary_location_id()
        set_inventory_absolute(inventory_item_id, loc, qty)

    # ---- PRICE LISTS ----
    price_lists = get_market_price_lists()

    for market, price in prices.items():
        if price is None:
            continue

        pl = price_lists.get(MARKET_NAMES.get(market))
        if not pl:
            continue

        update_price_list(pl["id"], variant_gid, price, pl["currency"], compare_prices.get(market))

    print("🎉 SYNC COMPLETE", flush=True)
    return jsonify({"status": "success"}), 200