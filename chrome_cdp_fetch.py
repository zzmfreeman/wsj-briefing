#!/usr/bin/env python3
"""
Chrome DevTools Protocol fetcher for WSJ articles
Bypasses DataDome anti-bot by using Chrome's authenticated session
"""

import json
import time
import re
import sys
import subprocess
import websocket
import urllib.request
from typing import Optional, Dict, Any

# Configuration
CHROME_DEBUG_PORT = 9222
NAVIGATION_TIMEOUT = 15
LOAD_WAIT_TIME = 3
WEBSOCKET_TIMEOUT = 1

def get_chrome_debug_port() -> Optional[int]:
    """Detect Chrome's remote debugging port from running processes"""
    try:
        result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.split('\n'):
            if '--remote-debugging-port=' in line and 'Google Chrome' in line:
                match = re.search(r'--remote-debugging-port=(\d+)', line)
                if match:
                    return int(match.group(1))
    except Exception as e:
        print(f"Error detecting Chrome port: {e}")
    return None

def get_chrome_pages(port: int) -> list:
    """Get list of available Chrome pages"""
    try:
        url = f'http://localhost:{port}/json/list'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"Error fetching Chrome pages: {e}")
        return []

def select_target_page(pages: list) -> Optional[Dict[str, Any]]:
    """Select a suitable page for navigation"""
    # Prefer pages that are not Chrome internal pages
    for page in pages:
        if page.get('type') == 'page':
            page_url = page.get('url', '')
            if not page_url.startswith('chrome://') and not page_url.startswith('about:'):
                return page
    
    # Fallback to first page
    if pages:
        return pages[0]
    
    return None

def fetch_html_via_cdp(url: str, port: int) -> Optional[str]:
    """Fetch page HTML using Chrome DevTools Protocol"""
    pages = get_chrome_pages(port)
    if not pages:
        print("Error: No Chrome pages found")
        return None
    
    target_page = select_target_page(pages)
    if not target_page:
        print("Error: No suitable page found")
        return None
    
    ws_url = target_page.get('webSocketDebuggerUrl')
    if not ws_url:
        print("Error: No WebSocket debugger URL found")
        return None
    
    print(f"Connecting to: {ws_url}")
    
    try:
        ws = websocket.create_connection(ws_url, timeout=WEBSOCKET_TIMEOUT)
    except Exception as e:
        print(f"WebSocket connection failed: {e}")
        return None
    
    message_id = 1
    html = None
    
    try:
        # Enable necessary domains
        for domain in ['Page', 'Runtime']:
            ws.send(json.dumps({"id": message_id, "method": f"{domain}.enable"}))
            message_id += 1
            ws.recv()
        
        # Navigate to the URL
        ws.send(json.dumps({
            "id": message_id,
            "method": "Page.navigate",
            "params": {"url": url}
        }))
        message_id += 1
        
        # Wait for page load
        print(f"Navigating to {url}...")
        load_event_received = False
        start_time = time.time()
        
        while time.time() - start_time < NAVIGATION_TIMEOUT:
            try:
                response = ws.recv()
                msg = json.loads(response)
                
                if msg.get('method') == 'Page.loadEventFired':
                    load_event_received = True
                    print("✓ Page load event fired")
                    time.sleep(LOAD_WAIT_TIME)
                    break
                elif msg.get('method') == 'Page.frameStoppedLoading':
                    print("✓ Frame stopped loading")
                elif msg.get('method') == 'Network.loadingFinished':
                    print("✓ Network loading finished")
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                print(f"Error receiving message: {e}")
                break
        
        if not load_event_received:
            print("⚠ Load event not received, proceeding anyway")
            time.sleep(2)
        
        # Get the page HTML
        ws.send(json.dumps({
            "id": message_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": "document.documentElement.outerHTML",
                "returnByValue": True
            }
        }))
        
        # Wait for response
        while True:
            try:
                response = ws.recv()
                msg = json.loads(response)
                
                if msg.get('id') == message_id:
                    result = msg.get('result', {})
                    if 'result' in result and 'value' in result['result']:
                        html = result['result']['value']
                        print(f"✓ HTML retrieved ({len(html)} chars)")
                    else:
                        print("✗ No HTML value in response")
                    break
            except websocket.WebSocketTimeoutException:
                print("⚠ Timeout waiting for HTML")
                break
            except Exception as e:
                print(f"Error receiving HTML: {e}")
                break
    
    except Exception as e:
        print(f"CDP fetch failed: {e}")
    finally:
        ws.close()
    
    return html

