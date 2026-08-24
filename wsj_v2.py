#!/usr/bin/env python3
"""WSJ Full Pipeline - v2"""
import asyncio, json, subprocess, sys, os, re, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from playwright.async_api import async_playwright

SH_TZ = timezone(timedelta(hours=8))
API_KEY = "8TPc5q00vXTLX9fqTeogRg6apSsuxQ5UXxrS_pZquyGNV2VfqVNVgiHy_MO7w_sExIGWXMY3jte3zAvEP7fgmg"
RSS_FEEDS = [
    ("📈 Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("🌍 World", "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
    ("💻 Tech", "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("🏢 Business", "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml"),
]

def llm_call(prompt, system="你是一名专业的财经信息分析师", max_tokens=2000, temp=0.7):
    body = json.dumps({"model": "qwen3.6-plus", "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": temp}).encode()
    req = urllib.request.Request("https://www.sophnet.com/api/open-apis/v1/chat/completions", data=body, headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()

def fetch_rss(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            root = ET.fromstring(r.read())
        items = []
        for item in root.findall(".//item"):
            def t(tag):
                el = item.find(tag)
                return el.text.strip() if el is not None and el.text else ""
            desc = re.sub(r"<[^>]+>", "", t("description"))
            link = t("link").split("?mod=")[0] if "?mod=" in t("link") else t("link")
            media = item.find("{http://search.yahoo.com/mrss/}content")
            img = media.get("url") if media is not None else ""
            items.append({"title": t("title"), "link": link, "summary": desc[:300], "published": t("pubDate"), "image": img})
        return items
    except:
        return []

async def scrape_cn(limit=15):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}, locale="zh-CN",
            extra_http_headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        page = await context.new_page()
        await page.goto("https://cn.wsj.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        for i in range(5):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {(i+1)} / 5)")
            await page.wait_for_timeout(800)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
        
        articles = await page.evaluate("""(limit) => {
            const imgs = Array.from(document.querySelectorAll('img[src*="images.wsj.net"]'))
                .filter(img => img.width > 50 && img.height > 50)
                .map(img => ({src: img.src.replace(/&amp;/g, '&'), c: (img.getBoundingClientRect().top + img.getBoundingClientRect().bottom) / 2}));
            const links = Array.from(document.querySelectorAll('a[href*="cn.wsj.com/articles/"]'));
            const seen = new Set(), used = new Set(), result = [];
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const url = href.split('?')[0].replace(/[.,;)\\]/]+$/, '');
                if (seen.has(url)) continue;
                let title = '';
                for (const s of a.querySelectorAll('span')) {
                    const t = s.textContent.trim();
                    if (t.length > 5 && t.length < 80) { title = t; break; }
                }
                if (!title) title = a.textContent.trim();
                if (title.length > 80 || title.length < 4) continue;
                seen.add(url);
                if (result.length >= limit) break;
                const lc = a.getBoundingClientRect().top + 15;
                let best = '', bestDist = 150;
                for (const img of imgs) {
                    if (used.has(img.src)) continue;
                    const d = Math.abs(lc - img.c);
                    if (d < bestDist) { bestDist = d; best = img.src; }
                }
                if (best) used.add(best);
                result.push({url, title, image: best});
            }
            return result;
        }""", limit)
        
        text = await page.inner_text("body")
        await browser.close()
    
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    nav = {"SKIP TO MAIN CONTENT", "The Wall Street Journal", "订阅", "登录", "华尔街日报"}
    for a in articles:
        t = a.get("title", "")
        if t and t in lines:
            idx = lines.index(t)
            if idx >= 0 and idx + 1 < len(lines) and len(lines[idx+1]) > 10 and lines[idx+1] not in nav:
                a["summary"] = lines[idx+1]
    return articles[:limit]

def main():
    t0 = time.time()
    print("=== 采集 ===")
    cn = asyncio.run(scrape_cn(15))
    for a in cn: a['section'], a['source'] = '🇨🇳 中文版', 'cn_home'
    print(f"cn: {len(cn)} 篇, {sum(1 for a in cn if a.get('image'))} 图")
    
    rss = []
    for sec, url in RSS_FEEDS:
        items = fetch_rss(url, 25)
        taken = [{"title": i["title"], "link": i["link"], "summary": i["summary"], "published": i["published"], "image": i["image"], "section": sec, "source": "rss"} for i in items[:5]]
        rss.extend(taken)
        print(f"rss {sec.split()[0]}: {len(taken)}")
    
    all_arts = cn + rss
    print(f"\n总计: {len(all_arts)} 篇, {sum(1 for a in all_arts if a.get('image'))} 图")
    
    print("\n=== AI摘要 ===")
    for i, a in enumerate(all_arts):
        if a.get("ai_summary"): continue
        if a.get("fulltext") or (a.get("title") and a.get("summary") and len(a["summary"]) > 10):
            ft = a.get("fulltext", "")
            s = a.get("summary", "")
            prompt = f"根据以下新闻标题和简短描述，生成一段100-200字的中文摘要。\n\n标题：{a['title']}\n{'描述：'+s[:500] if s else ''}\n\n要求：用流畅中文段落表述，只输出摘要正文。"
            try:
                r = llm_call(prompt, max_tokens=500)
                a["ai_summary"] = r[3:].strip() if r.startswith("摘要") else r.strip()
                print(f"  [{i+1}] ✓ {a['title'][:30]}")
            except Exception as e:
                print(f"  [{i+1}] ✗ {e}")
    
    # RSS translation
    for a in all_arts:
        if a.get("source") == "rss" and not a.get("ai_summary") and a.get("summary"):
            try:
                r = llm_call(f"将以下英文新闻翻译为中文。第一行输出中文标题，第二行输出中文摘要（100-200字）。\n\n标题：{a['title']}\n摘要：{a['summary']}", max_tokens=500, system="你是一名专业财经翻译员")
                lines = [l.strip() for l in r.split("\n") if l.strip()]
                if len(lines) >= 2: a["title"], a["ai_summary"] = lines[0], lines[1]
                elif len(lines) == 1: a["ai_summary"] = lines[0]
                print(f"  RSS翻译 ✓ {a['title'][:30]}")
            except: pass
        if a.get("source") == "rss" and a.get("ai_summary") and not a.get("title_translated"):
            try:
                r = llm_call(f"翻译为中文：{a['title']}", max_tokens=200)
                if r.strip(): a["title"], a["title_translated"] = r.strip(), True
            except: pass
    
    # Save
    date = datetime.now(SH_TZ).strftime("%Y年%m月%d日")
    date_str = datetime.now(SH_TZ).strftime("%Y-%m-%d")
    gen_at = datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M HKT")
    result = {"date": date, "article_count": len(all_arts), "articles": all_arts}
    with open("/tmp/wsj_final.json", "w") as f:
        json.dump(result, f, ensure_ascii=False)
    
    # Generate HTML
    print("\n=== HTML ===")
    sys.path.insert(1, os.path.expanduser("~/wsj-briefing"))
    from wsj_style_html import generate_html, generate_index
    html = generate_html(all_arts, date, gen_at)
    print("HTML: %d bytes, %d images" % (len(html), html.count('article-img')))
    
    local_docs = os.path.expanduser("~/wsj-briefing-docs")
    os.makedirs(local_docs, exist_ok=True)
    html_file = f"wsj-{date_str}.html"
    with open(f"{local_docs}/{html_file}", "w", encoding="utf-8") as f:
        f.write(html)
    with open(f"{local_docs}/index.html", "w", encoding="utf-8") as f:
        f.write(generate_index(all_arts, date))
    
    # Git push
    print("\n=== Git Push ===")
    repo = os.path.expanduser("~/.openclaw/workspace/wsj-briefing")
    docs = f"{repo}/docs"
    os.makedirs(docs, exist_ok=True)
    for p in [html_file, "index.html"]:
        subprocess.run(["cp", f"{local_docs}/{p}", f"{docs}/{p}"], capture_output=True)
    subprocess.run(["git", "-C", repo, "add", "docs/"], capture_output=True, text=True)
    r = subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["git", "-C", repo, "commit", "-m", f"update briefing {date_str}"], capture_output=True, text=True)
        r2 = subprocess.run(["git", "-C", repo, "push"], capture_output=True, text=True)
        print(f"Push: {'OK' if r2.returncode == 0 else 'FAIL'}")
    else:
        print("No changes")
    
    print(f"\n✓ 完成: {len(all_arts)} 篇, {sum(1 for a in all_arts if a.get('image'))} 图, 耗时: {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()