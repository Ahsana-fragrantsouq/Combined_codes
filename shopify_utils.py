import os
import requests

SHOP = os.environ["SHOPIFY_SHOP"]
TOKEN = os.environ["SHOPIFY_API_TOKEN"]
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-07")
PREFERRED_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")

MARKET_NAMES = {
    "UAE": "United Arab Emirates",
    "Asia": "Asia Market",
    "America": "America & Australia Market",
}

CACHED_PRICE_LISTS = None
CACHED_PRIMARY_LOCATION_ID = None


def _json_headers():
    return {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}


def _graphql_url():
    return f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"


def _rest_url(path: str):
    return f"https://{SHOP}/admin/api/{API_VERSION}/{path}"


def _to_number(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return x
    s = str(x).strip()
    if not s:
        return None
    try:
        return float(s) if "." in s else int(s)
    except Exception:
        return None


def shopify_graphql(query, variables=None):
    url = _graphql_url()
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    print(f"[GQL] POST {url} Vars: {variables}", flush=True)
    resp = requests.post(url, headers=_json_headers(), json=payload)
    print("[GQL] Status:", resp.status_code, flush=True)
    print("[GQL] Body:", resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()

# ---------- Markets / Price Lists ----------
def get_market_price_lists():
    global CACHED_PRICE_LISTS
    if CACHED_PRICE_LISTS is not None:
        print("Using cached price lists.", flush=True)
        return CACHED_PRICE_LISTS

    MARKET_QUERY = """
    query ($first: Int!) {
      markets(first: $first) {
        nodes {
          id
          name
          catalogs(first: 10) {
            nodes {
              id
              priceList {
                id
                name
                currency
              }
            }
          }
        }
      }
    }
    """
    result = shopify_graphql(MARKET_QUERY, {"first": 20})
    if "data" not in result or "markets" not in result["data"]:
        print("ERROR: Could not find data.markets in result", flush=True)
        print("Raw result:", result, flush=True)
        return {}

    price_lists = {}
    print("\nDEBUG: --- Shopify Market Catalogs/PriceLists ---", flush=True)
    for market in result["data"]["markets"]["nodes"]:
        mname = market["name"]
        for catalog in market["catalogs"]["nodes"]:
            pl = catalog.get("priceList")
            print(f"  Market: {mname} | Catalog: {catalog.get('id')}", flush=True)
            if pl:
                print(f"    PriceList: {pl['name']} (ID: {pl['id']}, Currency: {pl['currency']})", flush=True)
                price_lists[mname] = {"id": pl["id"], "currency": pl["currency"]}
            else:
                print("    No price list attached.", flush=True)

    CACHED_PRICE_LISTS = price_lists
    print("DEBUG: price_lists mapping used for updates:", price_lists, flush=True)
    return price_lists


def get_variant_product_and_inventory_by_sku(sku):
    GET_VARIANT_QUERY = """
    query ($sku: String!) {
      productVariants(first: 1, query: $sku) {
        nodes { id sku product { id } }
      }
    }
    """
    res = shopify_graphql(GET_VARIANT_QUERY, {"sku": sku})
    nodes = res.get("data", {}).get("productVariants", {}).get("nodes", [])
    if not nodes:
        print("No variant found for SKU:", sku, flush=True)
        return None, None, None, None

    variant_gid = nodes[0]["id"]
    product_gid = nodes[0]["product"]["id"]
    variant_num = variant_gid.split("/")[-1]

    # REST fetch inventory item
    url = _rest_url(f"variants/{variant_num}.json")
    r = requests.get(url, headers=_json_headers())
    print("[REST] GET variant:", r.status_code, r.text, flush=True)
    r.raise_for_status()
    inventory_item_id = r.json()["variant"]["inventory_item_id"]
    return variant_gid, product_gid, variant_num, inventory_item_id


def update_variant_default_price(variant_id_num, price, compare_at_price=None):
    url = _rest_url(f"variants/{variant_id_num}.json")
    variant_data = {"id": int(variant_id_num), "price": str(price)}
    if compare_at_price is not None:
        variant_data["compare_at_price"] = str(compare_at_price)
    payload = {"variant": variant_data}
    print(f"[REST] PUT default price {url} payload={payload}", flush=True)
    resp = requests.put(url, headers=_json_headers(), json=payload)
    print("[REST] default price resp:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()


def update_variant_details(variant_gid, title=None, barcode=None):
    if not (title or barcode):
        return None
    var_num = variant_gid.split("/")[-1]
    url = _rest_url(f"variants/{var_num}.json")
    vdata = {"id": int(var_num)}
    if title:
        vdata["title"] = title
    if barcode:
        vdata["barcode"] = barcode
    payload = {"variant": vdata}
    resp = requests.put(url, headers=_json_headers(), json=payload)
    print("[REST] variant details resp:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()


def update_product_title(product_gid, new_title):
    pid = product_gid.split("/")[-1]
    url = _rest_url(f"products/{pid}.json")
    payload = {"product": {"id": int(pid), "title": new_title}}
    resp = requests.put(url, headers=_json_headers(), json=payload)
    print("[REST] product title resp:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()


def set_metafield(owner_id_gid, namespace, key, mtype, value):
    MUT = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        metafields { id namespace key type value }
        userErrors { field message }
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
    res = shopify_graphql(MUT, variables)
    return res


def get_primary_location_id():
    global CACHED_PRIMARY_LOCATION_ID
    if PREFERRED_LOCATION_ID:
        return PREFERRED_LOCATION_ID
    if CACHED_PRIMARY_LOCATION_ID:
        return CACHED_PRIMARY_LOCATION_ID

    url = _rest_url("locations.json")
    r = requests.get(url, headers=_json_headers())
    r.raise_for_status()
    locs = r.json().get("locations", [])
    primary = next((l for l in locs if l.get("primary")), None)
    chosen = primary or locs[0]
    CACHED_PRIMARY_LOCATION_ID = str(chosen["id"])
    return CACHED_PRIMARY_LOCATION_ID


def set_inventory_absolute(inventory_item_id, location_id, quantity):
    url = _rest_url("inventory_levels/set.json")
    payload = {
        "inventory_item_id": int(inventory_item_id),
        "location_id": int(location_id),
        "available": int(quantity)
    }
    resp = requests.post(url, headers=_json_headers(), json=payload)
    print("[REST] inventory set resp:", resp.status_code, resp.text, flush=True)
    resp.raise_for_status()
    return resp.json()


def update_price_list(price_list_id, variant_gid, price_amount, currency, compare_at_amount=None):
    MUT = """
    mutation priceListFixedPricesUpdate(
      $priceListId: ID!,
      $pricesToAdd: [PriceListPriceInput!]!,
      $variantIdsToDelete: [ID!]!
    ) {
      priceListFixedPricesUpdate(
        priceListId: $priceListId,
        pricesToAdd: $pricesToAdd,
        variantIdsToDelete: $variantIdsToDelete
      ) {
        userErrors { field message }
      }
    }
    """
    price_input = {
        "variantId": variant_gid,
        "price": {"amount": str(price_amount), "currencyCode": currency}
    }
    if compare_at_amount is not None:
        price_input["compareAtPrice"] = {"amount": str(compare_at_amount), "currencyCode": currency}

    variables = {
        "priceListId": price_list_id,
        "pricesToAdd": [price_input],
        "variantIdsToDelete": []
    }
    res = shopify_graphql(MUT, variables)
    return res

def get_catalog_price_lists():
    """
    Fetch all catalogs directly (bypassing markets)
    and map by catalog name.
    """
    global CACHED_PRICE_LISTS
    if CACHED_PRICE_LISTS is not None:
        print("Using cached catalog price lists.", flush=True)
        return CACHED_PRICE_LISTS

    CATALOG_QUERY = """
    query {
      catalogs(first: 10) {
        nodes {
          id
          handle
          priceList {
            id
            name
            currency
          }
        }
      }
    }
    """

    print("Fetching catalogs & price lists...", flush=True)
    result = shopify_graphql(CATALOG_QUERY)
    catalogs = result.get("data", {}).get("catalogs", {}).get("nodes", [])
    if not catalogs:
        print("⚠️ No catalogs returned from Shopify GraphQL", flush=True)
        return {}

    price_lists = {}
    for cat in catalogs:
        cname = (cat.get("handle") or "").strip()
        pl = cat.get("priceList")
        if not pl:
            continue
        pl_id = pl["id"]
        currency = pl["currency"]

        price_lists[cname] = {"id": pl_id, "currency": currency}
        print(f"✅ Catalog '{cname}' → PriceList {pl_id} ({currency})", flush=True)

    print("✅ Final catalog → priceList mapping:", price_lists, flush=True)
    CACHED_PRICE_LISTS = price_lists
    return price_lists

def update_price_list_fixed(variant_gid, prices, compare_at_price=None):
    """
    Updates fixed prices for the given variant across your known markets.
    Uses Shopify's priceListFixedPricesAdd mutation.
    """
    print("================================================================================")
    print("STEP 6: UPDATING MARKET PRICES")
    print("================================================================================")

    # ✅ Direct mapping to your known price list IDs
    PRICE_LIST_IDS = {
        "UAE": "gid://shopify/PriceList/31168201019",       # United Arab Emirates
        "Asia": "gid://shopify/PriceList/31168266555",      # Asia Market with 55 rate
        "America": "gid://shopify/PriceList/31168233787",   # America catalog
    }

    for market, price_info in prices.items():
        amount = price_info.get("amount")
        currency = price_info.get("currency")
        if not amount or not currency:
            print(f"⊘ Missing price data for {market}, skipping...", flush=True)
            continue

        price_list_id = PRICE_LIST_IDS.get(market)
        if not price_list_id:
            print(f"⊘ No price list ID configured for {market}, skipping...", flush=True)
            continue

        mutation = """
        mutation priceListFixedPricesAdd(
          $priceListId: ID!,
          $prices: [PriceListFixedPriceInput!]!
        ) {
          priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {
            prices {
              price {
                amount
                currencyCode
              }
              variant {
                id
              }
            }
            userErrors {
              field
              message
            }
          }
        }
        """

        variables = {
            "priceListId": price_list_id,
            "prices": [
                {
                    "variantId": variant_gid,
                    "price": {
                        "amount": str(amount),
                        "currencyCode": currency,
                    },
                    "compareAtPrice": {
                        "amount": str(compare_at_price) if compare_at_price else None,
                        "currencyCode": currency,
                    },
                }
            ],
        }

        print(f"→ Updating {market} | PL={price_list_id} | Price={amount} {currency}", flush=True)
        res = shopify_graphql(mutation, variables)
        print("✓ Price update result:", res, flush=True)