def extract_article_content(html: str) -> Optional[str]:
    """Extract article content from HTML using multiple strategies"""
    
    # Strategy 1: <article> tag
    article_match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
    if article_match:
        article_html = article_match.group(1)
        # Remove script and style tags
        text = re.sub(r'<script[^>]*>.*?</script>', '', article_html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)
        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 200:
            return text[:4000]
    
    # Strategy 2: data-testid="article-body"
    body_match = re.search(r'data-testid="article-body"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
    if body_match:
        text = re.sub(r'<script[^>]*>.*?</script>', '', body_match.group(1), flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 200:
            return text[:4000]
    
    # Strategy 3: wsj-snippet-login (fallback content)
    login_match = re.search(r'class="wsj-snippet-login"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
    if login_match:
        text = re.sub(r'<[^>]+>', ' ', login_match.group(1))
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 200:
            return text[:4000]
    
    # Strategy 4: Look for long text blocks
    # Remove all script and style tags first
    clean_html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    clean_html = re.sub(r'<style[^>]*>.*?</style>', '', clean_html, flags=re.DOTALL | re.IGNORECASE)
    
    # Extract all text
    all_text = re.sub(r'<[^>]+>', '\n', clean_html)
    paragraphs = [p.strip() for p in all_text.split('\n') if len(p.strip()) > 50]
    
    if paragraphs:
        # Find the longest paragraph cluster (likely article content)
        article_text = ' '.join(paragraphs[:10])  # Take first 10 substantial paragraphs
        if len(article_text) > 200:
            return article_text[:4000]
    
    return None

def detect_antibot(html: str) -> bool:
    """Detect if page contains anti-bot protection"""
    html_sample = html[:5000].lower()
    indicators = [
        'dd=',
        'captcha',
        'challenge',
        'cloudflare',
        'data domain script',
        'bot detection'
    ]
    return any(indicator in html_sample for indicator in indicators)

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 chrome_cdp_fetch.py <url> [output_file]")
        print("\nExamples:")
        print("  python3 chrome_cdp_fetch.py 'https://www.wsj.com/articles/...'")
        print("  python3 chrome_cdp_fetch.py 'https://www.wsj.com/articles/...' output.html")
        sys.exit(1)
    
    url = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    # Detect Chrome debug port
    port = get_chrome_debug_port()
    if not port:
        print("❌ Error: Chrome not running with remote debugging")
        print("\nStart Chrome with:")
        print("  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222")
        sys.exit(1)
    
    print(f"✓ Chrome debug port: {port}")
    print(f"✓ Target URL: {url}")
    
    # Fetch HTML
    html = fetch_html_via_cdp(url, port)
    
    if not html:
        print("\n❌ Failed to fetch page")
        sys.exit(1)
    
    # Save HTML if requested
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"✓ HTML saved to {output_file}")
    
    # Check for anti-bot
    if detect_antibot(html):
        print("⚠ WARNING: Anti-bot protection detected in page")
    
    # Extract article content
    article_text = extract_article_content(html)
    
    if article_text:
        print(f"\n✓ Extracted article content ({len(article_text)} chars)")
        print("=" * 70)
        print(article_text[:800])
        if len(article_text) > 800:
            print("...")
        print("=" * 70)
    else:
        print("\n❌ Failed to extract article content")
        print(f"HTML length: {len(html)} chars")
        
        # Show first 1000 chars of HTML for debugging
        print("\nFirst 1000 chars of HTML:")
        print(html[:1000])

if __name__ == "__main__":
    main()
