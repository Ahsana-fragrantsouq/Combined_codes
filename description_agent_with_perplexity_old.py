import re
import json
import html
import pandas as pd
from typing import Optional, Dict, Any, List
from openai import OpenAI, APIError, RateLimitError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

client = OpenAI()

CONFIG = {
    "default_model": "gpt-4o-mini",
    "fallback_model": "gpt-4o",
    "max_retries": 3,
    "allowed_tags": {"h2", "h3", "p", "ul", "li", "strong", "a"},
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
    return slug.strip("-")

# ---------- Notes parsing ----------
def _split_notes(text: str) -> List[str]:
    # split by commas / semicolons / pipes / bullets
    parts = re.split(r"[,\|;/•\n]+", text)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        p = re.sub(r"\(.*?\)", "", p).strip()
        p = re.sub(r"\s+", " ", p)
        p = p.title()
        if 2 <= len(p) <= 50:
            out.append(p)
    # de-dupe preserve order
    seen = set()
    deduped = []
    for n in out:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            deduped.append(n)
    return deduped[:8]

def parse_notes_cell(notes_cell: Any) -> Dict[str, Any]:
    """
    Returns: {"top":[], "heart":[], "base":[], "sources":[]}
    """
    if notes_cell is None:
        return {"top": [], "heart": [], "base": [], "sources": []}

    s = str(notes_cell).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return {"top": [], "heart": [], "base": [], "sources": []}

    # 1) Try JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return {
                "top": _split_notes(" , ".join(obj.get("top", []) if isinstance(obj.get("top", []), list) else [str(obj.get("top", ""))])),
                "heart": _split_notes(" , ".join(obj.get("heart", []) if isinstance(obj.get("heart", []), list) else [str(obj.get("heart", ""))])),
                "base": _split_notes(" , ".join(obj.get("base", []) if isinstance(obj.get("base", []), list) else [str(obj.get("base", ""))])),
                "sources": obj.get("sources", []) if isinstance(obj.get("sources", []), list) else [],
            }
    except Exception:
        pass

    # 2) Labeled sections
    # Accept: top|opening, heart|middle, base|drydown
    def grab(label_patterns: List[str]) -> str:
        for pat in label_patterns:
            m = re.search(pat, s, flags=re.I | re.S)
            if m:
                return m.group(1).strip()
        return ""

    top_txt = grab([
        r"(?:^|\n)\s*top\s*notes?\s*[:\-]\s*(.*?)(?=\n\s*(?:heart|middle|base|drydown)\s*notes?\s*[:\-]|\Z)",
        r"(?:^|\n)\s*opening\s*[:\-]\s*(.*?)(?=\n\s*(?:heart|middle|base|drydown)\s*[:\-]|\Z)",
    ])
    heart_txt = grab([
        r"(?:^|\n)\s*(?:heart|middle)\s*notes?\s*[:\-]\s*(.*?)(?=\n\s*(?:top|opening|base|drydown)\s*notes?\s*[:\-]|\Z)",
    ])
    base_txt = grab([
        r"(?:^|\n)\s*(?:base|drydown)\s*notes?\s*[:\-]\s*(.*?)(?=\n\s*(?:top|opening|heart|middle)\s*notes?\s*[:\-]|\Z)",
    ])

    if top_txt or heart_txt or base_txt:
        return {
            "top": _split_notes(top_txt),
            "heart": _split_notes(heart_txt),
            "base": _split_notes(base_txt),
            "sources": [],
        }

    # 3) Fallback: treat as a general list -> heart
    return {"top": [], "heart": _split_notes(s), "base": [], "sources": []}

def split_notes_string(value: Any) -> List[str]:
    """
    Notes cell -> list of notes.
    Accepts comma / semicolon / pipe / newline separated strings.
    """
    if value is None:
        return []
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return []

    parts = re.split(r"[,\|;/•\n]+", s)
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

    # de-dupe preserve order
    seen = set()
    deduped = []
    for n in out:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            deduped.append(n)

    return deduped[:8]

