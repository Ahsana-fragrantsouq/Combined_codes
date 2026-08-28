"""
Slack thread reply -> MSG91 WhatsApp forwarder.

When someone replies inside a "MSG Message Notification" thread in
#msg-notification, this fetches the original notification message
from Slack (via conversations.replies) and extracts the customer's
number from its text ("Message received from <number> in MSG: ..."),
then forwards the reply to that number via MSG91's session-message API.

No Airtable lookup table needed - the number is parsed directly out
of the thread's parent message.

Imported as a side effect in app.py (import slackreply_to_whatsapp),
same pattern as the other modules in this project - registers its
route directly on the shared app object, no Blueprint needed.

Required environment variables (set these in Render):
    SLACK_SIGNING_SECRET      - from Slack app -> Basic Information
    SLACK_REPLY_BOT_TOKEN     - from "Session report (shopify)" app -> OAuth & Permissions
                                 (starts with xoxb-). Named distinctly because a
                                 different SLACK_BOT_TOKEN already exists for another app.
    MSG91_AUTHKEY             - from MSG91 -> Settings -> API keys
    MSG91_INTEGRATED_NUMBER   - your MSG91 WhatsApp number, e.g. 971541836101
"""

import os
import re
import hmac
import hashlib
import time
import requests
from flask import request, jsonify

from shared import app

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_BOT_TOKEN = os.environ["SLACK_REPLY_BOT_TOKEN"]
MSG91_AUTHKEY = os.environ["MSG91_AUTHKEY"]
MSG91_INTEGRATED_NUMBER = os.environ["MSG91_INTEGRATED_NUMBER"]

# Matches "Message received from 971524633389 ," in the notification text
CUSTOMER_NUMBER_PATTERN = re.compile(r"Message received from (\+?\d{8,15})\s*,")

# in-memory dedupe: Slack redelivers events on timeout, this avoids
# double-sending the same reply. Resets on redeploy - fine for this scale.
_seen_event_ids = set()


def verify_slack_signature(req) -> bool:
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "0")
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except ValueError:
        return False

    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    my_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    slack_signature = req.headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(my_signature, slack_signature)


def lookup_customer_number(channel: str, thread_ts: str):
    """Fetch the thread's parent (original notification) message and
    pull the customer number back out of its text."""
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    params = {"channel": channel, "ts": thread_ts, "limit": 1}
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok") or not data.get("messages"):
        print(f"[slack_reply] conversations.replies failed: {data}")
        return None

    parent_text = data["messages"][0].get("text", "")
    print(f"[slack_reply] parent message text: {parent_text!r}")
    match = CUSTOMER_NUMBER_PATTERN.search(parent_text)
    if match:
        print(f"[slack_reply] extracted customer number: {match.group(1)}")
    else:
        print(f"[slack_reply] regex did not match parent text")
    return match.group(1) if match else None


def send_whatsapp_text(recipient_number: str, text: str):
    print(f"[slack_reply] sending to MSG91: recipient={recipient_number} text={text!r}")
    url = "https://api.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/"
    headers = {
        "Authkey": MSG91_AUTHKEY,
        "accept": "application/json",
        "content-type": "application/json",
    }
    body = {
        "integrated_number": MSG91_INTEGRATED_NUMBER,
        "recipient_number": recipient_number,
        "content_type": "text",
        "text": text,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    print(f"[slack_reply] MSG91 response status={resp.status_code} body={resp.text}")
    resp.raise_for_status()
    return resp.json()


@app.route("/slack/events", methods=["POST"])
def slack_events():
    if not verify_slack_signature(request):
        print("[slack_reply] signature verification FAILED")
        return "invalid signature", 403

    payload = request.get_json(silent=True) or {}

    # One-time handshake Slack sends when you first save the Request URL
    if payload.get("type") == "url_verification":
        print("[slack_reply] handling url_verification challenge")
        return jsonify({"challenge": payload.get("challenge")})

    event_id = payload.get("event_id")
    if event_id:
        if event_id in _seen_event_ids:
            print(f"[slack_reply] duplicate event_id={event_id}, skipping")
            return jsonify({"ok": True}), 200
        _seen_event_ids.add(event_id)

    event = payload.get("event", {})
    print(f"[slack_reply] received event: type={event.get('type')} subtype={event.get('subtype')} "
          f"thread_ts={event.get('thread_ts')} ts={event.get('ts')} bot_id={event.get('bot_id')} "
          f"text={event.get('text')!r}")

    is_thread_reply = (
        event.get("type") == "message"
        and event.get("thread_ts")
        and event.get("thread_ts") != event.get("ts")
        and not event.get("bot_id")       # ignore Airtable's own bot message
        and event.get("subtype") is None  # ignore edits/deletes/channel-joins
    )
    print(f"[slack_reply] is_thread_reply={is_thread_reply}")

    if is_thread_reply:
        channel = event.get("channel", "")
        thread_ts = event["thread_ts"]
        reply_text = event.get("text", "")

        customer_number = lookup_customer_number(channel, thread_ts)
        if customer_number and reply_text:
            try:
                send_whatsapp_text(customer_number, reply_text)
                print(f"[slack_reply] SUCCESS: forwarded reply to {customer_number}")
            except requests.RequestException as e:
                print(f"[slack_reply] MSG91 send failed: {e}")
        elif not customer_number:
            print(f"[slack_reply] Could not extract customer number from thread_ts={thread_ts}")

    return jsonify({"ok": True}), 200