import re
import json
import time
from typing import Optional, Dict, Any, List, Tuple
import html
from dotenv import load_dotenv
from openai import OpenAI, APIError, RateLimitError
import pandas as pd
import os
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ---------- OpenAI Safe Call with Timeout + Retries ----------
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((RateLimitError, APIError))
)
def safe_openai_call(**kwargs):
    """Wrapper for OpenAI calls with extended timeout."""
    return client.chat.completions.create(timeout=90, **kwargs)


load_dotenv(override=True)

client = OpenAI()

# ---------- Configuration ----------
CONFIG = {
    "default_model": "gpt-4o-mini",  # More cost-effective than gpt-5
    "fallback_model": "gpt-4o",
    "max_retries": 3,
    "request_timeout": 30,
    "min_delay_between_calls": 0.5,
    "checkpoint_interval": 25,
    "allowed_tags": {"h2", "h3", "p", "ul", "li", "strong", "a"}
}

# ---------- Optimized Web Research ----------
def fetch_notes_with_fallback(perfume_name: str, brand_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Try with sonar first, if empty results fall back to sonar-pro.
    Now with strict source validation.
    """
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        print("❌ PERPLEXITY_API_KEY not found, using empty results")
        return empty_result()

    # Try sonar first (cheaper)
    sonar_result = _fetch_with_perplexity(perfume_name, brand_name, "sonar", api_key,timeout=45)
    
    if _has_meaningful_notes(sonar_result) and _has_reliable_sources(sonar_result):
        return sonar_result
    
    # Fallback to sonar-pro if sonar failed or has unreliable sources
    pro_result = _fetch_with_perplexity(perfume_name, brand_name, "sonar-pro", api_key)
    
    # If pro result has reliable sources, use it
    if _has_meaningful_notes(pro_result) and _has_reliable_sources(pro_result):
        return pro_result
    
    # If neither has reliable sources but pro has notes, use it with a warning
    if _has_meaningful_notes(pro_result):
        print(f"⚠️ Using notes from less reliable sources for {perfume_name}")
        return pro_result
    
    return empty_result()

def _fetch_with_perplexity(perfume_name: str, brand_name: Optional[str], model: str, api_key: str) -> Dict[str, Any]:
    """Internal function to call Perplexity API with improved query"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Improved query to prioritize reliable sources
    user_query = (
        f"Find the exact fragrance notes for '{perfume_name}'"
        + (f" by {brand_name}" if brand_name else "")
        + ". First check Fragrantica.com and Parfumo.net. "
        "If not found there, you may use other sources. "
        "Return JSON: {'top':[],'heart':[],'base':[],'sources':[]}"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only JSON. No prose. Prioritize Fragrantica.com and Parfumo.net for fragrance notes."},
            {"role": "user", "content": user_query},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
        "search_domain_filter": ["fragrantica.com", "parfumo.net"],
        "search_recency_filter": "year",
    }

    try:
        resp = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers=headers,
            json=payload,
            timeout=30,  # Using a reasonable timeout
        )

        if resp.status_code != 200:
            print(f"[{model}] API Error: {resp.status_code}")
            return empty_result()

        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        
        # Clean and parse JSON
        text = _clean_json_response(text)
        obj = json.loads(text)
        
        # Clean and validate notes
        cleaned_result = {
            "top": _clean_notes_list(obj.get("top", []))[:8],
            "heart": _clean_notes_list(obj.get("heart", []))[:8],
            "base": _clean_notes_list(obj.get("base", []))[:8],
            "sources": _clean_sources_list(obj.get("sources", []))[:3]
        }
        
        return cleaned_result
        
    except json.JSONDecodeError as e:
        print(f"[{model}] JSON Parse Error: {e}")
        print(f"Raw response: {text}")
        return empty_result()
    except Exception as e:
        print(f"[{model}] Error: {e}")
        return empty_result()

