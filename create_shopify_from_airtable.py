# create_shopify_from_airtable.py

# NOTE — route rename: the original standalone app.py used POST
# /airtable-webhook for the "create new product" webhook. That path is
# already taken in combined codes by syncing_my_price.py (Section 8's
# price/inventory sync webhook). To avoid silently shadowing that route,
# this module's webhook is namespaced to
# /create-shopify-item/airtable-webhook instead. Update the Airtable
# automation that used to call the old service's /airtable-webhook to
# point at this new path.

from flask import request, jsonify
import logging

from shared import app

from description_agent import (
    generate_description_from_three_note_strings,
    generate_description_for_api,
    CONFIG,
)
from gift_set_description_agent import generate_gift_set_description
from webhook_handlers import handle_airtable_webhook
from create_shopify_item import create_shopify_bp

# ✅ Register the create-shopify-item blueprint on the shared app
app.register_blueprint(create_shopify_bp)

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)


# ---------- Description generation endpoint ----------
@app.route("/generate", methods=["POST"])
def generate_description():
    data = request.get_json(silent=True) or {}
    print("DEBUG incoming JSON =", data)

    # Required field
    perfume_name = data.get("perfume_name")
    if not perfume_name:
        return jsonify({"error": "perfume_name is required"}), 400

    # Optional fields
    brand_name = data.get("brand_name")
    top_notes = data.get("top_notes")
    middle_notes = data.get("middle_notes")
    base_notes = data.get("base_notes")
    gift_items_list = data.get("gift_items_list")  # list of strings

    model = data.get("model", CONFIG["default_model"])

    # -----------------------------
    # GIFT SET vs PERFUME ROUTER
    # -----------------------------
    if "gift set" in perfume_name.lower():
        if not gift_items_list or not isinstance(gift_items_list, list):
            return jsonify({
                "error": "gift_items_list (array) is required for gift set products"
            }), 400

        result = generate_gift_set_description(
            product_name=perfume_name,
            brand_name=brand_name,
            set_items=gift_items_list,
            model=model
        )
    else:
        result = generate_description_for_api(
            perfume_name=perfume_name,
            brand_name=brand_name,
            top_notes=top_notes,
            middle_notes=middle_notes,
            base_notes=base_notes,
            model=model
        )

    # -----------------------------
    # RESPONSE
    # -----------------------------
    if result.get("success"):
        return jsonify({
            "success": True,
            "description": result.get("description") or result.get("description_html"),
            "metadata": {
                "perfume_name": perfume_name,
                "brand_name": brand_name,
                "length": result.get("length"),
                "model_used": result.get("model_used")
            }
        })
    else:
        return jsonify({
            "success": False,
            "error": result.get("error"),
            "fallback_description": result.get("fallback_description", "")
        }), 500


# ---------- Airtable webhook endpoint ----------
# Renamed from /airtable-webhook (see NOTE at top of file) to avoid
# colliding with syncing_my_price.py's existing /airtable-webhook route.
@app.route("/create-shopify-item/airtable-webhook", methods=["POST"])
def airtable_webhook_route():
    return handle_airtable_webhook()