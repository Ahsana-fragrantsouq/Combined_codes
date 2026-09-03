"""
order_whatsapp.py
Sends an automatic WhatsApp order-confirmation message via MSG91
when a Shopify order is created.

Add to combined app:
    from order_whatsapp import order_whatsapp_bp
    app.register_blueprint(order_whatsapp_bp)

Required env vars on Render:
    SHOPIFY_WEBHOOK_SECRET
    MSG91_AUTHKEY
    MSG91_INTEGRATED_NUMBER   (WA business number, no '+')
    MSG91_TEMPLATE_NAME       (approved MSG91 WhatsApp template name)

Shopify setup:
    Settings -> Notifications -> Webhooks -> Create webhook
    Event: Order creation
    URL:   https://<your-render-app>/webhook/order-whatsapp
    Format: JSON
"""

import os
import hmac
import hashlib
import base64
import requests
from flask import Blueprint, request, jsonify

order_whatsapp_bp = Blueprint('order_whatsapp', __name__)

SHOPIFY_WEBHOOK_SECRET = os.environ['SHOPIFY_WEBHOOK_SECRET']
MSG91_AUTHKEY = os.environ['MSG91_AUTHKEY']
MSG91_INTEGRATED_NUMBER = os.environ['MSG91_INTEGRATED_NUMBER']
MSG91_TEMPLATE_NAME = os.environ['MSG91_TEMPLATE_NAME']

MSG91_URL = "https://control.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/bulk/"


def verify_shopify_webhook(data, hmac_header):
    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode('utf-8'),
        data,
        hashlib.sha256
    ).digest()
    computed_hmac = base64.b64encode(digest).decode('utf-8')
    return hmac.compare_digest(computed_hmac, hmac_header or '')


def normalize_phone(raw_phone):
    """Normalize to international format without '+'. Adjust country
    fallback logic if you get customers outside UAE."""
    phone = raw_phone.replace('+', '').replace(' ', '').replace('-', '')
    if phone.startswith('00'):
        phone = phone[2:]
    if phone.startswith('0'):
        phone = '971' + phone[1:]
    return phone


def send_whatsapp_order_confirmation(phone, customer_name, order_number, order_total):
    headers = {
        "Content-Type": "application/json",
        "authkey": MSG91_AUTHKEY
    }
    payload = {
        "integrated_number": MSG91_INTEGRATED_NUMBER,
        "content_type": "template",
        "payload": {
            "messaging_product": "whatsapp",
            "type": "template",
            "template": {
                "name": MSG91_TEMPLATE_NAME,
                "language": {"code": "en", "policy": "deterministic"},
                "to_and_components": [
                    {
                        "to": [phone],
                        "components": {
                            "body_1": {"type": "text", "value": customer_name},
                            "body_2": {"type": "text", "value": order_number},
                            "body_3": {"type": "text", "value": str(order_total)}
                        }
                    }
                ]
            }
        }
    }
    resp = requests.post(MSG91_URL, json=payload, headers=headers, timeout=15)
    return resp.status_code, resp.text


@order_whatsapp_bp.route('/webhook/order-whatsapp', methods=['POST'])
def handle_order_created():
    hmac_header = request.headers.get('X-Shopify-Hmac-Sha256')
    if not verify_shopify_webhook(request.get_data(), hmac_header):
        return jsonify({"error": "unauthorized"}), 401

    order = request.get_json()

    phone = (order.get('customer', {}) or {}).get('phone') or order.get('phone')
    if not phone:
        shipping = order.get('shipping_address', {}) or {}
        phone = shipping.get('phone')
    if not phone:
        return jsonify({"status": "skipped", "reason": "no phone"}), 200

    phone = normalize_phone(phone)
    customer_name = (order.get('customer', {}) or {}).get('first_name', 'Customer')
    order_number = order.get('name', str(order.get('order_number', '')))
    order_total = order.get('total_price', '')

    status, resp_text = send_whatsapp_order_confirmation(
        phone, customer_name, order_number, order_total
    )

    return jsonify({"status": "sent", "msg91_status": status, "msg91_response": resp_text}), 200