# ---------- Prompts ----------
CREATOR_SYSTEM_PROMPT = """
You are an expert SEO copywriter for Shopify perfume listings.

GOAL:
Create elegant, factual, and SEO-optimized perfume descriptions in valid HTML format.

RULES:
- Use ONLY the notes provided. Never invent notes.
- Use semantic HTML only: <h2>, <h3>, <p>, <ul>, <li>, <strong>, <a>.
- Do NOT include inline styles, scripts, emojis, or special characters.

STRUCTURE:
1. <h2>Product Name</h2>
2. Intro paragraph: 2 natural sentences introducing the perfume.
3. <h3>The Experience</h3>
4. <h3>Signature Notes</h3>
   - Use <ul>.
   - Only include categories that actually have notes.
   - Must include at least one <li>.
   - If all note arrays are empty, include ONE neutral bullet that does not claim specific notes.
5. <h3>Perfect For</h3>
6. Add exactly one internal link at the end:
   <p>Discover more from <a href="/collections/{slug}">{Brand} perfumes</a></p>
"""

VALIDATOR_SYSTEM_PROMPT = """
Validate and correct the provided HTML perfume description.

VALIDATION RULES:
1. Must not invent fragrance notes beyond the provided notes.
2. Required order:
   H2 → Intro → The Experience → Signature Notes → Perfect For → Internal Link
3. Signature Notes must contain at least one <li> and no placeholders like "None".
4. Exactly one internal link at the end:
   <p>Discover more from <a href="/collections/{slug}">{Brand} perfumes</a></p>
5. Remove invalid tags and ensure proper nesting.

OUTPUT:
Return JSON:
{
  "overall_pass": bool,
  "failures": [],
  "corrected": {"content_html": "..."}
}
"""

# ---------- HTML sanitize (light, tag-only) ----------
def _sanitize_html_basic(html_text: str, perfume_name: str) -> str:
    html_text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html_text)

    def keep_allowed(m):
        tag = m.group(1).lower()
        return m.group(0) if tag in CONFIG["allowed_tags"] else ""

    html_text = re.sub(r"(?i)</?([a-z0-9]+)([^>]*)>", keep_allowed, html_text)

    if not re.search(rf"(?is)^\s*<h2>\s*{re.escape(perfume_name)}\s*</h2>", html_text):
        html_text = f"<h2>{html.escape(perfume_name)}</h2>\n" + html_text

    return html_text.strip()

# ---------- Main generator (NO web research) ----------
@retry(
    stop=stop_after_attempt(CONFIG["max_retries"]),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((RateLimitError, APIError)),
)
def generate_description_from_three_note_strings(
    perfume_name: str,
    brand_name: Optional[str],
    top_notes_str: Any,
    middle_notes_str: Any,
    base_notes_str: Any,
    model: str = CONFIG["default_model"],
) -> str:
    brand_display = (brand_name or "").strip() or (perfume_name.split()[0] if perfume_name else "Fragrances")
    brand_slug = _brand_slug(brand_display)

    notes_obj = {
        "top": split_notes_string(top_notes_str),
        "heart": split_notes_string(middle_notes_str),  # keep your internal key as "heart"
        "base": split_notes_string(base_notes_str),
        "sources": [],
    }

    facts = {
        "perfume_name": perfume_name,
        "brand_name": brand_display,
        "brand_slug": brand_slug,
        "notes": notes_obj,
    }

    creator = safe_openai_call(
        model=model,
        messages=[
            {"role": "system", "content": CREATOR_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ],
        temperature=0.3,
        max_tokens=800,
    )
    creator_html = creator.choices[0].message.content or ""

    try:
        validator = safe_openai_call(
            model=model,
            messages=[
                {"role": "system", "content": VALIDATOR_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"facts": facts, "content_html": creator_html}, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=900,
        )
        report = json.loads(validator.choices[0].message.content)
        final_html = report.get("corrected", {}).get("content_html", creator_html)
    except Exception:
        final_html = creator_html

    return _sanitize_html_basic(final_html, perfume_name)


# ---------- Excel runner ----------
def process_excel_generate_descriptions(
    input_path: str,
    output_path: str,
    perfume_col: str = "perfume_name",
    brand_col: str = "brand_name",
    notes_col: str = "notes",
    output_col: str = "description_html",
):
    df = pd.read_excel(input_path, dtype=str)
    df = df.fillna("")

    outputs = []
    for i, row in df.iterrows():
        perfume_name = (row.get(perfume_col) or "").strip()
        brand_name = (row.get(brand_col) or "").strip()
        notes_cell = row.get(notes_col)

        if not perfume_name:
            outputs.append("")
            continue

        html_out = generate_description_from_notes(
            perfume_name=perfume_name,
            brand_name=brand_name,
            notes_cell=notes_cell,
        )
        outputs.append(html_out)

    df[output_col] = outputs
    df.to_excel(output_path, index=False)
    print(f"✅ Saved: {output_path}")