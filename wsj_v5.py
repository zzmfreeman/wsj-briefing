#!/usr/bin/env python3
"""WSJ Briefing Generator v5 - 完整重写"""
import asyncio, json, subprocess, sys, os, re, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.async_api import async_playwright

SH_TZ = timezone(timedelta(hours=8))
API_KEY = "8TPc5q00vXTLX9fqTeogRg6apSsuxQ5UXxrS_pZquyGNV2VfqVNVgiHy_MO7w_sExIGWXMY3jte3zAvEP7fgmg"
RSS_FEEDS = [
    ("📈 Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("🌍 World", "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
    ("💻 Tech", "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("🏢 Business", "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml"),
]

def llm_call(prompt, system="", max_tokens=2000, temp=0.7):
    msgs = []
    if system: msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = json.dumps({"model": "qwen3.6-plus", "messages": msgs, "max_tokens": max_tokens, "temperature": temp}).encode()
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
            if "marketwatch.com" in link: continue
            media = item.find("{http://search.yahoo.com/mrss/}content")
            img = media.get("url") if media is not None else ""
            if "marketwatch.com" in img or "mktw.net" in img: img = ""
            items.append({"title": t("title"), "url": link, "link": link, "summary": desc[:300], "published": t("pubDate"), "image": img})
        return items
    except: return []

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
        for i in range(8):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight * %d / 8)" % (i+1))
            await page.wait_for_timeout(1500)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1000)
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
                let best = '', bestDist = 500;
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
    seen_urls_file = Path.home() / ".openclaw/workspace/wsj-briefing/seen_urls.json"
    seen_urls = {}
    if seen_urls_file.exists():
        try: seen_urls = json.loads(seen_urls_file.read_text())
        except: seen_urls = {}
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    seen_urls = {url: date for url, date in seen_urls.items() if date >= cutoff}
    p2 = Path.home() / ".openclaw/workspace/wsj-briefing/image_cache.json"
    if p2.exists(): p2.write_text("{}")
    
    print("=== 采集 ===")
    cn = asyncio.run(scrape_cn(15))
    for a in cn: a['section'], a['source'] = '🇨🇳 中文版', 'cn_home'
    print("cn: %d 篇, %d 图" % (len(cn), sum(1 for a in cn if a.get("image"))))
    rss = []
    for sec, url in RSS_FEEDS:
        items = fetch_rss(url, 25)
        for item in items[:5]: item["section"], item["source"] = sec, "rss"
        rss.extend(items[:5])
        print("rss %s: %d" % (sec.split()[0], len(items[:5])))
    all_arts = cn + rss
    print("\n总计: %d 篇, %d 图" % (len(all_arts), sum(1 for a in all_arts if a.get("image"))))
    
    before = len(all_arts)
    all_arts = [a for a in all_arts if (a.get("url") or a.get("link")) not in seen_urls]
    removed = before - len(all_arts)
    if removed: print("跨日去重: 移除 %d 篇已发文章" % removed)
    
    no_img = [a for a in all_arts if not a.get("image")]
    if no_img:
        print("\n=== 图片补抓 ===")
        print("无图: %d 篇" % len(no_img))
        try:
            import browser_cookie3
            subprocess.run(["security", "unlock-keychain", "-p", "", "/Users/zzm/Library/Keychains/login.keychain-db"], capture_output=True, timeout=5)
            cj = browser_cookie3.chrome(domain_name=".wsj.com")
            cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/", "httpOnly": True, "secure": True, "sameSite": "Lax"} for c in cj]
            print("Cookies: %d" % len(cookies))
        except:
            cookies = []
        
        if cookies:
            async def fetch_og():
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True, channel="chrome")
                    context = await browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
                    try: await context.add_cookies(cookies)
                    except: pass
                    page = await context.new_page()
                    for a in no_img:
                        url = a.get("url") or a.get("link")
                        if not url or ("wsj.com" not in url and "cn.wsj.com" not in url): continue
                        try:
                            resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                            if resp and resp.status == 200:
                                await page.wait_for_timeout(1500)
                                html = await page.content()
                                m = re.search(r'<meta[^>]+property=[\'"]og:image[\'"][^>]+content=[\'"]([^\'"]+)[\'"]', html, re.I)
                                if not m: m = re.search(r'content=[\'"]([^\'"]+)[\'"][^>]+property=[\'"]og:image[\'"]', html, re.I)
                                if m:
                                    img = m.group(1)
                                    if "marketwatch.com" not in img and "mktw.net" not in img:
                                        a["image"] = img.replace("/social", "/large") if "/social" in img else img
                                        print("  [og] %s" % url[-30:])
                                    else: print("  [mktw] %s" % url[-30:])
                                else: print("  [无og] %s" % url[-30:])
                            else: print("  [%s] %s" % (resp.status, url[-30:]))
                        except: print("  [失败] %s" % url[-30:])
                    await browser.close()
            asyncio.run(fetch_og())
        else: print("  Cookie 提取失败，跳过")
        print("图片: %d/%d 篇有图" % (sum(1 for a in all_arts if a.get("image")), len(all_arts)))
    
    print("\n=== AI摘要 ===")
    for i, a in enumerate(all_arts):
        if a.get("source") != "cn_home" or a.get("ai_summary"): continue
        s = a.get("summary", "")
        prompt = "根据以下新闻标题和简短描述，生成一段100-200字的中文摘要。\n\n标题：%s\n%s\n\n要求：只输出摘要正文，不要加任何前缀。" % (a['title'], '描述：'+s[:500] if s else '')
        try:
            r = llm_call(prompt, max_tokens=500)
            a["ai_summary"] = r.strip()
            print("  [%d] ✓ %s" % (i+1, a['title'][:30]))
        except Exception as e: print("  [%d] ✗ %s" % (i+1, e))
    
    print("\n=== RSS翻译 ===")
    for a in all_arts:
        if a.get("source") != "rss" or a.get("ai_summary") or not a.get("summary"): continue
        try:
            r = llm_call("翻译以下英文新闻标题和摘要为中文。\n\n标题：%s\n摘要：%s\n\n输出格式：\n中文标题：\n中文摘要：" % (a['title'], a['summary']), max_tokens=500, temp=0.3)
            for l in r.split("\n"):
                l = l.strip()
                if l.startswith("中文标题"): a["title"] = l.split("：", 1)[-1].split(":", 1)[-1].strip()
                elif l.startswith("中文摘要"): a["ai_summary"] = l.split("：", 1)[-1].split(":", 1)[-1].strip()
            a["title_translated"] = True
            print("  ✓ %s" % a['title'][:30])
        except: pass
    
    print("\n=== HTML ===")
    sys.path.insert(1, os.path.expanduser("~/wsj-briefing"))
    from wsj_style_html import generate_html, generate_index
    date = datetime.now(SH_TZ).strftime("%Y年%m月%d日")
    date_str = datetime.now(SH_TZ).strftime("%Y-%m-%d")
    gen_at = datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M HKT")
    for a in all_arts:
        if not a.get("url") and a.get("link"): a["url"] = a["link"]
        if a.get("ai_summary"):
            parts = re.split(r'([。！？])', a["ai_summary"])
            lead = ""
            for idx in range(0, min(len(parts), 5), 2):
                lead += (parts[idx] + parts[idx+1]) if idx+1 < len(parts) else parts[idx]
            a["lead"] = lead[:200]
    html = generate_html(all_arts, date, gen_at)
    img_count = html.count('class="article-img"')
    print("HTML: %d bytes, %d images" % (len(html), img_count))
    local_docs = os.path.expanduser("~/wsj-briefing-docs")
    os.makedirs(local_docs, exist_ok=True)
    html_file = "wsj-%s.html" % date_str
    with open("%s/%s" % (local_docs, html_file), "w", encoding="utf-8") as f: f.write(html)
    with open("%s/index.html" % (local_docs), "w", encoding="utf-8") as f: f.write(generate_index(all_arts, date))
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    for a in all_arts:
        url = a.get("url") or a.get("link")
        if url: seen_urls[url] = today_str
    seen_urls_file.write_text(json.dumps(seen_urls, ensure_ascii=False, indent=2))
    
    print("\n=== Git Push ===")
    repo = os.path.expanduser("~/.openclaw/workspace/wsj-briefing")
    docs = "%s/docs" % repo
    os.makedirs(docs, exist_ok=True)
    for p in [html_file, "index.html"]: subprocess.run(["cp", "%s/%s" % (local_docs, p), "%s/%s" % (docs, p)], capture_output=True)
    subprocess.run(["git", "-C", repo, "add", "docs/"], capture_output=True, text=True)
    r = subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["git", "-C", repo, "commit", "-m", "update briefing %s" % date_str], capture_output=True, text=True)
        r2 = subprocess.run(["git", "-C", repo, "push"], capture_output=True, text=True)
        print("Push: %s" % ("OK" if r2.returncode == 0 else "FAIL"))
    else: print("No changes")
    
    print("\nURL: https://zzmfreeman.github.io/wsj-briefing/%s" % html_file)
    print("完成: %d 篇, %d 图, 耗时: %ds" % (len(all_arts), sum(1 for a in all_arts if a.get("image")), time.time()-t0))

if __name__ == "__main__":
    main()