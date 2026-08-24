#!/usr/bin/env python3
"""Complete WSJ briefing: scroll for lazy images + long timeout"""
import asyncio, json, subprocess, sys, os, re, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.async_api import async_playwright

SH_TZ = timezone(timedelta(hours=8))

# ── RSS ──
RSS_FEEDS = [
    ("📈 Markets",  "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("🌍 World",    "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
    ("💻 Tech",     "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("🏢 Business", "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml"),
]

def fetch_rss(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            root = ET.fromstring(r.read())
        items = []
        for item in root.findall(".//item"):
            def t(tag):
                el = item.find(tag)
                return el.text.strip() if el is not None and el.text else ""
            desc = re.sub(r"<[^>]+>", "", t("description"))
            link = t("link")
            if "?mod=" in link:
                link = link.split("?mod=")[0]
            media = item.find("{http://search.yahoo.com/mrss/}content")
            img_url = media.get("url") if media is not None else ""
            items.append({"title": t("title"), "link": link,
                         "summary": desc[:300], "published": t("pubDate"),
                         "image": img_url})
        return items
    except Exception as e:
        print(f"  [RSS失败] {url[-30:]}: {e}")
        return []

# ── CN Homepage with SCROLLING for lazy images ──
async def scrape_cn(limit=15):
    print("  抓取 cn.wsj.com 首页（滚动加载图片）...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()
        await page.goto("https://cn.wsj.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        
        # Scroll down to trigger lazy image loading
        for i in range(5):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight * %d / 5)" % (i+1))
            await page.wait_for_timeout(1000)
        
        # Scroll back to top
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
        
        # Now extract all articles with images
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
            idx = lines.index(t)
            if idx >= 0 and idx + 1 < len(lines):
                d = lines[idx + 1]
                if len(d) > 10 and d not in nav_words and not d.startswith("http"):
                    a["summary"] = d
    
    has_img = sum(1 for a in articles if a.get("image"))
    unique = len(set(a.get("image", "") for a in articles if a.get("image")))
    print("  cn.wsj.com: %d 篇（%d 篇有图, %d 张独立）" % (len(articles), has_img, unique))
    return articles[:limit]

# ── Generate ──
def llm_call(prompt, system_msg="你是一名专业的财经信息分析师", max_tokens=2000, temperature=0.7):
    API_KEY = "8TPc5q00vXTLX9fqTeogRg6apSsuxQ5UXxrS_pZquyGNV2VfqVNVgiHy_MO7w_sExIGWXMY3jte3zAvEP7fgmg"
    body = json.dumps({
        "model": "qwen3.6-plus",
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        "https://www.sophnet.com/api/open-apis/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()

def generate_ai_summaries(articles):
    to_process = [a for a in articles if not a.get("ai_summary") and (a.get("fulltext") or (a.get("title") and a.get("summary") and len(a["summary"]) > 10))]
    if not to_process:
        print("  AI摘要: 无须处理")
        return articles
    print(f"  AI摘要: {len(to_process)} 篇...")
    for i, a in enumerate(to_process):
        title = a.get("title", "")
        ft = a.get("fulltext", "")
        summary = a.get("summary", "")
        if ft:
            prompt = f"请为以下文章生成一段150-250字的中文摘要。\n标题：{title}\n\n文章内容：\n{ft[:4000]}\n\n要求：提取核心观点和关键事实，用流畅中文段落表述，150-250字，只输出摘要正文。"
        else:
            prompt = f"根据以下新闻标题和简短描述，生成一段100-200字的中文摘要，补充背景信息并扩展核心内容。\n\n标题：{title}\n描述：{summary[:500]}\n\n要求：用流畅中文段落表述，100-200字，只输出摘要正文。"
        try:
            result = llm_call(prompt, max_tokens=500)
            if result.startswith("摘要：") or result.startswith("摘要:"):
                result = result[3:].strip()
            a["ai_summary"] = result
            print(f"    [{i+1}/{len(to_process)}] ✓ {title[:30]}...")
        except Exception as e:
            print(f"    [{i+1}/{len(to_process)}] ✗ {title[:30]}... {e}")
    ok = sum(1 for a in to_process if a.get("ai_summary"))
    print(f"  AI摘要完成: {ok}/{len(to_process)}")
    return articles

# ── Main ──
def main():
    t0 = time.time()
    
    # 1. Collect cn
    print("=== cn.wsj.com ===")
    cn = asyncio.run(scrape_cn(15))
    for a in cn:
        a['section'] = '🇨🇳 中文版'
        a['source'] = 'cn_home'
    
    # 2. RSS
    print("\n=== RSS ===")
    rss = []
    for sec, url in RSS_FEEDS:
        items = fetch_rss(url, timeout=25)
        taken = []
        s = set()
        for item in items[:10]:
            if item["link"] and item["link"] not in s:
                s.add(item["link"])
                taken.append({**item, "section": sec, "source": "rss"})
        rss.extend(taken[:5])
        print(f"  {sec}: {len(taken[:5])} 篇")
    
    all_articles = cn + rss
    print(f"\n采集: {len(all_articles)} 篇, {sum(1 for a in all_articles if a.get('image'))} 张图")
    print(f"耗时: {time.time()-t0:.1f}s")
    
    # 3. AI summaries
    print("\n=== AI摘要 ===")
    all_articles = generate_ai_summaries(all_articles)
    
    # 4. RSS translation
    rss_no_ft = [a for a in all_articles if a.get("source") == "rss" and not a.get("ai_summary")]
    if rss_no_ft:
        print(f"RSS翻译: {len(rss_no_ft)} 篇...")
        for i, a in enumerate(rss_no_ft):
            try:
                result = llm_call(
                    f"将以下英文新闻翻译为中文。第一行输出中文标题，第二行输出中文摘要（100-200字）。\n\n标题：{a['title']}\n摘要：{a['summary']}",
                    max_tokens=500, system_msg="你是一名专业财经翻译员")
                lines = [l.strip() for l in result.split("\n") if l.strip()]
                if len(lines) >= 2:
                    a["title"] = lines[0]
                    a["ai_summary"] = lines[1]
                elif len(lines) == 1:
                    a["ai_summary"] = lines[0]
                print(f"  [{i+1}/{len(rss_no_ft)}] ✓")
            except Exception as e:
                print(f"  [{i+1}/{len(rss_no_ft)}] ✗ {e}")
    
    # 5. Title translation
    rss_eng = [a for a in all_articles if a.get("source") == "rss" and a.get("ai_summary") and not a.get("title_translated")]
    for a in rss_eng:
        try:
            result = llm_call(f"翻译为中文：{a['title']}", max_tokens=200)
            if result.strip():
                a["title"] = result.strip()
                a["title_translated"] = True
        except:
            pass
    
    # 6. Save result
    result = {
        "date": datetime.now(SH_TZ).strftime("%Y年%m月%d日"),
        "article_count": len(all_articles),
        "articles": all_articles,
    }
    with open("/tmp/wsj_final.json", "w") as f:
        json.dump(result, f, ensure_ascii=False)
    
    print(f"\n✓ 完成: {len(all_articles)} 篇, {sum(1 for a in all_articles if a.get('image'))} 张图")
    print(f"总耗时: {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()