import re
import json
import html
import pandas as pd
from typing import Optional, Dict, Any, List, Tuple
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

# ---------- Enhanced Notes Processing ----------
def process_notes_input(notes_input: Any) -> List[str]:
    """
    Process notes input from API (string or list).
    Returns cleaned list of notes.
    """
    if notes_input is None:
        return []
    
    # If it's already a list
    if isinstance(notes_input, list):
        notes_list = []
        for item in notes_input:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    notes_list.append(cleaned.title())
        return notes_list[:8]  # Limit to 8 notes
    
    # If it's a string
    if isinstance(notes_input, str):
        s = notes_input.strip()
        if not s or s.lower() in {"nan", "none", "null", "n/a", "na", ""}:
            return []
        
        # Split by common delimiters
        parts = re.split(r"[,\|;/•\n]+", s)
        out = []
        
        for p in parts:
            p = p.strip()
            if not p:
                continue
            
            # Remove content in parentheses
            p = re.sub(r"\(.*?\)", "", p).strip()
            # Normalize whitespace
            p = re.sub(r"\s+", " ", p)
            # Convert to Title Case
            p = p.title()
            
            if 2 <= len(p) <= 50:
                out.append(p)
        
        # De-dupe while preserving order
        seen = set()
        deduped = []
        for n in out:
            k = n.lower()
            if k not in seen:
                seen.add(k)
                deduped.append(n)
        
        return deduped[:8]
    
    # For any other type, convert to string
    return process_notes_input(str(notes_input))

# ---------- Enhanced Prompts for API ----------
CREATOR_SYSTEM_PROMPT = """
You are an expert SEO copywriter for Shopify perfume listings.

GOAL:
Create elegant, factual, and SEO-optimized perfume descriptions in valid HTML format.

CRITICAL RULES:
1. You MUST use the notes provided in the "notes" object.
2. NEVER say "no notes available" or similar phrases.
3. If notes are provided, describe them beautifully in the description.
4. If no notes are provided for a category, simply don't mention that category.

STRUCTURE:
1. <h2>{Perfume Name}</h2>
2. Intro paragraph (2-3 sentences introducing the perfume)
3. <h3>The Experience</h3>
4. <h3>Signature Notes</h3>
   - Create an unordered list (<ul>) with list items (<li>)
   - Group notes by category if available: Top Notes, Heart Notes, Base Notes
   - Only create sections for categories that have notes
   - Describe each note category naturally
5. <h3>Perfect For</h3>
6. Internal link: <p>Discover more from <a href="/collections/{brand_slug}">{Brand} perfumes</a></p>

FORMATTING:
- Use ONLY these HTML tags: <h2>, <h3>, <p>, <ul>, <li>, <strong>, <a>
- No inline styles, no scripts, no emojis
- Make the description compelling and sales-oriented

EXAMPLE WITH NOTES:
If notes are: {"top": ["Bergamot", "Lemon"], "heart": ["Jasmine"], "base": ["Sandalwood"]}
Output should include mention of Bergamot, Lemon, Jasmine, and Sandalwood.

EXAMPLE WITHOUT ANY NOTES:
If notes are: {"top": [], "heart": [], "base": []}
Focus on the perfume's character, mood, and occasion. Do not mention specific notes.
"""

VALIDATOR_SYSTEM_PROMPT = """
You are a strict HTML validator for perfume descriptions.

CRITICAL RULES:
1. The description MUST include any notes provided in the input.
2. NEVER output phrases like "no notes available", "notes not provided", etc.
3. If notes are empty, focus on general perfume description without mentioning missing notes.

VALIDATION CHECKLIST:
✅ <h2> with perfume name exists
✅ Structure follows: H2 → Intro → The Experience → Signature Notes → Perfect For → Internal Link
✅ Signature Notes section contains proper <ul> with <li> items
✅ If notes are provided, they are mentioned in the description
✅ Exactly one internal link at the end with correct {brand_slug} and {brand_name}
✅ No invalid HTML tags
✅ No mention of missing or unavailable notes

OUTPUT FORMAT:
{
  "overall_pass": boolean,
  "failures": [list of issues found],
  "corrected": {"content_html": "corrected HTML content"}
}

CORRECTIONS TO MAKE:
1. Add missing H2 if needed
2. Ensure notes are properly incorporated
3. Remove any phrases about missing notes
4. Fix invalid HTML
5. Add internal link if missing
"""

