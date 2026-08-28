"""
slack_reply.py — Shopify order status → Slack thread replies.

"""
import os
import hmac
import hashlib
import base64
import threading
import time
import requests
from flask import request, jsonify
from datetime import datetime, timedelta
import re

from shared import app

SLACK_BOT_TOKEN = os.getenv("SLACK_REPLY_SLACK_BOT_TOKEN")
SHOPIFY_SHOP = os.getenv("SHOPIFY_SHOP_NAME")         # e.g. fragrantsouq.myshopify.com
SHOPIFY_ACCESS_TOKEN = os.getenv("SLACK_REPLY_SHOPIFY_ACCESS_TOKEN")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")
SLACK_REPLY_WEBHOOK_SECRET = os.getenv("SLACK_REPLY_SHOPIFY_WEBHOOK_SECRET")


# ---------------- SECURITY ----------------
# Named distinctly (not verify_webhook / shopint_verify_webhook) to avoid
# colliding with the same-purpose, differently-scoped functions already in
# abandoned_cart.py and shopify_sync.py.
def slack_reply_verify_webhook(data, hmac_header):
    if not hmac_header or not SLACK_REPLY_WEBHOOK_SECRET:
        return False
    digest = hmac.new(
        SLACK_REPLY_WEBHOOK_SECRET.encode("utf-8"),
        data,
        hashlib.sha256
    ).digest()
    computed_hmac = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed_hmac, hmac_header)

CHANNELS_TO_SEARCH = [
    "C0A02M2VCTB",
    "C0A068PHZMY"
]

order_tracking = {}

# --------------------------------------------------
def is_new_order_message(text, order_number):
    if not text:
        return False

    text_lower = text.lower().strip()
    blacklist = ["fulfilled", "tracking", "report", "generated", "payment"]
    if any(word in text_lower for word in blacklist):
        return False

    match = re.search(r"\bst\.order\s+#?(\d+)\b", text_lower)
    return bool(match and match.group(1) == order_number)


