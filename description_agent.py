import re
import json
import html
from typing import Optional, Dict, Any, List
from openai import OpenAI, APIError, RateLimitError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

client = OpenAI()

CONFIG = {
    "default_model": "gpt-4o-mini",
    "fallback_model": "gpt-4o",
    "max_retries": 3,
    "allowed_tags": {"h2", "h3", "p", "ul", "li", "strong", "a"},
    "max_notes_per_group": 8,
}

# ---------- OpenAI Safe Call ----------
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((RateLimitError, APIError)),
)
def safe_openai_call(**kwargs):
    return client.chat.completions.create(timeout=90, **kwargs)

# ---------- Slug ----------
def _brand_slug(brand_name: str) -> str:
    if not brand_name:
        return "fragrances"
    slug = brand_name.strip().lower()
    slug = re.sub(r"[&+]", "and", slug)
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug.strip("-") or "fragrances"

# ---------- Notes Processing ----------
def process_notes_input(notes_input: Any) -> List[str]:
    """
    Process notes input from API (string or list).
    Returns cleaned list of notes.
    """
    if notes_input is None:
        return []

    if isinstance(notes_input, list):
        notes_list = []
        for item in notes_input:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    notes_list.append(cleaned.title())
        return notes_list[: CONFIG["max_notes_per_group"]]

    if isinstance(notes_input, str):
        s = notes_input.strip()
        if not s or s.lower() in {"nan", "none", "null", "n/a", "na", ""}:
            return []

        # Split by common delimiters + also split on "and"
        parts = re.split(r"[,\|;/•\n]+|\s+\band\b\s+", s, flags=re.IGNORECASE)

        out: List[str] = []
        for p in parts:
            p = p.strip()
            if not p:
                continue

            p = re.sub(r"\(.*?\)", "", p).strip()
            p = re.sub(r"\s+", " ", p)
            p = p.title()

            if 2 <= len(p) <= 50:
                out.append(p)

        seen = set()
        deduped = []
        for n in out:
            k = n.lower()
            if k not in seen:
                seen.add(k)
                deduped.append(n)

        return deduped[: CONFIG["max_notes_per_group"]]

    return process_notes_input(str(notes_input))

# ---------- Prompts ----------
CREATOR_SYSTEM_PROMPT = """
You are an expert SEO copywriter for Shopify perfume listings.

Return ONLY valid HTML (no markdown, no explanations).

ALLOWED TAGS ONLY: <h2>, <h3>, <p>, <ul>, <li>, <strong>, <a>
No other tags. No inline styles. No emojis.

CRITICAL:
- The <h2> MUST match the provided perfume_name EXACTLY.
- If notes are provided, you MUST include every note at least once (spelling preserved).
- NEVER mention missing notes or say "notes not provided".

MUST FOLLOW THIS STRUCTURE:
1) <h2>{perfume_name}</h2>
2) <p>Intro (2–3 sentences)</p>
3) <h3>The Experience</h3>
   <p>1 short paragraph</p>

IF (and only if) at least one note exists in notes.top / notes.heart / notes.base:
4) <h3>Signature Notes</h3>
   <ul>
     (If notes.top exists) <li><strong>Top Notes:</strong> ...</li>
     (If notes.heart exists) <li><strong>Heart Notes:</strong> ...</li>
     (If notes.base exists) <li><strong>Base Notes:</strong> ...</li>
   </ul>

5) <h3>Perfect For</h3>
   <p>1 short paragraph</p>

6) Final line MUST be:
<p>Discover more from <a href="/collections/{brand_slug}">{brand_name} perfumes</a></p>
"""

VALIDATOR_SYSTEM_PROMPT = """
You are a strict formatter. Return ONLY JSON.

You must:
- Ensure the final HTML follows the required structure
- Ensure the final HTML contains EVERY provided note (case-insensitive match is fine)
- Ensure the final internal link is exactly the required one and is the last line
- Remove any disallowed tags or any mention of missing notes

Return JSON:
{
  "overall_pass": boolean,
  "failures": ["..."],
  "corrected": {"content_html": "..."}
}
"""

# ============================================================
# Hard-enforcement helpers (fix name drift, missing notes, dup H2)
# ============================================================

