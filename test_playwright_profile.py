#!/usr/bin/env python3
"""Test Playwright with copied Chrome profile - browser handles decryption"""
import asyncio, json, os, shutil, tempfile
from playwright.async_api import async_playwright
import re

async def test():
    chrome_data = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    tmpdir = tempfile.mkdtemp(prefix="chrome_wsj_")
    
    # Copy entire Default profile (minimal set for cookies)
    default_src = os.path.join(chrome_data, "Default")
    default_dst = os.path.join(tmpdir, "Default")
    
    os.makedirs(default_dst, exist_ok=True)
    for f in os.listdir(default_src):
        # Only copy essential files
        if f in ("Cookies", "Cookies-journal", "Local State", "Preferences"):
            src = os.path.join(default_src, f)
            dst = os.path.join(default_dst, f)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                print(f"Copied: {f} ({os.path.getsize(src)} bytes)")
    
    print(f"Profile dir: {tmpdir}")
    
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=tmpdir,
            headless=True,
            channel="chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        url = "https://www.wsj.com/articles/deepseek-ai-china-tech-stocks-explained-ee6cc80e"
        print(f"\nNavigating to: {url}")
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            print(f"Status: {resp.status if resp else 'None'}")
            await page.wait_for_timeout(3000)
            
            # Get og:image
            og_image = await page.evaluate("document.querySelector('meta[property=\"og:image\"]')?.content || ''")
            print(f"og:image: {og_image[:80] if og_image else 'NOT FOUND'}")
            
            # Get page content
            text = await page.inner_text("body")
            print(f"Body text: {len(text)} chars")
            if text:
                print(f"First 200: {text[:200]}")
            
            # Check for login wall
            if "Sign In" in text[:500] or "Subscribe" in text[:500]:
                print("LOGIN WALL DETECTED - cookies not working")
            else:
                print("SUCCESS - no paywall, content accessible")
                
        except Exception as e:
            print(f"Error: {e}")
        finally:
            await context.close()
        
        shutil.rmtree(tmpdir, ignore_errors=True)

asyncio.run(test())