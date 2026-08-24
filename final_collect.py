#!/usr/bin/env python3
"""Run full WSJ briefing with current best data"""
import asyncio, json, subprocess, sys, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~/wsj-briefing"))
from remote_collect import RSS_FEEDS, ARTICLES_PER_SECTION, fetch_rss, load_seen_urls, save_seen_urls
from playwright.async_api import async_playwright

SH_TZ = timezone(timedelta(hours=8))

async def scrape_cn(limit=15):
    """DOM API - only articles with images get images"""
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
                const img = slot.querySelector('img[src*="images.wsj.net"]');
                const imgSrc = img ? img.src.replace(/&amp;/g, '&') : '';
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
            idx = lines.index(t) if t in lines else -1
            if idx >= 0 and idx + 1 < len(lines):
                d = lines[idx + 1]
                if len(d) > 10 and d not in nav_words and not d.startswith("http"):
                    a["summary"] = d
    
    has_img = sum(1 for a in articles if a.get("image"))
    unique = len(set(a.get("image","") for a in articles if a.get("image")))
    print("  cn.wsj.com: %d 篇（%d 篇有图, %d 张独立）" % (len(articles), has_img, unique))
    return articles[:limit]

def main():
    t0 = time.time()
    seen = load_seen_urls()
    
    # 1. CN homepage
    print("=== cn.wsj.com ===")
    cn = asyncio.run(scrape_cn(15))
    for a in cn:
        a['section'] = '🇨🇳 中文版'
        a['source'] = 'cn_home'
    cn = [a for a in cn if a.get("url") not in seen]
    
    # 2. RSS
    print("=== RSS ===")
    rss = []
    for sec, url in RSS_FEEDS:
        items = fetch_rss(url)
        taken = []
        s = set()
        for item in items[:10]:
            if item["link"] and item["link"] not in s:
                s.add(item["link"])
                taken.append({**item, "section": sec, "source": "rss"})
        rss.extend(taken[:ARTICLES_PER_SECTION])
        print("  %s: %d 篇" % (sec, len(taken[:ARTICLES_PER_SECTION])))
    rss = [a for a in rss if (a.get("link") or a.get("url")) not in seen]
    
    all_articles = cn + rss
    save_seen_urls(all_articles)
    
    print("\n总计: %d 篇, 有图: %d" % (len(all_articles), sum(1 for a in all_articles if a.get("image"))))
    print("耗时: %.1fs" % (time.time() - t0))
    
    # Output
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