# ---------- HTML Sanitization ----------
def _sanitize_html_basic(html_text: str, perfume_name: str) -> str:
    """Sanitize HTML while preserving allowed tags."""
    if not html_text:
        # Create a minimal valid description
        return f"""<h2>{html.escape(perfume_name)}</h2>
<p>An exquisite fragrance that captivates the senses with its unique character.</p>
<h3>The Experience</h3>
<p>This distinctive scent creates a memorable impression that lasts throughout the day.</p>
<h3>Signature Notes</h3>
<ul><li>A captivating blend of distinctive aromas</li></ul>
<h3>Perfect For</h3>
<p>Evening occasions, special events, and moments when you want to make a lasting impression.</p>"""
    
    # Remove scripts and styles
    html_text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html_text)
    
    # Keep only allowed tags
    def keep_allowed(m):
        tag = m.group(1).lower()
        return m.group(0) if tag in CONFIG["allowed_tags"] else ""
    
    html_text = re.sub(r"(?i)</?([a-z0-9]+)([^>]*)>", keep_allowed, html_text)
    
    # Ensure H2 exists with perfume name
    escaped_name = re.escape(html.escape(perfume_name))
    if not re.search(rf'(?is)<h2[^>]*>\s*{escaped_name}\s*</h2>', html_text):
        html_text = f"<h2>{html.escape(perfume_name)}</h2>\n" + html_text
    
    # Clean up whitespace
    html_text = re.sub(r'\n\s*\n+', '\n', html_text).strip()
    
    return html_text

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
    """
    Generate perfume description from three separate note inputs.
    This is the main function called from your API endpoint.
    """
    # Process brand information
    brand_display = (brand_name or "").strip()
    if not brand_display:
        # Extract brand from perfume name if possible
        name_parts = perfume_name.split()
        brand_display = name_parts[0] if name_parts else "Fragrances"
    brand_slug = _brand_slug(brand_display)
    
    # Process notes
    notes_obj = {
        "top": process_notes_input(top_notes),
        "heart": process_notes_input(middle_notes),
        "base": process_notes_input(base_notes),
    }
    
    # Check if we have any notes
    has_notes = any(len(notes) > 0 for notes in notes_obj.values())
    
    # Debug output
    if debug:
        print("=" * 60)
        print(f"Generating description for: {perfume_name}")
        print(f"Brand: {brand_display}")
        print(f"Top notes: {notes_obj['top']}")
        print(f"Heart notes: {notes_obj['heart']}")
        print(f"Base notes: {notes_obj['base']}")
        print(f"Has notes: {has_notes}")
        print("=" * 60)
    
    # Prepare the facts for the AI
    facts = {
        "perfume_name": perfume_name,
        "brand_name": brand_display,
        "brand_slug": brand_slug,
        "notes": notes_obj,
        "has_notes": has_notes,
        "instructions": "IMPORTANT: Always use the provided notes if available. Never mention missing notes."
    }
    
    try:
        # Step 1: Generate initial description
        creator_response = safe_openai_call(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": CREATOR_SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": json.dumps(facts, ensure_ascii=False, indent=2)
                }
            ],
            temperature=0.4,  # Slightly higher for creativity
            max_tokens=1000,
        )
        
        creator_html = creator_response.choices[0].message.content or ""
        
        if debug:
            print("Generated HTML (raw):")
            print(creator_html[:500] + "..." if len(creator_html) > 500 else creator_html)
        
        # Step 2: Validate and correct
        validator_response = safe_openai_call(
            model=model,
            messages=[
                {
                    "role": "system", 
                    "content": VALIDATOR_SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "facts": facts,
                        "original_html": creator_html,
                        "validation_requirements": [
                            "Must include provided notes if available",
                            "Never mention missing notes",
                            "Must have proper HTML structure"
                        ]
                    }, ensure_ascii=False, indent=2)
                }
            ],
            response_format={"type": "json_object"},
            temperature=0.1,  # Very low for validation
            max_tokens=1200,
        )
        
        validator_data = json.loads(validator_response.choices[0].message.content)
        final_html = validator_data.get("corrected", {}).get("content_html", creator_html)
        
        # Log validation failures for debugging
        if debug and validator_data.get("failures"):
            print(f"Validation failures: {validator_data['failures']}")
        
    except Exception as e:
        print(f"Error in description generation for {perfume_name}: {str(e)}")
        
        # Create a robust fallback description
        notes_description = ""
        if has_notes:
            note_sections = []
            if notes_obj["top"]:
                note_sections.append(f"<strong>Top Notes:</strong> {', '.join(notes_obj['top'])}")
            if notes_obj["heart"]:
                note_sections.append(f"<strong>Heart Notes:</strong> {', '.join(notes_obj['heart'])}")
            if notes_obj["base"]:
                note_sections.append(f"<strong>Base Notes:</strong> {', '.join(notes_obj['base'])}")
            
            if note_sections:
                notes_description = f"<h3>Signature Notes</h3><ul><li>{'</li><li>'.join(note_sections)}</li></ul>"
        
        # Fallback HTML template
        final_html = f"""<h2>{html.escape(perfume_name)}</h2>
<p>An exquisite fragrance by {html.escape(brand_display)} that creates a captivating sensory experience.</p>
<h3>The Experience</h3>
<p>This distinctive scent evolves beautifully, creating a memorable impression that lasts throughout the day.</p>
{notes_description}
<h3>Perfect For</h3>
<p>Evening occasions, special events, romantic moments, and any time you want to make a lasting impression.</p>
<p>Discover more from <a href="/collections/{brand_slug}">{html.escape(brand_display)} perfumes</a></p>"""
    
    # Final sanitization
    sanitized_html = _sanitize_html_basic(final_html, perfume_name)
    
    if debug:
        print("Final HTML length:", len(sanitized_html))
        print("=" * 60)
    
    return sanitized_html
# ---------- API Helper Function ----------
def generate_description_for_api(
    perfume_name: str,
    brand_name: str = None,
    top_notes: Any = None,
    middle_notes: Any = None,
    base_notes: Any = None,
    model: str = CONFIG["default_model"]
) -> Dict[str, Any]:
    """
    Wrapper function specifically for API calls.
    Returns a dict with the result and metadata.
    """
    try:
        description_html = generate_description_from_three_note_strings(
            perfume_name=perfume_name,
            brand_name=brand_name,
            top_notes=top_notes,
            middle_notes=middle_notes,
            base_notes=base_notes,
            model=model,
            debug=False  # Set to True for debugging
        )
        
        return {
            "success": True,
            "description": description_html,
            "perfume_name": perfume_name,
            "brand_name": brand_name or "Not specified",
            "length": len(description_html),
            "model_used": model
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "perfume_name": perfume_name,
            "fallback_description": f"<h2>{html.escape(perfume_name)}</h2><p>An exquisite fragrance with a captivating presence.</p>"
        }