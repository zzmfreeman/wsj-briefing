#!/usr/bin/env python3
"""Test cn.wsj.com DOM scraping"""
import asyncio
from playwright.async_api import async_playwright

JS_CODE = r"""
() => {
    const nav_words = new Set([
        "SKIP TO MAIN CONTENT", "The Wall Street Journal", "订阅", "登录", "华尔街日报",
        "中文 (Chinese)", "简体版", "更多", "首页", "国际", "中国", "金融市场", "经济",
        "商业", "科技", "生活与理财", "专栏与观点", "视频", "专题报道", "广告", "独家报道"
    ]);
    
    const articles = [];
    const seen_urls = new Set();
    
    // Find all <a> tags with article links
    const all_links = document.querySelectorAll('a');
    for (const link of all_links) {
        const href = link.href || '';
        if (href.indexOf('/articles/') === -1) continue;
        
        // Check if this link contains an <img>
        const img = link.querySelector('img');
        if (!img) continue;
        
        const url = href.split('?')[0].replace(/[.,;\)\]]+$/, '');
        if (seen_urls.has(url)) continue;
        seen_urls.add(url);
        
        const img_src = img.src || '';
        if (img_src.indexOf('images.wsj.net') === -1) continue;
        
        // Get title from alt or nearby text
        let title = img.alt || '';
        if (!title || title.length < 4) {
            // Try parent text
            const parent = link.parentElement;
            if (parent) {
                const spans = parent.querySelectorAll('span, h1, h2, h3, p');
                for (const s of spans) {
                    const t = s.textContent.trim();
                    if (t.length >= 4 && t.length <= 100 && !nav_words.has(t)) {
                        title = t;
                        break;
                    }
                }
            }
        }
        
        if (title && title.length >= 4 && title.length <= 100) {
            articles.push({
                url: url,
                title: title,
                image: img_src.replace(/&amp;/g, '&'),
                summary: ''
            });
        }
    }
    
    return articles;
}
"""

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()
        try:
            resp = await page.goto("https://cn.wsj.com/", wait_until="domcontentloaded", timeout=30000)
            print("Status: %s" % (resp.status if resp else "None"))
            await page.wait_for_timeout(5000)
            
            result = await page.evaluate(JS_CODE)
            print("Articles found: %d" % len(result))
            for a in result[:15]:
                print("  [IMG] %s" % a["title"][:50])
                print("        img: %s" % a["image"][:60])
                
        except Exception as e:
            print("Error: %s" % e)
            import traceback
            traceback.print_exc()
        finally:
            await browser.close()

asyncio.run(test())
