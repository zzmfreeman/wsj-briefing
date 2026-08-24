#!/usr/bin/env python3
"""
WSJ Authenticated Article Reader
Uses autocli with Chrome extension login to read WSJ articles and extract metadata
"""
import json, re, subprocess
from pathlib import Path

AUTOCLI = "/usr/local/bin/autocli"

def read_article(url):
    """Read WSJ article via autocli (uses Chrome extension login session).
    Returns dict with title, text_content, html_content, excerpt, byline, published_time.
    Returns None on failure."""
    try:
        r = subprocess.run(
            [AUTOCLI, "read", url, "-f", "json"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            return {
                "title": data.get("title", ""),
                "text_content": data.get("textContent", ""),
                "html_content": data.get("content", ""),
                "excerpt": data.get("excerpt", ""),
                "byline": data.get("byline", ""),
                "published_time": data.get("publishedTime", ""),
                "site_name": data.get("siteName", ""),
                "length": data.get("length", 0),
            }
    except Exception as e:
        print(f"    [autocli_read error] {e}")
    return None


def extract_lead_from_text(text):
    """Extract lead (first meaningful paragraph) from article text.
    Returns (lead, remaining_text).
    Skips: timestamps, bylines, copyright, author info, metadata."""
    if not text:
        return "", ""
    
    # Clean timestamp prefix from the entire text
    # autocli format: "Updated Jan. 28, 2025 12:44 pm ETDeepSeek has..."
    # or " 2026年7月17日 16:52 CST中国领导人..."
    # Use explicit timezone list to avoid greedy matching
    tz_list = r'(?:ET|EST|EDT|CST|CDT|PST|PDT|MST|MDT|GMT|UTC|BST|CET|IST|HKT|JST|KST|SGT|AEST|ACST)'
    cleaned = re.sub(r'^\s*Updated \w+\.? \d{1,2},? \d{4} \d{1,2}:\d{2} [ap]m ' + tz_list, '', text)
    cleaned = re.sub(r'^\s*\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}\s+' + tz_list, '', cleaned)
    cleaned = re.sub(r'^\s*\w+\.? \d{1,2},? \d{4} \d{1,2}:\d{2} [ap]m ' + tz_list, '', cleaned)
    cleaned = re.sub(r'^\s*\d{1,2}:\d{2} [ap]m ' + tz_list, '', cleaned)
    
    # Split by double newlines or paragraphs
    paragraphs = re.split(r'\n\n+', cleaned)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    
    lead = ""
    body = ""
    found_lead = False
    
    for i, p in enumerate(paragraphs):
        if len(p) < 30:
            continue
        
        if not found_lead:
            lead = p[:500]
            found_lead = True
            if i + 1 < len(paragraphs):
                body_parts = paragraphs[i+1:]
                body = "\n\n".join(body_parts)[:3000]
            break
    
    if not lead and paragraphs:
        lead = paragraphs[0][:500]
    
    return lead, body


def extract_og_image_from_html(html_content):
    """Try to extract image from autocli HTML content (usually none, but worth trying)."""
    if not html_content:
        return ""
    m = re.search(r'src="(https?://images\.wsj\.net[^"]+)"', html_content)
    if m:
        return m.group(1).replace("&amp;", "&")
    return ""


def fetch_article_with_metadata(url):
    """Fetch article full text + metadata via autocli.
    Returns dict with fulltext, lead, summary, title, or None."""
    data = read_article(url)
    if not data or not data.get("text_content"):
        return None
    
    text = data["text_content"]
    lead, body = extract_lead_from_text(text)
    
    result = {
        "fulltext": text[:4000],
        "lead": lead,
        "summary": body or data.get("excerpt", ""),
        "title": data.get("title", ""),
        "byline": data.get("byline", ""),
        "published_time": data.get("published_time", ""),
    }
    
    return result


if __name__ == "__main__":
    # Test
    test_urls = [
        "https://www.wsj.com/articles/deepseek-ai-china-tech-stocks-explained-ee6cc80e",
        "https://cn.wsj.com/articles/WP-WSJS-0003743544",
    ]
    for url in test_urls:
        print(f"\n=== {url[:50]} ===")
        result = fetch_article_with_metadata(url)
        if result:
            print(f"  Title: {result['title'][:50]}")
            print(f"  Lead: {result['lead'][:100]}")
            print(f"  Fulltext: {len(result['fulltext'])} chars")
        else:
            print(f"  FAILED")