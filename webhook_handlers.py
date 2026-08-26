from flask import jsonify, request
import os, time
import requests

from shopify_utils import (
    _to_number,
    get_variant_product_and_inventory_by_sku,
    update_variant_default_price,
    update_variant_details,
    update_product_title,
    set_metafield,
    get_primary_location_id,
    set_inventory_absolute,
    update_price_list,  # FIX: was called below but never imported
)

# FIX: combined codes already uses WEBHOOK_SECRET for syncing_my_price.py's
# /airtable-webhook route, and that value is NOT the same secret your
# Airtable "create new product" automation sends for this route. Reusing
# WEBHOOK_SECRET here would 401 every request. Uses its own dedicated env
# var instead — same fix syncing_my_price.py applied to CLIENT_ID/
# CLIENT_SECRET/API_VERSION for the same reason. Set
# CREATE_SHOPIFY_WEBHOOK_SECRET on Render to the secret this specific
# Airtable automation already sends (i.e. the old service's WEBHOOK_SECRET
# value) — no need to change the Airtable automation itself.
WEBHOOK_SECRET = os.environ["CREATE_SHOPIFY_WEBHOOK_SECRET"]
# FIX: was hardcoded to "https://ec207e-a2.myshopify.com" with a separate,
# never-actually-set SHOPIFY_TOKEN env var (confirmed absent on Render —
# only SHOPIFY_API_TOKEN exists). Now reuses the same SHOPIFY_SHOP /
# SHOPIFY_API_TOKEN already configured for this service, same as
# shopify_utils.py and create_shopify_item.py.
SHOPIFY_STORE = f"https://{os.environ['SHOPIFY_SHOP']}"
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_API_TOKEN")

# FIX: get_market_price_lists() below does `global CACHED_PRICE_LISTS` and
# checks `if CACHED_PRICE_LISTS is not None` on its first call — that raised
# NameError unless this module-level default existed.
CACHED_PRICE_LISTS = None

# FIX: STEP 6 referenced this dict but it was never defined anywhere in this
# file. Keys match what handle_airtable_webhook()'s `prices`/`compare_prices`
# dicts use (UAE/Asia/America); values match the market names this file's
# own get_market_price_lists() produces (see CATALOG_TO_MARKET below).
MARKET_NAMES = {
    "UAE": "United Arab Emirates",
    "Asia": "Asia Market",
    "America": "International Market",
}

# -------------------------------------------------------
#  SAFE GRAPHQL CALLER WITH RETRY & RATE-LIMIT HANDLING
# -------------------------------------------------------
def shopify_graphql(query, variables, max_retries=3):
    url = f"{SHOPIFY_STORE}/admin/api/2024-07/graphql.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }

    for attempt in range(max_retries):
        res = requests.post(url, headers=headers, json={"query": query, "variables": variables})

        # Handle rate limit (429) or throttle header near capacity
        if res.status_code == 429 or "X-Shopify-Shop-Api-Call-Limit" in res.headers:
            limit_info = res.headers.get("X-Shopify-Shop-Api-Call-Limit", "")
            print(f"⚠️ Shopify throttle: {limit_info} | attempt {attempt+1}", flush=True)
            wait_time = 2 * (attempt + 1)
            print(f"Waiting {wait_time}s before retry...", flush=True)
            time.sleep(wait_time)
            continue

        try:
            res.raise_for_status()
            return res.json()
        except Exception as err:
            print(f"✗ Shopify request failed (attempt {attempt+1}): {err}", flush=True)
            time.sleep(2 * (attempt + 1))
    raise Exception("❌ Shopify GraphQL failed after retries")
#-------------------------------------------------------
# GET MARKET PRICE LISTS function
#-------------------------------------------------------
# ---------- Markets / Price Lists ----------
def get_market_price_lists():
    global CACHED_PRICE_LISTS
    if CACHED_PRICE_LISTS is not None:
        print("Using cached price lists.", flush=True)
        return CACHED_PRICE_LISTS

    # Query catalogs directly instead of through markets
    CATALOG_QUERY = """
    query ($first: Int!) {
      catalogs(first: $first, type: MARKET) {
        nodes {
          id
          title
          status
          priceList {
            id
            name
            currency
          }
        }
      }
    }
    """
    
    result = shopify_graphql(CATALOG_QUERY, {"first": 20})
    if "data" not in result or "catalogs" not in result["data"]:
        print("ERROR: Could not find data.catalogs in result", flush=True)
        print("Raw result:", result, flush=True)
        return {}

    # Map catalog titles to your market keys
    CATALOG_TO_MARKET = {
        "United Arab Emirates": "United Arab Emirates",
        "Asia Market with 55 rate": "Asia Market",
        "America catlog": "International Market",  # Maps to your "International Market"
    }

    price_lists = {}
    print("\nDEBUG: --- Shopify Market Catalogs/PriceLists ---", flush=True)
    
    for catalog in result["data"]["catalogs"]["nodes"]:
        catalog_title = catalog["title"]
        catalog_status = catalog["status"]
        
        print(f"  Catalog: {catalog_title} (ID: {catalog['id']}, Status: {catalog_status})", flush=True)
        
        # Only process ACTIVE catalogs
        if catalog_status != "ACTIVE":
            print(f"    ⊘ Skipping - not active", flush=True)
            continue
        
        pl = catalog.get("priceList")
        if pl:
            print(f"    PriceList: {pl['name']} (ID: {pl['id']}, Currency: {pl['currency']})", flush=True)
            
            # Map catalog title to market name
            market_name = CATALOG_TO_MARKET.get(catalog_title, catalog_title)
            price_lists[market_name] = {"id": pl["id"], "currency": pl["currency"]}
        else:
            print("    ⊘ No price list attached.", flush=True)

    CACHED_PRICE_LISTS = price_lists
    print("DEBUG: price_lists mapping used for updates:", price_lists, flush=True)
    return price_lists


