#!/usr/bin/env python3
"""
WSJ Authenticated Scraper
Uses Chrome cookies decrypted via macOS Keychain to access WSJ articles with login
"""
import asyncio, json, os, re, sqlite3, shutil, subprocess, tempfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

HOME = Path.home()
COOKIES_DB = HOME / "Library/Application Support/Google/Chrome/Default/Cookies"

def get_chrome_key():
    """Get Chrome cookie encryption key via AppleScript (GUI session)"""
    script = 'do shell script "security find-generic-password -w -s \'Chrome Safe Storage\'"'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get Chrome key: {result.stderr}")
    return result.stdout.strip()

def decrypt_chrome_cookies():
    """Decrypt all Chrome cookies for wsj.com domains"""
    key_material = get_chrome_key()
    print(f"  [cookies] Got Chrome key ({len(key_material)} chars)")
    
    # Derive AES key
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1003)
    aes_key = kdf.derive(key_material.encode("utf-8"))
    
    # Copy Cookies DB (Chrome might have it locked)
    tmp_db = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy2(COOKIES_DB, tmp_db)
    except PermissionError:
        # Try to read without locking
        import shutil
        try:
            # Use cp to copy without locking
            subprocess.run(["cp", str(COOKIES_DB), tmp_db], check=True, timeout=5)
        except:
            print("  [cookies] WARNING: Could not copy Cookies DB, trying directly")
            tmp_db = str(COOKIES_DB)
    
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    
    # Get all cookies for wsj.com domains
    rows = conn.execute(
        "SELECT host_key, name, encrypted_value, path, is_secure, is_httponly "
        "FROM cookies WHERE host_key LIKE '%wsj.com'"
    ).fetchall()
    
    decoded = []
    for row in rows:
        enc_val = row["encrypted_value"]
        if not enc_val or enc_val[:3] != b"v10":
            continue
        
        try:
            nonce = enc_val[3:15]
            ct = enc_val[15:]
            aesgcm = AESGCM(aes_key)
            value = aesgcm.decrypt(nonce, ct, None).decode("utf-8")
            
            if value:
                decoded.append({
                    "name": row["name"],
                    "value": value,
                    "domain": row["host_key"],
                    "path": row["path"] or "/",
                    "secure": bool(row["is_secure"]),
                    "httpOnly": bool(row["is_httponly"]),
                    "sameSite": "Lax",
                })
        except:
            # Try with zero nonce (older format)
            try:
                aesgcm = AESGCM(aes_key)
                value = aesgcm.decrypt(b"\x00" * 12, enc_val[3:], None).decode("utf-8")
                if value:
                    decoded.append({
                        "name": row["name"],
                        "value": value,
                        "domain": row["host_key"],
                        "path": row["path"] or "/",
                        "secure": bool(row["is_secure"]),
                        "httpOnly": bool(row["is_httponly"]),
                        "sameSite": "Lax",
                    })
            except:
                pass
    
    if tmp_db != str(COOKIES_DB):
        os.unlink(tmp_db)
    
    print(f"  [cookies] Decrypted {len(decoded)} cookies for wsj.com")
    return decoded


async def fetch_wsj_article(url, cookies):
    """Fetch WSJ article with login cookies via Playwright"""
    from playwright.async_api import async_playwright
    
    async with async_playwright() as p:
        # Create fresh browser with cookies injected
        browser = await p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN" if "cn.wsj.com" in url else "en-US",
        )
        await context.add_cookies(cookies)
        page = await context.new_page()
        
        result = {}
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            result["status"] = resp.status if resp else 0
            await page.wait_for_timeout(3000)
            
            # Extract og:image
            result["og_image"] = await page.evaluate(
                'document.querySelector(\'meta[property="og:image"]\')?.content || ""'
            )
            
            # Extract article text
            text = await page.inner_text("body")
            result["article_text"] = text[:5000] if text else ""
            
            # Extract article HTML
            html = await page.content()
            # Try to find <article> tag
            article_match = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL)
            if article_match:
                article_text = re.sub(r"<[^>]+>", " ", article_match.group(1))
                result["article_text"] = re.sub(r"\s+", " ", article_text).strip()[:5000]
            
            # Extract lead (first paragraph of article)
            if result.get("article_text"):
                # Split by double newlines or paragraphs
                paragraphs = [p.strip() for p in result["article_text"].split("\n\n") if p.strip()]
                for p in paragraphs:
                    p = p.strip()
                    if len(p) > 50 and not p.startswith("Copyright") and "Sign In" not in p:
                        result["lead"] = p[:500]
                        break
            
            html_content = await page.content()
            result["html"] = html_content[:10000]  # First 10KB for analysis
            
        except Exception as e:
            result["error"] = str(e)
        finally:
            await browser.close()
        
        return result


async def main():
    url = "https://www.wsj.com/articles/deepseek-ai-china-tech-stocks-explained-ee6cc80e"
    
    print("=== WSJ Authenticated Scraper ===")
    print(f"URL: {url}")
    
    # Step 1: Get cookies from Chrome
    print("\n[1/3] Getting Chrome cookies for wsj.com...")
    cookies = decrypt_chrome_cookies()
    
    if not cookies:
        print("ERROR: No cookies found. Are you logged into wsj.com in Chrome?")
        return
    
    # Step 2: Fetch article
    print("\n[2/3] Fetching article with login...")
    result = await fetch_wsj_article(url, cookies)
    
    # Step 3: Report
    print(f"\n[3/3] Results")
    print(f"  Status: {result.get('status')}")
    print(f"  og:image: {result.get('og_image', 'NOT FOUND')[:80]}")
    print(f"  Article text: {len(result.get('article_text', ''))} chars")
    print(f"  Lead: {result.get('lead', 'NOT FOUND')[:200]}")
    
    if result.get("og_image"):
        print("\n✅ SUCCESS: Authenticated access works!")
    elif result.get("status") == 200:
        print("\n⚠️ Page loaded (200) but no og:image found")
    else:
        print(f"\n❌ Failed: status={result.get('status')}")

if __name__ == "__main__":
    asyncio.run(main())