def _collapse_duplicate_h2(html_text: str) -> str:
    """Keep only the first <h2>...</h2> and remove any additional h2 blocks."""
    if not html_text:
        return ""
    h2s = list(re.finditer(r"(?is)<h2>\s*.*?\s*</h2>", html_text))
    if len(h2s) <= 1:
        return html_text.strip()

    first = h2s[0].group(0)
    remainder = re.sub(r"(?is)<h2>\s*.*?\s*</h2>\s*", "", html_text).strip()
    return (first + "\n" + remainder).strip()


def _force_exact_h2(html_text: str, perfume_name: str) -> str:
    """Ensure the first <h2> equals the exact perfume_name; if no h2, prepend it."""
    html_text = html_text or ""
    desired = f"<h2>{html.escape(perfume_name)}</h2>"
    if re.search(r"(?is)<h2>\s*.*?\s*</h2>", html_text):
        html_text = re.sub(r"(?is)<h2>\s*.*?\s*</h2>", desired, html_text, count=1)
    else:
        html_text = desired + "\n" + html_text
    return html_text.strip()


def _ensure_required_sections(content_html: str) -> str:
    """Ensure The Experience and Perfect For sections exist."""
    content_html = content_html or ""

    if not re.search(r"(?is)<h3>\s*The Experience\s*</h3>", content_html):
        content_html += (
            "\n<h3>The Experience</h3>\n"
            "<p>A refined wearing experience designed to feel polished, modern, and memorable.</p>"
        )

    if not re.search(r"(?is)<h3>\s*Perfect For\s*</h3>", content_html):
        content_html += (
            "\n<h3>Perfect For</h3>\n"
            "<p>Daily elegance, evenings out, and moments when you want a confident signature.</p>"
        )

    return content_html.strip()


def _build_signature_notes_ul(notes_obj: Dict[str, List[str]]) -> str:
    li_parts: List[str] = []
    if notes_obj.get("top"):
        li_parts.append(f"<li><strong>Top Notes:</strong> {', '.join(notes_obj['top'])}</li>")
    if notes_obj.get("heart"):
        li_parts.append(f"<li><strong>Heart Notes:</strong> {', '.join(notes_obj['heart'])}</li>")
    if notes_obj.get("base"):
        li_parts.append(f"<li><strong>Base Notes:</strong> {', '.join(notes_obj['base'])}</li>")

    if not li_parts:
        return ""
    return "<ul>\n" + "\n".join(li_parts) + "\n</ul>"


def _ensure_signature_notes_if_needed(html_text: str, notes_obj: Dict[str, List[str]]) -> str:
    """
    If notes exist, force a canonical Signature Notes section.
    Uses a broader <ul[^>]*> regex to handle attributes the model may add.
    This does NOT rely on the model.
    """
    html_text = html_text or ""

    has_notes = any((notes_obj.get(k) or []) for k in ("top", "heart", "base"))
    if not has_notes:
        # Remove accidental Signature Notes block if any
        html_text = re.sub(
            r"(?is)<h3>\s*Signature Notes\s*</h3>\s*<ul[^>]*>.*?</ul>",
            "",
            html_text
        )
        return html_text.strip()

    ul = _build_signature_notes_ul(notes_obj)
    if not ul:
        return html_text.strip()

    block = "<h3>Signature Notes</h3>\n" + ul

    # Remove any existing Signature Notes block (handles <ul> with or without attributes)
    html_text = re.sub(
        r"(?is)<h3>\s*Signature Notes\s*</h3>\s*<ul[^>]*>.*?</ul>",
        "",
        html_text
    ).strip()

    # Insert before Perfect For if possible, otherwise before internal link, otherwise append
    if re.search(r"(?is)<h3>\s*Perfect For\s*</h3>", html_text):
        html_text = re.sub(
            r"(?is)(<h3>\s*Perfect For\s*</h3>)",
            block + "\n\\1",
            html_text,
            count=1
        )
    elif re.search(r'(?is)<p>\s*Discover\s+more\s+from\s*<a\b', html_text):
        html_text = re.sub(
            r'(?is)(<p>\s*Discover\s+more\s+from\s*<a\b[^>]*>.*?</a>\s*</p>)',
            block + "\n\\1",
            html_text,
            count=1
        )
    else:
        html_text = html_text.rstrip() + "\n" + block

    return html_text.strip()


