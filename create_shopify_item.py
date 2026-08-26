import os
import requests
from flask import Blueprint, request, jsonify
from airtable import Airtable
import shopify
from typing import List, Dict, Optional
from shopify_utils import _to_number, shopify_graphql  # 🆕 for price updates

import unicodedata
import re

def convert_title_to_image_name(product_title):
    # Normalize Unicode characters
    product_name_clean = unicodedata.normalize('NFKD', product_title)
    product_name_clean = product_name_clean.encode('ascii', 'ignore').decode('ascii')
    
    # Replace common brand variations first
    brand_replacements = {
        "Victor&Rolf": "Viktor_Rolf",
        "Van Cleef & Arpels": "Van_Cleef_Arpels",
        "Dolce & Gabbana": "Dolce_Gabbana",
        "Yves Saint Laurent": "Yves_Saint_Laurent",
        "Jean Paul Gaultier": "Jean_Paul_Gaultier",
        "Salvatore Ferragamo": "Salvatore_Ferragamo",
        "Tonino Lamborghini": "Tonino_Lamborghini"
    }
    
    for old, new in brand_replacements.items():
        product_name_clean = product_name_clean.replace(old, new)
    
    # Handle specific character replacements
    product_name_clean = product_name_clean.replace("'", "_")  # Handle apostrophes
    product_name_clean = product_name_clean.replace("â€™", "_")  # Handle curly apostrophes
    product_name_clean = product_name_clean.replace("Ãª", "e")  # Handle encoding issues
    product_name_clean = product_name_clean.replace("Ã©", "e")
    product_name_clean = product_name_clean.replace("&", "and")  # Replace & with 'and'
    
    # Add underscore between letters and numbers (both directions)
    product_name_clean = re.sub(r'([A-Za-z])(\d)', r'\1_\2', product_name_clean)  # letter then number
    product_name_clean = re.sub(r'(\d)([A-Za-z])', r'\1_\2', product_name_clean)  # number then letter
    
    # Add underscore between different case letters (camelCase to snake_case)
    product_name_clean = re.sub(r'([a-z])([A-Z])', r'\1_\2', product_name_clean)
    
    # Remove or replace problematic characters but keep some
    product_name_clean = re.sub(r'[®™°º"+]+', '', product_name_clean)  # Remove specific symbols
    product_name_clean = re.sub(r'[.,:/()\-]', ' ', product_name_clean)  # Replace these with spaces
    
    # Normalize spaces and convert to underscores
    product_name_clean = re.sub(r'\s+', ' ', product_name_clean).strip()
    product_name_clean = product_name_clean.replace(" ", "_")
    
    # Handle multiple consecutive underscores
    product_name_clean = re.sub(r'_+', '_', product_name_clean)
    
    # Ensure it starts with a letter and remove trailing underscores
    product_name_clean = re.sub(r'^[^A-Za]*', '', product_name_clean)
    product_name_clean = product_name_clean.strip('_')
    
    return product_name_clean


create_shopify_bp = Blueprint("create_shopify_bp", __name__)

# ---------------------------
# CONFIGURATION
# ---------------------------
SHOP = os.environ.get("SHOPIFY_SHOP")
TOKEN = os.environ.get("SHOPIFY_API_TOKEN")
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01")

AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_TABLE_NAME = "French Inventories"

# Initialize Airtable only if credentials exist
airtable = None
if AIRTABLE_BASE_ID and AIRTABLE_API_KEY:
    try:
        airtable = Airtable(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, AIRTABLE_API_KEY)
    except Exception as e:
        print(f"⚠️ Airtable initialization failed: {e}")
else:
    print("⚠️ Airtable credentials not configured")

# ---------------------------
# SHOPIFY SESSION MANAGEMENT
# ---------------------------
def setup_shopify_session():
    """Setup and activate Shopify session"""
    try:
        if not all([SHOP, TOKEN]):
            raise Exception("Shopify credentials not configured")

        # Proper session setup
        shopify.Session.setup(api_key="dummy", secret="dummy")
        session = shopify.Session(f"https://{SHOP}", API_VERSION, TOKEN)
        shopify.ShopifyResource.activate_session(session)
        return True
    except Exception as e:
        print(f"⚠️ Shopify session setup failed: {e}")
        return False


