#!/usr/bin/env python3
"""Fast WSJ collection with cookie-based batch og:image"""
import asyncio, json, re, subprocess, sys, os, time, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.async_api import async_playwright

SH_TZ = timezone(timedelta(hours=8))
sys.path.insert(0, os.path.expanduser("~/wsj-briefing"))
from remote_collect import (
    pw_fetch_homepage, RSS_FEEDS, ARTICLES_PER_SECTION, 
    fetch_rss, enrich_images, load_seen_urls, save_seen_urls,
    IMAGE_CACHE_FILE, SEEN_URLS_FILE, DEDUP_DAYS
)

async def scrape_cn_homepage(limit=15):
    """用 Playwright DOM API 提取 cn.wsj.com 首页"""
    print("  抓取 cn.wsj.com 首页 (Playwright DOM)...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()
        await page.goto("https://cn.wsj.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        
        articles = await page.evaluate("""
        (limit) => {
            const result = [];
            const seen = new Set();
            const slots = document.querySelectorAll('[data-parsely-slot]');
            for (const slot of slots) {
                const slotImg = slot.querySelector('img[src*="images.wsj.net"]');
                const imgSrc = slotImg ? slotImg.src.replace(/&amp;/g, '&') : '';
                const links = slot.querySelectorAll('a[href*="cn.wsj.com/articles/"]');
                let first = true;
                for (const a of links) {
                    const href = a.getAttribute('href') || '';
                    const url = href.split('?')[0].replace(/[.,;)\\]/]+$/, '');
                    if (seen.has(url)) continue;
                    let title = '';
                    const spans = a.querySelectorAll('span');
                    for (const s of spans) {
                        const t = s.textContent.trim();
                        if (t.length > 5 && t.length < 80) { title = t; break; }
                    }
                    if (!title) title = a.textContent.trim();
                    if (title.length > 80 || title.length < 4) continue;
                    seen.add(url);
                    result.push({url, title, image: first ? imgSrc : ''});
                    first = false;
                    if (result.length >= limit) break;
                }
                if (result.length >= limit) break;
            }
            return result;
        }
        """, limit)
        
        text = await page.inner_text("body")
        await browser.close()
    
    nav_words = {"SKIP TO MAIN CONTENT", "The Wall Street Journal", "订阅", "登录", "华尔街日报"}
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    for a in articles:
        t = a.get("title", "")
        if t and t in lines:
            idx = lines.index(t)
            if idx + 1 < len(lines):
                d = lines[idx + 1]
                if len(d) > 10 and d not in nav_words and not d.startswith("http"):
                    a["summary"] = d
    
    has_img = sum(1 for a in articles if a.get("image"))
    unique_img = len(set(a.get("image", "") for a in articles if a.get("image")))
    print(f"  cn.wsj.com: {len(articles)} 篇（{has_img} 篇有图, {unique_img} 张独立图片）")
    return articles[:limit]


def batch_og_images_with_cookies(urls, timeout=12):
    """One Playwright browser session + cookie for all og:image extractions"""
    cookies = None
    try:
        subprocess.run(["security", "unlock-keychain", "-p", "", 
                       os.path.expanduser("~/Library/Keychains/login.keychain-db")],
                      capture_output=True, timeout=5)
        import browser_cookie3
        cj = browser_cookie3.chrome(domain_name="wsj.com")
        cookies = []
        for c in cj:
            cookies.append({"name": c.name, "value": c.value, "domain": c.domain,
                           "path": c.path or "/", "httpOnly": True, "secure": True, "sameSite": "Lax"})
        try:
            cj_cn = browser_cookie3.chrome(domain_name="cn.wsj.com")
            for c in cj_cn:
                cookies.append({"name": c.name, "value": c.value, "domain": c.domain,
                               "path": c.path or "/", "httpOnly": True, "secure": True, "sameSite": "Lax"})
        except:
            pass
        print(f"  提取到 {len(cookies)} 个 cookie")
    except Exception as e:
        print(f"  [cookie提取失败] {e}")
    
    async def _batch():
        results = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            if cookies:
                try:
                    await context.add_cookies(cookies)
                except:
                    pass
            page = await context.new_page()
            for url in urls:
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
                    if not resp:
                        results[url] = ""
                        continue
                    await page.wait_for_timeout(1500)
                    html = await page.content()
                    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
                    if not m:
                        m = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
                    if not m:
                        m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
                    if m:
                        img = m.group(1)
                        if "/social" in img:
                            img = img.replace("/social", "/large")
                        results[url] = img
                    else:
                        results[url] = ""
                except:
                    results[url] = ""
            await browser.close()
        return results
    
    return asyncio.run(_batch())


def main():
    print(f"[{datetime.now(SH_TZ).strftime('%H:%M:%S')}] WSJ 采集启动")
    t0 = time.time()
    
    seen_urls = load_seen_urls()
    print(f"  去重库: {len(seen_urls)} 条历史记录")
    
    # 1. cn.wsj.com 首页
    print("  === cn.wsj.com ===")
    cn_articles = asyncio.run(scrape_cn_homepage(15))
    for a in cn_articles:
        a['section'] = '🇨🇳 中文版'
        a['source'] = 'cn_home'
    before = len(cn_articles)
    cn_articles = [a for a in cn_articles if a.get("url") not in seen_urls]
    removed = before - len(cn_articles)
    if removed:
        print(f"  去重: 移除 {removed} 篇已发文章")
    
    # 2. RSS (with longer timeout)
    print("  === RSS 4板块 ===")
    rss_articles = []
    for section, rss_url in RSS_FEEDS:
        print(f"  RSS {section}...")
        items = fetch_rss(rss_url)
        # fetch_rss doesn't exist as standalone - let me inline it
        # Actually it does through the import
        sec_arts = []
        seen = set()
        for item in items[:10]:
            if item["link"] and item["link"] not in seen:
                seen.add(item["link"])
                sec_arts.append({**item, "section": section, "source": "rss"})
        taken = sec_arts[:ARTICLES_PER_SECTION]
        rss_articles.extend(taken)
        print(f"    → {len(taken)} 篇")
    
    before = len(rss_articles)
    rss_articles = [a for a in rss_articles if (a.get("link") or a.get("url")) not in seen_urls]
    removed = before - len(rss_articles)
    if removed:
        print(f"  去重: 移除 {removed} 篇已发RSS文章")
    
    all_articles = cn_articles + rss_articles
    
    # 3. Batch og:image with cookies (one Playwright session)
    has_img_before = sum(1 for a in all_articles if a.get("image"))
    no_img = [a for a in all_articles if not a.get("image")]
    if no_img:
        urls_to_fetch = []
        url_to_art = {}
        for a in no_img:
            url = a.get("url") or a.get("link", "")
            if url and ("wsj.com" in url or "cn.wsj.com" in url):
                urls_to_fetch.append(url)
                url_to_art[url] = a
        
        if urls_to_fetch:
            print(f"  Cookie批量提取 {len(urls_to_fetch)} 篇 og:image...")
            results = batch_og_images_with_cookies(list(set(urls_to_fetch)), timeout=12)
            for url, img in results.items():
                if img and url in url_to_art:
                    url_to_art[url]["image"] = img
    
    has_img = sum(1 for a in all_articles if a.get("image"))
    print(f"  配图: {has_img_before} → {has_img}/{len(all_articles)} 篇有图")
    
    # 4. Save dedup
    save_seen_urls(all_articles)
    
    elapsed = time.time() - t0
    print(f"\n  采集总计: CN {len(cn_articles)} + RSS {len(rss_articles)} = {len(all_articles)} 篇")
    print(f"  配图: {has_img}/{len(all_articles)} 篇有图")
    print(f"  采集耗时: {elapsed:.1f}s")
    
    result = {
        "date": datetime.now(SH_TZ).strftime("%Y年%m月%d日"),
        "article_count": len(all_articles),
        "articles": all_articles,
    }
    print("\n===COLLECT_RESULT_START===")
    print(json.dumps(result, ensure_ascii=False))
    print("===COLLECT_RESULT_END===")

if __name__ == "__main__":
    main()