def _clean_notes_list(notes: List[str]) -> List[str]:
    """Clean and validate individual fragrance notes"""
    cleaned_notes = []
    for note in notes:
        if isinstance(note, str) and note.strip():
            # Remove any extra descriptions in parentheses
            clean_note = re.sub(r'\(.*?\)', '', note).strip()
            # Normalize whitespace
            clean_note = re.sub(r'\s+', ' ', clean_note)
            # Standardize capitalization
            clean_note = clean_note.title()
            
            if 2 <= len(clean_note) <= 50:  # Reasonable length for a note
                cleaned_notes.append(clean_note)
    
    return cleaned_notes

def _clean_sources_list(sources: List[str]) -> List[str]:
    """Clean and filter sources"""
    cleaned_sources = []
    for source in sources:
        if isinstance(source, str) and source.strip():
            # Basic URL validation
            if re.match(r'https?://', source):
                cleaned_sources.append(source)
    return cleaned_sources

def _has_meaningful_notes(result: Dict[str, Any]) -> bool:
    """Check if result has meaningful fragrance notes"""
    total_notes = len(result["top"]) + len(result["heart"]) + len(result["base"])
    return total_notes >= 3  # At least 3 notes total

def _has_reliable_sources(result: Dict[str, Any]) -> bool:
    """Check if result has sources from reliable fragrance databases"""
    reliable_domains = ['fragrantica.com', 'parfumo.net', 'basenotes.net', 'theluxuryconcepts.com']
    
    for source in result.get("sources", []):
        if any(domain in source.lower() for domain in reliable_domains):
            return True
    
    return False

def empty_result() -> Dict[str, Any]:
    return {"top": [], "heart": [], "base": [], "sources": []}

def _clean_json_response(text: str) -> str:
    """Clean JSON response by removing markdown code blocks and other non-JSON content"""
    # Remove markdown code blocks
    if text.startswith("```json"):
        text = text.split("```json")[1].split("```")[0].strip()
    elif text.startswith("```"):
        text = text.split("```")[1].split("```")[0].strip()
    
    # Extract JSON from text if it's embedded in other content
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        text = json_match.group(0)
    
    return text.strip()

# ---------- Optimized Slug + Link Utilities ----------
def _brand_slug(brand_name: str) -> str:
    """Generate URL-safe brand slug"""
    if not brand_name:
        return "fragrances"
    
    slug = brand_name.strip().lower()
    slug = re.sub(r"[&+]", "and", slug)
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug.strip("-")

def _strip_internal_links(html_text: str) -> str:
    """Remove existing internal links"""
    patterns = [
        r'(?is)<p>\s*Explore\s+more\s+from.*?</p>',
        r'(?is)<p>.*?<a\s+href\s*=\s*["\']\s*/collections/[^"\']+["\'].*?>.*?</a>.*?</p>'
    ]
    for pattern in patterns:
        html_text = re.sub(pattern, '', html_text)
    return html_text

# ---------- Optimized Prompts ----------
CREATOR_SYSTEM_PROMPT = """
You are an expert SEO copywriter for Shopify perfume listings.

GOAL:
Create elegant, factual, and SEO-optimized perfume descriptions in valid HTML format.

RULES:
- Use ONLY the factual notes provided. Never invent new ones.
- Use semantic HTML only: <h2>, <h3>, <p>, <ul>, <li>, <strong>, <a>.
- Do NOT include inline styles, scripts, emojis, or special characters.
- All text must be professionally written and naturally flowing.

STRUCTURE:
1. <h2>Product Name</h2>
2. Intro paragraph: 2 natural sentences introducing the perfume (tone depends on gender/unisex and concentration hints).
3. <h3>The Experience</h3> — describe the sensory character, projection, and emotion of the scent.
4. <h3>Signature Notes</h3>
   - Present the notes in a clean <ul>.
   - Include list items only for note categories that contain actual notes.
   - If one category (top/heart/base) is missing, omit it entirely.
   - Never write “None specified” or similar placeholders.
   - If all categories are empty, reuse any available note from other tiers to show at least one item.
5. <h3>Perfect For</h3> — describe suitable occasions or seasons in 1–2 lines.
6. Add exactly one internal link at the end:
   <p>Discover more from <a href="/collections/{slug}">{Brand} perfumes</a></p>

SEO STYLE:
- Keep it human, smooth, and concise.
- Mention the perfume name once naturally in the text.
- Use clear spacing between paragraphs.
- Avoid repetition or keyword stuffing.
"""

