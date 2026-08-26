# gift_set_description_agent.py
# Standalone Gift Set HTML Description Generator (Shopify-safe)
#
# What it does:
# - Takes brand + product_name + set_items list
# - Generates SEO HTML (allowed tags only)
# - Adds ONE internal brand backlink: /collections/{brand_slug}
# - Strict-sanitizes output to allowed tags only
#
# Usage example:
#   python gift_set_description_agent.py --brand "Guess" \
#     --product-name "Guess Seductive Gift For Women - Seductive EDT 75ML + EDT 15ML + Body Lotion 100ML + Pouch EDT Women Perfume" \
#     --items "Seductive EDT 75ML" "Seductive EDT 15ML" "Body Lotion 100ML" "Pouch"
#
# Requirements:
# - OPENAI_API_KEY in environment
# - pip install openai

from __future__ import annotations

import argparse
import html
import json
import os
import re
from typing import Any, Dict, List, Optional

from openai import OpenAI


# -----------------------------
# Config
# -----------------------------
CONFIG = {
    "default_model": os.getenv("GIFTSET_DESC_MODEL", "gpt-4o-mini"),
    "temperature": 0.3,
    "max_tokens": 950,
    "allowed_tags": {"h2", "h3", "p", "ul", "li", "strong", "a", "br"},
}


# -----------------------------
# Utilities
# -----------------------------
def _brand_slug(brand_name: str) -> str:
    """Make a Shopify-friendly slug from brand name."""
    if not brand_name:
        return "brand"
    s = brand_name.strip().lower()
    s = s.replace("&", "and")
    s = s.replace("'", "")
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "brand"


def _force_exact_h2(html_text: str, product_name: str) -> str:
    """Ensure the first H2 exactly matches product_name."""
    product_name_esc = html.escape(product_name, quote=False)

    # If there is an <h2>...</h2>, replace first one; else prepend.
    if re.search(r"<h2>.*?</h2>", html_text, flags=re.DOTALL | re.IGNORECASE):
        html_text = re.sub(
            r"<h2>.*?</h2>",
            f"<h2>{product_name_esc}</h2>",
            html_text,
            count=1,
            flags=re.DOTALL | re.IGNORECASE,
        )
    else:
        html_text = f"<h2>{product_name_esc}</h2>\n" + html_text
    return html_text


def _ensure_single_brand_link(html_text: str, brand_slug: str, brand_name: str) -> str:
    """
    Enforce exactly ONE brand backlink:
      <p>Discover more from <a href="/collections/{brand_slug}">{brand_name} perfumes</a></p>
    Removes any other <a> tags.
    """
    # Remove all <a ...>...</a> first
    html_text = re.sub(r"<a\b[^>]*>.*?</a>", "", html_text, flags=re.DOTALL | re.IGNORECASE)

    # Remove empty anchors residue spaces
    html_text = re.sub(r"\s{2,}", " ", html_text)

    # Remove any href fragments that might remain (paranoia)
    html_text = re.sub(r'href\s*=\s*"[^"]*"', "", html_text, flags=re.IGNORECASE)

    final_link = (
        f'<p>Discover more from <a href="/collections/{html.escape(brand_slug)}">'
        f"{html.escape(brand_name)} perfumes</a></p>"
    )

    # Remove any existing "Discover more from" lines
    html_text = re.sub(
        r"<p>\s*Discover more from\s*.*?</p>",
        "",
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()

    # Append link exactly once
    if html_text.endswith("</p>") or html_text.endswith("</ul>") or html_text.endswith("</h3>") or html_text.endswith("</h2>"):
        html_text = html_text + "\n" + final_link
    else:
        html_text = html_text + "\n" + final_link

    return html_text


def sanitize_html_strict(html_text: str, allowed_tags: set[str]) -> str:
    """
    Very strict sanitizer:
    - Keeps only allowed tags
    - Removes all attributes EXCEPT: <a href="...">
    - Strips disallowed tags entirely but keeps their inner text
    """
    if not html_text:
        return ""

    # Normalize newlines
    html_text = html_text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove script/style entirely
    html_text = re.sub(r"<(script|style)\b.*?>.*?</\1>", "", html_text, flags=re.DOTALL | re.IGNORECASE)

    # Tokenize tags
    parts = re.split(r"(<[^>]+>)", html_text)
    out: List[str] = []

    tag_re = re.compile(r"^</?\s*([a-zA-Z0-9]+)(.*?)>$", flags=re.DOTALL)

    for part in parts:
        if not part.startswith("<"):
            out.append(part)
            continue

        m = tag_re.match(part.strip())
        if not m:
            continue

        tag = m.group(1).lower()
        attrs = m.group(2) or ""
        is_close = part.strip().startswith("</")

        if tag not in allowed_tags:
            # drop tag but keep text outside tags (already in other parts)
            continue

        if is_close:
            out.append(f"</{tag}>")
            continue

        # Opening/self-closing tags
        if tag == "a":
            # Keep only href attribute, strip everything else.
            href_match = re.search(r'href\s*=\s*"([^"]*)"', attrs, flags=re.IGNORECASE)
            href = href_match.group(1) if href_match else ""
            # Escape href quotes
            href = href.replace('"', "%22")
            out.append(f'<a href="{href}">')
        elif tag == "br":
            out.append("<br>")
        else:
            out.append(f"<{tag}>")

    cleaned = "".join(out)

    # Cleanup: collapse too many blank lines/spaces
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)

    return cleaned.strip()


