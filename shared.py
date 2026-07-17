"""
shared.py — the single Flask `app` instance and the constants used across
multiple section modules. Every other module in this project imports `app`
from here instead of creating its own Flask() instance, so all routes still
register on ONE running app — matching how the original combined app.py behaved.
This file is new; everything else is the original code, unchanged, split by section.
"""
import os
import sys
import logging
from collections import defaultdict
from flask import Flask
import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(line_buffering=True)

load_dotenv()
requests.adapters.DEFAULT_RETRIES = 3

# ── Logging ───────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ════════════════════════════════════════════════════════════════════════════════════
# SHARED CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════════
AIRTABLE_URL    = "https://api.airtable.com/v0"
REQUEST_TIMEOUT = 30