# --------------------------------------------------
# Wraps any Slack API call with retry-on-ratelimit, honoring Slack's Retry-After header.
def slack_request(method, url, headers, max_retries=6, **kwargs):
    for attempt in range(max_retries):
        r = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        try:
            data = r.json()
        except ValueError:
            data = {"ok": False, "error": "invalid_json_response"}

        if data.get("error") == "ratelimited":
            retry_after = int(r.headers.get("Retry-After", 2))
            print(f"⏳ Rate limited by Slack — retrying in {retry_after}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_after)
            continue

        return data

    return data  # give up after max_retries, return last (still rate-limited) response


# --------------------------------------------------
# Fetches each channel's history ONCE (paginated) and builds an order_number -> (ts, channel)
# lookup. Used by sync-all so we don't hit Slack once per order — that's what was causing
# rate limiting when syncing hundreds of orders.
def build_order_message_index(max_pages_per_channel=25, page_size=200):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    index = {}

    for channel_id in CHANNELS_TO_SEARCH:
        cursor = None
        pages = 0
        print(f"📚 Indexing channel {channel_id}...")

        while pages < max_pages_per_channel:
            params = {"channel": channel_id, "limit": page_size}
            if cursor:
                params["cursor"] = cursor

            data = slack_request(
                "GET",
                "https://slack.com/api/conversations.history",
                headers,
                params=params
            )

            if not data.get("ok"):
                print(f"❌ Slack API error while indexing {channel_id}:", data)
                break

            for msg in data.get("messages", []):
                text = msg.get("text", "")
                match = re.search(r"\bst\.order\s+#?(\d+)\b", text.lower())
                if match:
                    order_number = match.group(1)
                    if is_new_order_message(text, order_number) and order_number not in index:
                        index[order_number] = (msg["ts"], channel_id)

            cursor = data.get("response_metadata", {}).get("next_cursor")
            pages += 1
            if not cursor:
                break

            time.sleep(1.2)  # pace pagination requests to stay well under Slack's rate limit

        print(f"📚 Indexed {pages} page(s) from {channel_id}")

    print(f"📚 Order message index built: {len(index)} orders found")
    return index


# --------------------------------------------------
def find_new_order_message(order_number):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

    for channel_id in CHANNELS_TO_SEARCH:
        try:
            print(f"🔍 Searching order {order_number} in channel {channel_id}")

            data = slack_request(
                "GET",
                "https://slack.com/api/conversations.history",
                headers,
                params={"channel": channel_id, "limit": 100}
            )

            if not data.get("ok"):
                print("❌ Slack API error:", data)
                continue

            for msg in reversed(data.get("messages", [])):
                if is_new_order_message(msg.get("text", ""), order_number):
                    print(f"✅ Found new order message in {channel_id} at ts={msg['ts']}")
                    return msg["ts"], channel_id

        except Exception as e:
            print(f"🔥 Slack search exception: {e}")

    print(f"⚠️ Order {order_number} not found in Slack")
    return None, None


# --------------------------------------------------
def post_thread_message(channel, thread_ts, text):
    print(f"📤 Posting to Slack thread {thread_ts}: {text}")

    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    payload = {
        "channel": channel,
        "thread_ts": thread_ts,
        "text": text
    }

    data = slack_request(
        "POST",
        "https://slack.com/api/chat.postMessage",
        headers,
        json=payload
    )

    print("📨 Slack response:", data)
    return data.get("ok", False)


# --------------------------------------------------
PAYMENT_LABELS = {
    "pending": "⏳ Payment Pending",
    "paid": "✅ Payment Paid",
    "authorized": "🔒 Payment Authorized",
    "voided": "❌ Payment Voided",
    "refunded": "↩️ Payment Refunded"
}

FULFILLMENT_LABELS = {
    "fulfilled": "🚀 Fulfilled",
    "partially_fulfilled": "📤 Partially Fulfilled",
    "unfulfilled": "📦 Unfulfilled",
    "on_hold": "⏸️ On Hold",
    "in_progress": "⚙️ In Progress"
}


def payment_message(status):
    return PAYMENT_LABELS.get(status, f"💳 Payment {status}")


def fulfillment_message(status, tracking=None, courier=None):
    msg = FULFILLMENT_LABELS.get(status, f"📦 {status}")

    details = []
    if tracking:
        details.append(f"Tracking: {tracking}")
    if courier:
        details.append(f"Courier: {courier}")

    if details:
        msg += f" ({', '.join(details)})"

    return msg


# --------------------------------------------------
# Fetches a thread's existing replies and figures out which payment/fulfillment
# status was already posted, by matching each reply's leading text against our
# known label sets. Used to "seed" the in-memory cache after a restart so we
# never re-post a status that's already sitting in the thread.
def seed_track_from_slack_thread(channel, thread_ts):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

    data = slack_request(
        "GET",
        "https://slack.com/api/conversations.replies",
        headers,
        params={"channel": channel, "ts": thread_ts, "limit": 200}
    )

    payment = None
    fulfillment = None

    if not data.get("ok"):
        print(f"⚠️ Could not fetch thread replies to seed status ({channel}/{thread_ts}):", data)
        return payment, fulfillment

    for msg in data.get("messages", []):
        text = msg.get("text", "")
        for status, label in PAYMENT_LABELS.items():
            if text.startswith(label):
                payment = status
        for status, label in FULFILLMENT_LABELS.items():
            if text.startswith(label):
                fulfillment = status

    if payment or fulfillment:
        print(f"🌱 Seeded from existing thread — payment={payment}, fulfillment={fulfillment}")

    return payment, fulfillment


# --------------------------------------------------
# Shared logic used by both the live webhook and /slack-reply/sync-all.
# Returns a small dict describing what happened, for reporting back in sync results.
def process_order_status(order, event_id=None, prebuilt_index=None):
    order_number = str(order.get("name", "")).replace("#", "").strip()
    result = {"order": order_number, "payment_posted": False, "fulfillment_posted": False, "note": ""}

    if not order_number:
        result["note"] = "order number missing"
        return result

    if order_number not in order_tracking:
        if prebuilt_index is not None:
            ts, channel = prebuilt_index.get(order_number, (None, None))
        else:
            print("🧠 Order not cached. Searching Slack...")
            ts, channel = find_new_order_message(order_number)

        if not ts:
            result["note"] = "Slack thread not found"
            return result

        seeded_payment, seeded_fulfillment = seed_track_from_slack_thread(channel, ts)

        order_tracking[order_number] = {
            "ts": ts,
            "channel": channel,
            "payment": seeded_payment,
            "fulfillment": seeded_fulfillment,
            "last_event_id": None
        }
        print("✅ Order cached:", order_tracking[order_number])

    track = order_tracking[order_number]

    # Webhook dedupe only applies to live webhook calls (event_id present + matches last seen)
    if event_id and track.get("last_event_id") == event_id:
        result["note"] = "duplicate webhook ignored"
        return result

    if event_id:
        track["last_event_id"] = event_id

    time_now = datetime.now().strftime("%I:%M %p")

    payment_status = (order.get("financial_status") or "").lower().strip()
    if payment_status and payment_status != track["payment"]:
        msg = f"{payment_message(payment_status)} • {time_now}"
        if post_thread_message(track["channel"], track["ts"], msg):
            track["payment"] = payment_status
            result["payment_posted"] = True
            time.sleep(0.6)  # brief pause to avoid Slack's high-volume message throttle

    fulfillment_status = (order.get("fulfillment_status") or "").lower().strip()
    tracking_no = None
    courier = None
    if order.get("fulfillments"):
        f = order["fulfillments"][-1]
        tracking_no = f.get("tracking_number")
        courier = f.get("tracking_company")

    if fulfillment_status and fulfillment_status != track["fulfillment"]:
        msg = f"{fulfillment_message(fulfillment_status, tracking_no, courier)} • {time_now}"
        if post_thread_message(track["channel"], track["ts"], msg):
            track["fulfillment"] = fulfillment_status
            result["fulfillment_posted"] = True

    if not result["payment_posted"] and not result["fulfillment_posted"]:
        result["note"] = "already up to date"

    return result


# --------------------------------------------------
@app.route("/webhook/shopify", methods=["POST"])
def shopify_webhook():
    print("🔔 Webhook received")

    raw_data = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    if not slack_reply_verify_webhook(raw_data, hmac_header):
        print("🚫 Webhook HMAC verification failed")
        return "Unauthorized", 401

    data = request.get_json(force=True)
    print("📦 Payload:", data)

    order = data.get("order", data)
    order_number = str(order.get("name", "")).replace("#", "").strip()
    print("🆔 Order Number:", order_number)

    if not order_number:
        print("❌ Order number missing")
        return jsonify({"error": "order number missing"}), 400

    event_id = request.headers.get("X-Shopify-Webhook-Id")
    print("🔑 Webhook Event ID:", event_id)

    result = process_order_status(order, event_id=event_id)
    print("🎯 Webhook processed:", result, "\n")

    return jsonify({"ok": True, "result": result}), 200


# --------------------------------------------------
def fetch_recent_shopify_orders(days):
    if not SHOPIFY_SHOP or not SHOPIFY_ACCESS_TOKEN:
        raise RuntimeError("SHOPIFY_SHOP_NAME / SLACK_REPLY_SHOPIFY_ACCESS_TOKEN env vars not set")

    since = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
    url = f"https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
    params = {"status": "any", "updated_at_min": since, "limit": 250}

    orders = []
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        payload = r.json()
        orders.extend(payload.get("orders", []))

        # handle Shopify Link-header pagination
        link = r.headers.get("Link", "")
        next_url = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part[part.find("<") + 1: part.find(">")]
        url = next_url
        params = {}  # next_url already has query params embedded

    return orders


# --------------------------------------------------
# Manual backfill: re-checks recent orders and posts any status that's missing/stale
# in Slack — e.g. after an outage, a missing_scope error, or a restart that cleared
# the in-memory order_tracking cache.
#
# Runs in a background thread so the HTTP request returns immediately — a long
# synchronous sync was holding the request open until gunicorn's worker timeout
# killed it mid-run. Poll /sync-status to check progress/results.

sync_state = {"running": False, "days": None, "results": None, "error": None}
sync_lock = threading.Lock()


def run_sync(days):
    print(f"🔄 Sync-all started for last {days} day(s)")
    try:
        orders = fetch_recent_shopify_orders(days)
        print(f"📦 {len(orders)} orders fetched from Shopify")

        message_index = build_order_message_index()

        results = []
        for order in orders:
            try:
                result = process_order_status(order, prebuilt_index=message_index)
                results.append(result)
                if result.get("payment_posted") or result.get("fulfillment_posted"):
                    time.sleep(0.8)  # pace posts so Slack doesn't throttle/hide messages from this app
            except Exception as e:
                order_number = str(order.get("name", "")).replace("#", "").strip()
                print(f"🔥 Error syncing order {order_number}: {e}")
                results.append({"order": order_number, "note": f"error: {e}"})

        posted = [r for r in results if r.get("payment_posted") or r.get("fulfillment_posted")]
        not_found = [r for r in results if r.get("note") == "Slack thread not found"]
        print(f"🎯 Sync-all complete: {len(posted)} updated, {len(not_found)} threads not found\n")

        with sync_lock:
            sync_state["results"] = {
                "orders_checked": len(orders),
                "updated_count": len(posted),
                "not_found_count": len(not_found),
                "results": results
            }
            sync_state["error"] = None
    except Exception as e:
        print("🔥 Sync-all failed:", e)
        with sync_lock:
            sync_state["error"] = str(e)
    finally:
        with sync_lock:
            sync_state["running"] = False


# Usage: GET/POST /slack-reply/sync-all?days=2  -> kicks off the sync in the background
# NOTE: renamed from /sync-all to /slack-reply/sync-all to avoid conflict with
# amazon_sync.py's existing /sync-all route.
@app.route("/slack-reply/sync-all", methods=["GET", "POST"])
def sync_all():
    days = int(request.args.get("days", 2))

    with sync_lock:
        if sync_state["running"]:
            return jsonify({"ok": False, "note": "a sync is already running"}), 409
        sync_state["running"] = True
        sync_state["days"] = days
        sync_state["results"] = None
        sync_state["error"] = None

    thread = threading.Thread(target=run_sync, args=(days,), daemon=True)
    thread.start()

    return jsonify({
        "ok": True,
        "note": f"sync started for last {days} day(s) — check /sync-status for progress"
    }), 202


# Usage: GET /sync-status -> poll this to see if the sync is done and view results
@app.route("/sync-status")
def sync_status():
    with sync_lock:
        return jsonify(dict(sync_state)), 200


# --------------------------------------------------
# NOTE: renamed from /health to /slack-reply/health to avoid conflict with
# abandoned_cart.py's existing /health route.
@app.route("/slack-reply/health")
def slack_reply_health():
    print("❤️ Health check called")
    return jsonify({
        "status": "ok",
        "tracked_orders": len(order_tracking)
    })