def clear_shopify_session():
    """Clear Shopify session"""
    try:
        shopify.ShopifyResource.clear_session()
    except Exception as e:
        print(f"⚠️ Error clearing Shopify session: {e}")


# ---------------------------
# ENVIRONMENT VALIDATION
# ---------------------------
def validate_environment():
    """Validate that all required environment variables are set"""
    required_vars = {
        "SHOPIFY_SHOP": SHOP,
        "SHOPIFY_API_TOKEN": TOKEN,
        "AIRTABLE_BASE_ID": AIRTABLE_BASE_ID,
        "AIRTABLE_API_KEY": AIRTABLE_API_KEY
    }

    missing = [var for var, value in required_vars.items() if not value]
    if missing:
        raise Exception(f"Missing environment variables: {', '.join(missing)}")

    return True


# ---------------------------
# IMAGE SEARCHER CLASS
# ---------------------------
class ImageSearcher:
    """Class for searching images in Shopify store"""
    @staticmethod
    def search_by_product_name(
        product_name: str,
        limit: int = 10,
        exact_match: bool = False,
        cursor: Optional[str] = None
    ) -> Dict:
        """Search for images by product name using Shopify Files API"""
        if not product_name:
            return {"success": False, "error": "Empty product name", "images": []}

        try:
            if not setup_shopify_session():
                return {"success": False, "error": "Failed to setup Shopify session", "images": []}

            # Clean the product name to match Shopify's filename format:
            # 1. Remove special characters (&, -, ', etc.)
            # 2. Replace multiple spaces with single space
            # 3. Replace spaces with underscores
            """
            import re
            import unicodedata

            # Normalize and clean product name for filename-safe pattern
            product_name_clean = unicodedata.normalize('NFKD', product_name)
            product_name_clean = product_name_clean.encode('ascii', 'ignore').decode('ascii')  # remove non-ASCII like °, é, etc.
            product_name_clean = re.sub(r'[&\-\'"®™°º.,:/()]+', '', product_name_clean)  # remove special symbols
            product_name_clean = re.sub(r'\s+', ' ', product_name_clean)  # normalize multiple spaces
            product_name_clean = product_name_clean.strip()
            product_name_clean = re.sub(r'([A-Za-z])(\d)', r'\1_\2', product_name_clean)  # add underscore between letters and digits
            product_name_clean = product_name_clean.replace(" ", "_")  # replace spaces with underscores

            """

            product_name_clean = convert_title_to_image_name(product_name)
            if exact_match:
                search_pattern = f'filename:"{product_name_clean}"'
            else:
                # Use wildcard to catch variations (like filename1, filename2, etc.)
                search_pattern = f'filename:{product_name_clean}*'

            after_param = f', after: "{cursor}"' if cursor else ""
            query = f"""
            query {{
            files(first: {limit}{after_param}, query: "{search_pattern} AND media_type:IMAGE") {{
                edges {{
                node {{
                    ... on MediaImage {{
                    id
                    alt
                    createdAt
                    updatedAt
                    image {{
                        id
                        url
                        width
                        height
                    }}
                    }}
                }}
                }}
                pageInfo {{
                hasNextPage
                endCursor
                }}
            }}
            }}
            """

            print(f"🔍 Original: {product_name}", flush=True)
            print(f"🔍 Cleaned: {product_name_clean}", flush=True)
            print(f"🔍 Pattern: {search_pattern}", flush=True)
            
            gql = shopify.GraphQL()
            result = gql.execute(query)

            if isinstance(result, str):
                import json
                result = json.loads(result)

            if "errors" in result:
                err_msg = result["errors"][0]["message"] if isinstance(result["errors"], list) else str(result["errors"])
                print(f"⚠️ GraphQL error: {err_msg}", flush=True)
                return {"success": False, "error": err_msg, "images": []}

            data = result.get("data", {}).get("files", {})
            images = [edge["node"] for edge in data.get("edges", []) if edge.get("node")]
            print(f"✅ Found {len(images)} images for: {product_name}", flush=True)

            return {"success": True, "images": images, "count": len(images)}

        except Exception as e:
            print(f"⚠️ Image search error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e), "images": []}
        finally:
            clear_shopify_session()





# ---------------------------
# HELPER FUNCTIONS
# ---------------------------
def _json_headers():
    return {"Content-Type": "application/json", "X-Shopify-Access-Token": TOKEN}


def _rest_url(path: str):
    return f"https://{SHOP}/admin/api/{API_VERSION}/{path}"


def _graphql_url():
    return f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"


def get_linked_image_urls_from_name_field(linked_record_ids, linked_table_name="Image URLs"):
    """Fetch 'Name' field values (the image URLs) from linked records."""
    if not linked_record_ids:
        return []

    urls = []
    try:
        linked_table = Airtable(AIRTABLE_BASE_ID, linked_table_name, AIRTABLE_API_KEY)
        for rec in linked_record_ids:
            rec_id = rec.get("id")
            if not rec_id:
                continue
            try:
                linked_record = linked_table.get(rec_id)
                name_value = linked_record.get("fields", {}).get("Name")
                if isinstance(name_value, str) and name_value.startswith("http"):
                    urls.append(name_value.strip())
            except Exception as e:
                print(f"⚠️ Failed to fetch linked image for {rec_id}: {e}", flush=True)
                continue
    except Exception as e:
        print(f"⚠️ Error accessing linked table {linked_table_name}: {e}", flush=True)

    return urls


def set_metafield(owner_id, namespace, key, mtype, value):
    """Generic helper to set a metafield via GraphQL."""
    try:
        mutation = """
        mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields { id namespace key type value }
            userErrors { field message }
          }
        }
        """
        variables = {
            "metafields": [{
                "ownerId": owner_id,
                "namespace": namespace,
                "key": key,
                "type": mtype,
                "value": str(value)
            }]
        }

        resp = requests.post(_graphql_url(), headers=_json_headers(), json={"query": mutation, "variables": variables})
        result = resp.json()
        print(f"[GQL metafield] {result}", flush=True)
        return result
    except Exception as e:
        print(f"⚠️ Error setting metafield {namespace}.{key}: {e}", flush=True)
        return None


def get_shopify_locations():
    """Get available Shopify locations"""
    resp = requests.get(_rest_url("locations.json"), headers=_json_headers())
    resp.raise_for_status()
    locations = resp.json().get("locations", [])
    if not locations:
        raise Exception("No locations found in Shopify store")
    return locations


# ---------------------------
# MAIN ROUTE
# ---------------------------
@create_shopify_bp.route("/create-shopify-item", methods=["POST"])
def create_shopify_item():
    try:
        validate_environment()

        data = request.get_json(force=True)
        record_id = data.get("record_id")
        record = data.get("fields", {})

        if not record_id or not record:
            return jsonify({"error": "Missing record_id or fields"}), 400

        qty = _to_number(record.get("Qty given in shopify", 0))
        status = "active" if qty > 0 else "draft"

        # ---------------- 1️⃣ Prepare Product ----------------
        product_data = {
            "product": {
                "title": record.get("Product Name", "").strip(),
                "body_html": record.get("ShopifyDesc", ""),
                "vendor": record.get("Brand", ""),
                "product_type": record.get("Type", ""),
                "tags": record.get("Category", ""),
                "status": status,
                "category": "Health & Beauty > Personal Care > Cosmetics > Perfumes & Colognes", 
                "variants": [{
                    "sku": record.get("SKU", ""),
                    "price": str(_to_number(record.get("UAE Price", 0))),
                    "barcode": record.get("Barcode", ""),
                    "weight": _to_number(record.get("Weight", 850)),
                    "weight_unit": "g",
                    "inventory_management": "shopify",
                    "inventory_policy": "deny"
                }],
                "options": [{"name": "Title", "values": ["Default Title"]}],
                "images": []
            }
        }

        # ---------------- 2️⃣ Add Images ----------------
        linked_image_records = record.get("Image URLs", [])
        image_urls = get_linked_image_urls_from_name_field(linked_image_records, linked_table_name="Image URLs")

        if not image_urls:
            product_name = record.get("Product Name", "")
            print(f"🔍 Searching images for: {product_name}", flush=True)
            search_result = ImageSearcher.search_by_product_name(product_name, limit=5)
            if search_result["success"] and search_result["count"] > 0:
                image_urls = [
                    img["image"]["url"] for img in search_result["images"]
                    if img.get("image") and img["image"].get("url")
                ]
                print(f"✅ Found {len(image_urls)} images via search", flush=True)

        for url in image_urls[:10]:
            product_data["product"]["images"].append({"src": url})

        # ---------------- 3️⃣ Create Product ----------------
        resp = requests.post(_rest_url("products.json"), headers=_json_headers(), json=product_data)
        if resp.status_code != 201:
            return jsonify({"error": "Shopify API error", "details": resp.text}), resp.status_code

        result = resp.json()
        product = result.get("product", {})
        shopify_product_id = product.get("id")
        variant = product.get("variants", [{}])[0]
        variant_id = variant.get("id")
        inventory_item_id = variant.get("inventory_item_id")

        variant_gid = f"gid://shopify/ProductVariant/{variant_id}"
        product_gid = f"gid://shopify/Product/{shopify_product_id}"

        print(f"✅ Product created: {shopify_product_id}", flush=True)

        # ---------------- 4️⃣ Inventory ----------------
        location_id = get_shopify_locations()[0]["id"]
        inv_payload = {"location_id": location_id, "inventory_item_id": inventory_item_id, "available": int(qty)}
        inv_resp = requests.post(_rest_url("inventory_levels/set.json"), headers=_json_headers(), json=inv_payload)
        print(f"✅ Inventory updated: {qty}", flush=True)

        # ---------------- 5️⃣ Metafields ----------------
        set_metafield(product_gid, "custom", "size", "single_line_text_field", record.get("Size", ""))
        set_metafield(product_gid, "custom", "brands", "single_line_text_field", record.get("Brand", ""))
        raw_gender = (record.get("Category") or "").strip().lower()
        gender_value = raw_gender if raw_gender in ["male", "female", "unisex"] else "unisex"
        set_metafield(variant_gid, "google_shopping", "age_group", "single_line_text_field", "adult")
        set_metafield(variant_gid, "google_shopping", "condition", "single_line_text_field", "new")
        set_metafield(variant_gid, "google_shopping", "gender", "single_line_text_field", gender_value)
        set_metafield(variant_gid, "google_shopping", "mpn", "single_line_text_field", record.get("SKU", ""))

        # ---------------- 🆕 6️⃣ Update Regional Prices ----------------
        print("=" * 80, flush=True)
        print("STEP 6: Updating regional prices...", flush=True)
        print("=" * 80, flush=True)

        PRICE_LIST_IDS = {
            "UAE": "gid://shopify/PriceList/31168201019",
            "Asia": "gid://shopify/PriceList/31168266555",
            "America": "gid://shopify/PriceList/31168233787",
        }

        prices = {
            "UAE": _to_number(record.get("UAE Price")),
            "Asia": _to_number(record.get("Asia Price")),
            "America": _to_number(record.get("America Price")),
        }

        compare_prices = {
            "UAE": _to_number(record.get("UAE Comparison Price")),
            "Asia": _to_number(record.get("Asia Comparison Price")),
            "America": _to_number(record.get("America Comparison Price")),
        }

        mutation = """
        mutation priceListFixedPricesAdd($priceListId: ID!, $prices: [PriceListPriceInput!]!) {
          priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {
            prices {
              price { amount currencyCode }
              compareAtPrice { amount currencyCode }
              variant { id }
            }
            userErrors { field message }
          }
        }
        """

        for region, price_val in prices.items():
            if not price_val:
                continue
            price_list_id = PRICE_LIST_IDS.get(region)
            compare_val = compare_prices.get(region)
            currency = "AED"  # adjust if needed
            price_input = {
                "variantId": variant_gid,
                "price": {"amount": str(price_val), "currencyCode": currency},
            }
            if compare_val:
                price_input["compareAtPrice"] = {"amount": str(compare_val), "currencyCode": currency}
            variables = {"priceListId": price_list_id, "prices": [price_input]}
            print(f"→ Updating {region} | Price={price_val}", flush=True)
            res = shopify_graphql(mutation, variables)
            print(f"✓ {region} price result: {res}", flush=True)

        # ---------------- 7️⃣ Update Airtable ----------------
        if airtable:
            airtable.update(record_id, {"ShopifyID": str(shopify_product_id), "Create in Shopify": False})
            print(f"✅ Airtable updated: {record_id}", flush=True)

        return jsonify({
            "success": True,
            "shopify_id": shopify_product_id,
            "variant_id": variant_id,
            "prices": prices,
            "inventory": qty
        }), 201

    except Exception as e:
        print(f"❌ Unexpected error: {e}", flush=True)
        return jsonify({"error": str(e)}), 500