def _ensure_internal_link_last(html_text: str, brand_slug: str, brand_name: str) -> str:
    """Ensure exactly one internal link paragraph, and it is the last element."""
    html_text = html_text or ""
    canonical = (
        f'<p>Discover more from '
        f'<a href="/collections/{brand_slug}">{html.escape(brand_name)} perfumes</a></p>'
    )

    # Remove any existing discover-more paragraph variants
    html_text = re.sub(
        r'(?is)<p>\s*Discover\s+more\s+from\s*<a\b[^>]*>.*?</a>\s*</p>\s*',
        '',
        html_text
    ).strip()

    return (html_text + "\n" + canonical).strip()

# =========================
# Strict Sanitizer
# =========================

def sanitize_html_strict(content_html: str, perfume_name: str) -> str:
    """
    - Remove script/style blocks
    - Keep ONLY allowed tags
    - Strip ALL attributes except href on <a>
    - Ensure <h2>perfume_name</h2> exists
    """
    if not content_html:
        return (
            f"<h2>{html.escape(perfume_name)}</h2>\n"
            "<p>An exquisite fragrance that captivates the senses with its distinctive character.</p>\n"
            "<h3>The Experience</h3>\n"
            "<p>A polished scent profile designed to feel modern, elegant, and memorable.</p>\n"
            "<h3>Perfect For</h3>\n"
            "<p>Evenings out, special occasions, and everyday confidence.</p>"
        )

    # Strip script/style blocks
    content_html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", content_html)

    allowed = CONFIG["allowed_tags"]

    def _tag_replacer(m: re.Match) -> str:
        full = m.group(0)
        tagname = m.group(1).lower()
        is_closing = full.startswith("</")

        if tagname not in allowed:
            return ""

        if is_closing:
            return f"</{tagname}>"

        if tagname == "a":
            href_match = re.search(r'(?is)\bhref\s*=\s*(".*?"|\'.*?\'|[^\s>]+)', full)
            href_val = href_match.group(1) if href_match else '"/collections/fragrances"'
            if not (href_val.startswith('"') or href_val.startswith("'")):
                href_val = f'"{href_val}"'
            return f"<a href={href_val}>"

        return f"<{tagname}>"

    content_html = re.sub(r"(?is)</?([a-z0-9]+)\b[^>]*>", _tag_replacer, content_html)

    # Ensure correct H2 exists
    escaped_name = html.escape(perfume_name)
    if not re.search(rf"(?is)<h2>\s*{re.escape(escaped_name)}\s*</h2>", content_html):
        content_html = f"<h2>{escaped_name}</h2>\n" + content_html

    content_html = re.sub(r"\n\s*\n+", "\n", content_html).strip()
    return content_html