# -------------------------------------------------------
#  MAIN HANDLER
# -------------------------------------------------------
def handle_airtable_webhook():
    """
    Handles Airtable → Shopify updates.
    Updates variant details, inventory, default price,
    and regional (UAE / Asia / America) price lists safely.
    """
    try:
        # ---- Security ----
        secret = request.headers.get("X-Secret-Token")
        print("Secret header:", secret, flush=True)
        if secret != WEBHOOK_SECRET:
            print("Unauthorized!", flush=True)
            return jsonify({"error": "Unauthorized"}), 401

        # ---- Parse incoming data ----
        data = request.json or {}
        print("=" * 80, flush=True)
        print("WEBHOOK RECEIVED", flush=True)
        print("=" * 80, flush=True)
        print("Received data:", data, flush=True)

        sku = data.get("SKU")
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
        qty_abs = _to_number(data.get("Qty given in shopify"))
        title = data.get("Title")
        barcode = data.get("Barcode")
        size_value = data.get("Size")

        print(f"SKU: {sku}\nPrices: {prices}\nCompare: {compare_prices}\nQty: {qty_abs}", flush=True)
        if not sku:
            return jsonify({"error": "SKU missing"}), 400

        # ---- STEP 1: Find variant / product ----
        print("\n" + "=" * 80)
        print("STEP 1: FINDING VARIANT", flush=True)
        variant_gid, product_gid, variant_num, inventory_item_id = get_variant_product_and_inventory_by_sku(sku)
        if not variant_gid:
            return jsonify({"error": f"Variant with SKU {sku} not found"}), 404
        print(f"✓ variant_gid={variant_gid} | product_gid={product_gid}", flush=True)

        # ---- STEP 2: Update variant & product details ----
        print("\n" + "=" * 80)
        print("STEP 2: UPDATING VARIANT & PRODUCT DETAILS", flush=True)
        if title or barcode:
            update_variant_details(variant_gid, title=title, barcode=barcode)
        if title:
            update_product_title(product_gid, title)

        # ---- STEP 3: Default Shopify price ----
        print("\n" + "=" * 80)
        print("STEP 3: UPDATING DEFAULT PRICE", flush=True)
        if prices.get("UAE") is not None:
            update_variant_default_price(variant_num, prices["UAE"], compare_at_price=compare_prices["UAE"])
            print(f"✓ Default price={prices['UAE']} | compare={compare_prices['UAE']}", flush=True)

        # ---- STEP 4: Size metafield ----
        print("\n" + "=" * 80)
        print("STEP 4: UPDATING METAFIELDS", flush=True)
        if size_value:
            set_metafield(
                owner_id_gid=variant_gid,
                namespace="custom",
                key="size",
                mtype="single_line_text_field",
                value=str(size_value),
            )
            print(f"✓ Metafield size set: {size_value}", flush=True)

        # ---- STEP 5: Inventory ----
        print("\n" + "=" * 80)
        print("STEP 5: UPDATING INVENTORY", flush=True)
        inventory_update = None
        if qty_abs is not None:
            try:
                loc_id = get_primary_location_id()
                if loc_id:
                    inventory_update = set_inventory_absolute(inventory_item_id, loc_id, qty_abs)
                    print(f"✓ Inventory updated to {qty_abs}", flush=True)
                else:
                    print("✗ Primary location not found", flush=True)
            except Exception as inv_err:
                print(f"✗ Inventory error: {inv_err}", flush=True)
        else:
            print("⊘ Skipping inventory (no qty provided)", flush=True)

        # ---- STEP 6: Regional market prices ----
        print("\n" + "=" * 80)
        print("STEP 6: UPDATING MARKET PRICES", flush=True)
        #myedit*/
        price_lists = get_market_price_lists()
        print("Price lists:", price_lists, flush=True)
        #myedit*/
        """PRICE_LIST_IDS = {
            "UAE": "gid://shopify/PriceList/31168201019",
            "Asia": "gid://shopify/PriceList/31168266555",
            "America": "gid://shopify/PriceList/31168233787",
        }
        """
        price_updates = {}  # FIX: was referenced below without ever being initialized
        for market_key, amount in prices.items():
            if amount is None:
                continue
            mname = MARKET_NAMES.get(market_key)
            if not mname or mname not in price_lists:
                print(f"No price list for market {market_key}", flush=True)
                continue
            pl = price_lists[mname]
            # FIX: was `uae_compare_price`, an undefined name — this is the
            # per-market compare-at price already parsed into compare_prices above.
            compare_amt = compare_prices.get(market_key)
            print(f"Updating PL={pl['id']} Market={market_key} price={amount} {pl['currency']} compare_at={compare_amt}", flush=True)
            res = update_price_list(pl["id"], variant_gid, amount, pl["currency"], compare_at_amount=compare_amt)
            price_updates[market_key] = res

        # ---- FINAL RESPONSE ----
        response_data = {
            "status": "success",
            "variant_id": variant_gid,
            "product_id": product_gid,
            "inventory_update": inventory_update,
            "price_list_updates": price_updates,
        }
        print("=" * 80, flush=True)
        print("FINAL RESPONSE:", response_data, flush=True)
        print("=" * 80, flush=True)
        return jsonify(response_data), 200

    except Exception as e:
        import traceback
        print("\n" + "=" * 80, flush=True)
        print("FATAL ERROR:", e, flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500