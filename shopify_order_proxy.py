"""
Proxy endpoint so Airtable's Scripting extension (which runs in a
browser sandbox and can't call Shopify's Admin API directly due to
CORS) can fetch a Shopify order's real order_number through our own
server instead.

Imported as a side effect in app.py (import shopify_order_proxy),
same pattern as the other modules in this project.

Uses the same SHOPIFY_SHOP / SHOPIFY_API_TOKEN already set in Render,
via shopify_utils.py's existing setup.
"""

from flask import jsonify
import requests

from shared import app
from shopify_utils import SHOP, TOKEN, API_VERSION, _json_headers, _rest_url


@app.route("/shopify/order-number/<order_id>", methods=["GET"])
def shopify_order_number(order_id):
    url = _rest_url(f"orders/{order_id}.json")
    try:
        resp = requests.get(url, headers=_json_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        order_number = data.get("order", {}).get("order_number")
    except requests.RequestException as e:
        return _with_cors(jsonify({"error": str(e)})), 502

    return _with_cors(jsonify({"order_number": order_number}))


def _with_cors(response):
    # Airtable's Scripting extension calls this from airtable.com,
    # so it needs an explicit CORS allow header to not be blocked.
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response