# ---------- Main Generator Function (API Version) ----------
@retry(
    stop=stop_after_attempt(CONFIG["max_retries"]),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((RateLimitError, APIError)),
)
def generate_description_from_three_note_strings(
    perfume_name: str,
    brand_name: Optional[str],
    top_notes: Any,
    middle_notes: Any,
    base_notes: Any,
    model: str = CONFIG["default_model"],
    debug: bool = False,
) -> str:
    brand_display = (brand_name or "").strip() or "Fragrances"
    brand_slug = _brand_slug(brand_display)

    notes_obj = {
        "top":   process_notes_input(top_notes),
        "heart": process_notes_input(middle_notes),
        "base":  process_notes_input(base_notes),
    }
    has_notes = any(len(v) > 0 for v in notes_obj.values())

    if debug:
        print("=" * 60)
        print("Perfume :", perfume_name)
        print("Brand   :", brand_display, "| Slug:", brand_slug)
        print("Notes   :", notes_obj)
        print("Has notes:", has_notes)
        print("=" * 60)

    facts = {
        "perfume_name": perfume_name,
        "brand_name":   brand_display,
        "brand_slug":   brand_slug,
        "notes":        notes_obj,
        "has_notes":    has_notes,
        "output_rule":  "Return ONLY HTML using allowed tags. No markdown. No explanations.",
    }

    try:
        # Step 1: Create
        creator_response = safe_openai_call(
            model=model,
            messages=[
                {"role": "system", "content": CREATOR_SYSTEM_PROMPT},
                {"role": "user",   "content": json.dumps(facts, ensure_ascii=False, indent=2)},
            ],
            temperature=0.3,
            max_tokens=900,
        )
        creator_html = creator_response.choices[0].message.content or ""

        if debug:
            print("--- Creator HTML (raw) ---")
            print(creator_html[:800])

        # Step 2: Validate / correct via LLM
        validator_response = safe_openai_call(
            model=model,
            messages=[
                {"role": "system", "content": VALIDATOR_SYSTEM_PROMPT},
                {"role": "user",   "content": json.dumps(
                    {"facts": facts, "original_html": creator_html},
                    ensure_ascii=False, indent=2
                )},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=900,
        )

        validator_data = json.loads(validator_response.choices[0].message.content or "{}")
        final_html = (validator_data.get("corrected") or {}).get("content_html") or creator_html

        if debug and validator_data.get("failures"):
            print("Validator failures:", validator_data["failures"])

    except Exception as e:
        if debug:
            print("Generation error:", str(e))

        # Minimal safe fallback
        final_html = (
            f"<h2>{html.escape(perfume_name)}</h2>\n"
            f"<p>An exquisite fragrance by {html.escape(brand_display)} "
            f"with a captivating, refined presence.</p>\n"
            "<h3>The Experience</h3>\n"
            "<p>It opens with a confident impression, then settles into a smooth, "
            "lasting trail designed for modern wear.</p>\n"
            "<h3>Perfect For</h3>\n"
            "<p>Evenings out, special occasions, and everyday confidence.</p>\n"
        )

    # ----------------------------------------------------------------
    # HARD ENFORCEMENT — order matters:
    #   1. Sanitize first  → strip all rogue attributes so regexes below
    #      work on clean, attribute-free tags (e.g. <ul> not <ul class="…">)
    #   2. Enforce structure sections
    #   3. Fix / de-duplicate <h2>
    #   4. Inject canonical Signature Notes block (now safe to regex-match)
    #   5. Force internal link to be the very last line
    # ----------------------------------------------------------------

    # 1. Sanitize first
    final_html = sanitize_html_strict(final_html, perfume_name)

    # 2. Ensure required structural sections exist
    final_html = _ensure_required_sections(final_html)

    # 3. Force exact h2 and remove duplicates
    final_html = _force_exact_h2(final_html, perfume_name)
    final_html = _collapse_duplicate_h2(final_html)

    # 4. Inject / rebuild Signature Notes from notes_obj (not from model output)
    final_html = _ensure_signature_notes_if_needed(final_html, notes_obj)

    # 5. Force internal link last
    final_html = _ensure_internal_link_last(final_html, brand_slug, brand_display)

    # Final whitespace cleanup
    final_html = re.sub(r"\n\s*\n+", "\n", final_html).strip()

    if debug:
        print("--- Final HTML (after enforcement) ---")
        print(final_html)

    return final_html


# ---------- API Helper Function ----------
def generate_description_for_api(
    perfume_name: str,
    brand_name: str = None,
    top_notes: Any = None,
    middle_notes: Any = None,
    base_notes: Any = None,
    model: str = CONFIG["default_model"],
) -> Dict[str, Any]:
    try:
        description_html = generate_description_from_three_note_strings(
            perfume_name=perfume_name,
            brand_name=brand_name,
            top_notes=top_notes,
            middle_notes=middle_notes,
            base_notes=base_notes,
            model=model,
            debug=False,
        )
        return {
            "success":      True,
            "description":  description_html,
            "perfume_name": perfume_name,
            "brand_name":   (brand_name or "Fragrances"),
            "length":       len(description_html),
            "model_used":   model,
        }
    except Exception as e:
        return {
            "success":              False,
            "error":                str(e),
            "perfume_name":         perfume_name,
            "fallback_description": (
                f"<h2>{html.escape(perfume_name)}</h2>"
                "<p>An exquisite fragrance with a captivating presence.</p>"
            ),
        }