# -----------------------------
# Prompt
# -----------------------------
GIFTSET_CREATOR_SYSTEM_PROMPT = """
You are an expert SEO copywriter for Shopify perfume gift set listings.

Return ONLY valid HTML (no markdown, no explanations).

ALLOWED TAGS ONLY: <h2>, <h3>, <p>, <ul>, <li>, <strong>, <a>, <br>
No other tags. No inline styles. No emojis.

CRITICAL:
- The <h2> MUST match the provided product_name EXACTLY.
- You MUST list every item from set_items exactly as provided (do not rename, do not change sizes).
- Do NOT mention competitors or other stores.
- Do NOT mention prices, delivery timelines, stock status, or guarantees.
- Avoid medical/therapeutic claims.

MUST FOLLOW THIS STRUCTURE:
1) <h2>{product_name}</h2>

2) <p>Premium intro (2–3 sentences) about the gift set and the brand.</p>

3) <h3>What’s Inside</h3>
   <ul>
     <li>Each item from set_items (exact text)</li>
   </ul>

4) <h3>Fragrance Profile</h3>
   <p>If notes/family are provided, describe them. If not, describe the scent style without inventing exact notes.</p>

5) <h3>Why You’ll Love It</h3>
   <ul>
     <li>3–5 benefits focused on gifting, layering, travel size, convenience.</li>
   </ul>

6) <h3>How To Use</h3>
   <p>Simple steps. If lotion is included, mention applying it first, then spray on pulse points.</p>

7) Do NOT include any links. (Links will be added by code.)
"""


# -----------------------------
# Main generator
# -----------------------------
def generate_gift_set_description(
    product_name: str,
    brand_name: str,
    set_items: List[str],
    fragrance_family: Optional[str] = None,
    notes: Optional[Dict[str, Any]] = None,
    model: str = CONFIG["default_model"],
) -> Dict[str, Any]:
    brand_slug = _brand_slug(brand_name)

    items_clean = [str(x).strip() for x in (set_items or []) if str(x).strip()]
    if not items_clean:
        raise ValueError("set_items is empty. Provide at least 1 item.")

    payload = {
        "product_name": product_name,
        "brand_name": brand_name,
        "brand_slug": brand_slug,
        "set_items": items_clean,
        "fragrance_family": (fragrance_family or "").strip(),
        "notes": notes or {},  # optional: {"top":[...], "heart":[...], "base":[...]}
    }

    client = OpenAI()

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": GIFTSET_CREATOR_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
        temperature=CONFIG["temperature"],
        max_tokens=CONFIG["max_tokens"],
    )

    raw_html = (resp.choices[0].message.content or "").strip()

    # Strict sanitize
    cleaned = sanitize_html_strict(raw_html, CONFIG["allowed_tags"])

    # Enforce exact H2
    cleaned = _force_exact_h2(cleaned, product_name)

    # Enforce exactly ONE internal brand backlink
    cleaned = _ensure_single_brand_link(cleaned, brand_slug, brand_name)

    return {
        "success": True,
        "brand_slug": brand_slug,
        "description_html": cleaned,
        "model_used": model,
        "length": len(cleaned),
    }


def _parse_args():
    p = argparse.ArgumentParser(description="Generate Shopify HTML description for perfume gift sets.")
    p.add_argument("--brand", required=True, help="Brand name (e.g., Guess)")
    p.add_argument("--product-name", required=True, help="Exact product name for H2")
    p.add_argument("--items", nargs="+", required=True, help="Gift set items list (each as its own argument)")
    p.add_argument("--family", default="", help="Optional fragrance family (e.g., Floral, Amber, Fresh)")
    p.add_argument("--model", default=CONFIG["default_model"], help="OpenAI model name")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    result = generate_gift_set_description(
        product_name=args.product_name,
        brand_name=args.brand,
        set_items=args.items,
        fragrance_family=args.family or None,
        notes=None,
        model=args.model,
    )

    print(result["description_html"])