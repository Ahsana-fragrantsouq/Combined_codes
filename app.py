"""
app.py — entry point. Imports the shared Flask `app` instance, then imports
every section module (which register their routes on that same `app` object
as a side effect of being imported), then runs the app.

Render's start command should stay: gunicorn app:app --bind 0.0.0.0:$PORT
"""
import os

from shared import app

import delivery_tracker
import amazon_sync
import trendyol_sync
import abandoned_cart
import session_report
import danabooks_airtable
import shopify_sync
import syncing_my_price

# ════════════════════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)