VALIDATOR_SYSTEM_PROMPT = """
Validate and correct the provided HTML perfume description.

VALIDATION RULES:
1. Content must match provided factual notes and perfume data exactly (no invented notes or brands).
2. Required section order:
   H2 (Product Name) → Intro → The Experience → Signature Notes → Perfect For → Internal Link
3. The <h3>Signature Notes</h3> section must:
   - Contain at least one <li> element.
   - Never contain text like "None", "Not specified", or placeholders.
   - Remove empty categories (e.g., Top Notes with no notes).
4. There must be exactly one internal link at the end in this exact format:
   <p>Discover more from <a href="/collections/{slug}">{Brand} perfumes</a></p>
5. Remove any emojis, symbols, or invalid tags.
6. Ensure HTML is valid, clean, and properly nested.

OUTPUT FORMAT:
Return JSON in this exact structure:
{
  "overall_pass": bool,
  "failures": ["list of issues found"],
  "corrected": {
    "content_html": "fully corrected and validated HTML"
  }
}
"""


# ---------- Optimized Main Function ----------
@retry(
    stop=stop_after_attempt(CONFIG["max_retries"]),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((RateLimitError, APIError))
)
def generate_description_from_web(
    perfume_name: str,
    brand_name: Optional[str] = None,
    model: str = CONFIG["default_model"]
) -> str:
    """Generate HTML description with research → create → validate workflow"""
    
    # 1) Research notes
    research = fetch_notes_with_fallback(perfume_name, brand_name)
    brand_display = brand_name or perfume_name.split()[0]
    brand_slug = _brand_slug(brand_display)

    # 2) Creator: generate HTML
    facts = {
        "perfume_name": perfume_name,
        "brand_name": brand_display,
        "brand_slug": brand_slug,
        "notes": research,
        "sources": research.get("sources", [])
    }

    creator_response = safe_openai_call(
        model=model,
        messages=[
            {"role": "system", "content": CREATOR_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}
        ],
        temperature=0.3,
        max_tokens=800
    )
    
    creator_html = creator_response.choices[0].message.content

    # 3) Validator: check and correct
    try:
        validator_response = safe_openai_call(
            model=model,
            messages=[
                {"role": "system", "content": VALIDATOR_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({
                    "facts": facts,
                    "content_html": creator_html
                }, ensure_ascii=False)}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        
        report = json.loads(validator_response.choices[0].message.content)
        final_html = report.get("corrected", {}).get("content_html", creator_html)
        
    except Exception:
        final_html = creator_html  # Fallback to original if validation fails

    # 4) Post-processing
    final_html = _sanitize_html(final_html, perfume_name)
    return final_html.strip()

def _sanitize_html(html_text: str, perfume_name: str) -> str:
    """Sanitize HTML and ensure proper structure"""
    # Remove disallowed tags
    def replace_disallowed(match):
        tag = match.group(1).lower()
        return match.group(0) if tag in CONFIG["allowed_tags"] else ""
    
    html_text = re.sub(r"(?i)<(script|style)[^>]*>.*?</\1>", "", html_text)
    html_text = re.sub(r"(?i)</?([a-z0-9]+)([^>]*)>", replace_disallowed, html_text)
    
    # Ensure H2 with exact name
    if not re.search(rf"(?i)<h2>\s*{re.escape(perfume_name)}\s*</h2>", html_text):
        html_text = f"<h2>{perfume_name}</h2>\n" + html_text
    
    return html_text

def load_csv(csv_path: str) -> pd.DataFrame:
    """
    Load a CSV robustly:
      - Try UTF-8
      - Fall back to cp1252 (Windows)
      - Finally latin-1
    """
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding=enc)
        except UnicodeDecodeError:
            continue
    # If all fail, replace undecodable chars
    return pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="latin-1", errors="replace")