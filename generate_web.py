#!/usr/bin/env python3
"""
WSJ 简报网页生成器 v2（回退版）
- 用 Stealth + 全部 Cookie（不过滤域名）
- 逐页访问文章提取 og:image
- 生成带配图的 HTML 页面
- 索引页 + Git push
"""
import json, re, time, subprocess, os, sys, asyncio
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
ARCHIVE_DIR  = SCRIPT_DIR / "archive"
COOKIE_FILE  = SCRIPT_DIR / "cn_wsj_cookies.txt"
WEB_DIR      = Path(os.environ.get("WEB_DIR", "/Users/zzm/.openclaw/workspace/openclaw_macmini_ICnews/docs"))
SITE_BASE    = "https://zzmfreeman.github.io/openclaw_macmini_ICnews"
CACHE_FILE   = SCRIPT_DIR / "image_cache.json"

def parse_cookies(path):
    cookies = []
    if not Path(path).exists(): return cookies
    now_ts = time.time()
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line: continue
        if line.startswith('#HttpOnly_'): line = line[len('#HttpOnly_'):]
        elif line.startswith('#'): continue
        parts = line.split('\t')
        if len(parts) < 7: continue
        domain, _, path_, secure, expires, name, value = parts[:7]
        try:
            exp = int(expires)
            if exp > 0 and exp < now_ts: continue
        except: pass
        c = {'name': name, 'value': value, 'domain': domain.lstrip('.'),
             'path': path_, 'secure': secure.upper() == 'TRUE', 'sameSite': 'None'}
        try:
            exp = int(expires)
            if exp > 0: c['expires'] = exp
        except: pass
        cookies.append(c)
    return cookies

async def fetch_images_playwright(urls, limit=25):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    cookies = parse_cookies(COOKIE_FILE)
    images = {}

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 900},
            locale='zh-CN',
        )
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()

        for i, url in enumerate(urls[:limit]):
            try:
                resp = await page.goto(url, wait_until='domcontentloaded', timeout=10000)
                if resp and resp.status == 200:
                    await asyncio.sleep(1.5)
                    og_img = await page.evaluate('''
                        () => {
                            const el = document.querySelector('meta[property="og:image"]');
                            if (el) return el.getAttribute('content');
                            const el2 = document.querySelector('meta[name="twitter:image"]');
                            if (el2) return el2.getAttribute('content');
                            const img = document.querySelector('article figure img, .article-hero img, [data-type="image"] img');
                            if (img) return img.src;
                            return null;
                        }
                    ''')
                    if og_img and og_img.startswith('http'):
                        images[url] = og_img
                        print(f"  [img] ✓ {i+1}/{len(urls[:limit])}")
                    else:
                        images[url] = ''
                        print(f"  [img] — {i+1} no image found")
                else:
                    images[url] = ''
                    print(f"  [img] ✗ {i+1} status={resp.status if resp else 'None'}")
                await asyncio.sleep(0.5)
            except Exception as e:
                images[url] = ''
                print(f"  [img] ✗ {i+1} {str(e)[:80]}")

        await browser.close()
    return images

def load_image_cache():
    if CACHE_FILE.exists():
        try: return json.loads(CACHE_FILE.read_text())
        except: return {}
    return {}

def save_image_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))

def parse_archive_md(md_text):
    articles = []
    pattern_full = re.compile(
        r'\*\*【(.+?)】\*\*\s*`(\d{2}-\d{2})`\s*\n'
        r'(>▎网页导语：.+?)\n'
        r'\*\*▎AI摘要：\*\*\s*(.+?)\n'
        r'原文：<([^>]+)>',
        re.DOTALL
    )
    for m in pattern_full.finditer(md_text):
        guide_text = m.group(3).strip().lstrip('>▎网页导语：').strip()
        articles.append({
            'title': m.group(1), 'date': m.group(2),
            'guide': guide_text, 'summary': m.group(4).strip(),
            'url': m.group(5), 'has_full': True,
        })
    pattern_bullet = re.compile(
        r'•\s*\*\*【(.+?)】\*\*\s*—\s*\[([^\]]*)\s*([^]]*)\]\s*<([^>]+)>'
    )
    for m in pattern_bullet.finditer(md_text):
        title = m.group(1)
        if any(a['title'] == title for a in articles): continue
        articles.append({
            'title': title, 'guide': m.group(2).strip(),
            'summary': m.group(3).strip(), 'url': m.group(4),
            'has_full': False,
        })
    return articles

def generate_html(articles, date_str, period_label, images):
    tech_kw = ['科技', 'AI', '人工智能', '芯片', '半导体', '互联网', '苹果', '谷歌', '微软',
               '英伟达', '特斯拉', 'Meta', 'OpenAI', '字节', '大模型', '机器人', '数据中心',
               'DeepSeek', '自动驾驶', '英特尔', 'SpaceX', 'Cursor']
    
    tech_articles = []
    headline_articles = []
    other_articles = []
    
    for a in articles:
        text = a.get('title', '') + a.get('guide', '') + a.get('summary', '')
        if a.get('has_full'):
            if any(kw in text for kw in tech_kw):
                tech_articles.append(a)
            elif len(headline_articles) < 6:
                headline_articles.append(a)
            else:
                other_articles.append(a)
        else:
            other_articles.append(a)
    
    def article_card(a):
        img_url = images.get(a.get('url', ''), '')
        img_html = f'<img src="{img_url}" alt="{a["title"]}" loading="lazy" class="article-img">' if img_url else ''
        url_html = f'<a href="{a["url"]}" target="_blank" rel="noopener">🔗 原文链接</a>' if a.get('url') else ''
        
        if a.get('has_full'):
            guide_class = 'guide-missing' if a.get('guide') == '[原文导言未提取]' else 'guide-text'
            return f'''
            <div class="article-card">
                {img_html}
                <h3>{a["title"]}</h3>
                <span class="date-tag">{a.get("date", "")}</span>
                <div class="{guide_class}">▎网页导语：{a["guide"]}</div>
                <div class="ai-summary">▎AI摘要：{a["summary"]}</div>
                {url_html}
            </div>'''
        else:
            return f'''
            <div class="article-card compact">
                {img_html}
                <h4>{a["title"]}</h4>
                <span class="date-tag">{a.get("date", "")}</span>
                <p class="bullet-guide">{a.get("guide", "")} {a.get("summary", "")}</p>
                {url_html}
            </div>'''
    
    tech_html = '\n'.join(article_card(a) for a in tech_articles)
    headline_html = '\n'.join(article_card(a) for a in headline_articles)
    other_html = '\n'.join(article_card(a) for a in other_articles)
    
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WSJ 中文简报 · {date_str}</title>
<style>
body {{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;max-width:800px;margin:0 auto;padding:20px;background:#f8f9fa;color:#333}}
h1 {{color:#1a1a2e;border-bottom:2px solid #e94560;padding-bottom:10px}}
h2 {{color:#e94560;margin-top:30px}}
.article-card {{background:white;border-radius:8px;padding:16px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,0.1);transition:box-shadow 0.2s}}
.article-card:hover {{box-shadow:0 3px 8px rgba(0,0,0,0.15)}}
.article-card.compact {{padding:12px}}
.article-img {{width:100%;height:auto;object-fit:contain;border-radius:6px;margin-bottom:12px;background:#f0f0f0}}
h3 {{color:#16213e;margin:0 0 6px 0}}
h4 {{color:#0f3460;margin:0 0 4px 0}}
.date-tag {{background:#e94560;color:white;padding:2px 10px;border-radius:4px;font-size:0.85em;display:inline-block}}
.guide-text {{color:#555;font-style:italic;padding:6px 0 6px 12px;border-left:3px solid #e94560;margin:8px 0;line-height:1.5}}
.guide-missing {{color:#999;font-style:italic;padding:4px 0}}
.ai-summary {{background:#f0f4ff;padding:10px 14px;border-radius:4px;line-height:1.6;margin:8px 0}}
.bullet-guide {{color:#666;font-size:0.9em;line-height:1.4}}
a {{color:#e94560;text-decoration:none}}
a:hover {{text-decoration:underline}}
footer {{text-align:center;color:#999;margin-top:40px;padding:20px}}
</style>
</head>
<body>
<h1>📰 WSJ 中文简报 · {date_str}</h1>
<p style="color:#666">来源：cn.wsj.com 首页 · {period_label}</p>

<h2>💻 科技资讯</h2>
{tech_html if tech_articles else '<p>今日无科技类报道</p>'}

<h2>🔥 今日头条</h2>
{headline_html if headline_articles else '<p>今日无重点头条</p>'}

<h2>📋 其他资讯</h2>
{other_html if other_articles else '<p>今日无其他资讯</p>'}

<footer>
🤖 模型：GLM-5.1 · Powered by OpenClaw<br>
<a href="{SITE_BASE}/wsj-index.html">← 返回简报索引</a>
</footer>
</body>
</html>'''

def generate_index(existing_pages):
    items = []
    for page in sorted(existing_pages, reverse=True):
        date = page.replace('wsj-', '').replace('.html', '')
        items.append(f'<li><a href="{page}">{date}</a></li>')
    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WSJ 中文简报索引</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:600px;margin:0 auto;padding:20px;background:#f8f9fa}}
h1{{color:#1a1a2e;border-bottom:2px solid #e94560}}
ul{{list-style:none;padding:0}}
li{{background:white;padding:12px 16px;margin:6px 0;border-radius:6px;box-shadow:0 1px 2px rgba(0,0,0,0.1)}}
a{{color:#e94560;text-decoration:none;font-weight:500}}
a:hover{{text-decoration:underline}}
</style></head>
<body><h1>📰 WSJ 中文简报索引</h1><ul>{"".join(items)}</ul>
<footer style="text-align:center;color:#999;margin-top:30px">🤖 Powered by OpenClaw</footer>
</body></html>'''

def cleanup_old_files():
    cutoff = datetime.now() - timedelta(days=30)
    for f in WEB_DIR.glob("wsj-*.html"):
        try:
            if datetime.strptime(f.stem.replace('wsj-', ''), '%Y-%m-%d') < cutoff: f.unlink()
        except: pass
    for f in WEB_DIR.glob("wsj-*.json"):
        try:
            if datetime.strptime(f.stem.replace('wsj-', ''), '%Y-%m-%d') < cutoff: f.unlink()
        except: pass

def git_push():
    if os.environ.get("SKIP_GIT_PUSH") == "1":
        print("  ⏭️ Git push skipped (container mode)")
        return
    ic_dir = Path("/Users/zzm/.openclaw/workspace/openclaw_macmini_ICnews")
    try:
        subprocess.run(['git', 'add', '-A'], cwd=str(ic_dir), check=True)
        subprocess.run(['git', 'commit', '-m', 'wsj-briefing update'], cwd=str(ic_dir), check=True)
        subprocess.run(['git', 'push'], cwd=str(ic_dir), check=True, timeout=30)
        print("  ✅ Git push 成功")
    except Exception as e:
        print(f"  ⚠️ Git push: {e}")

def main():
    date_str = datetime.now().strftime('%Y-%m-%d')
    archive_path = ARCHIVE_DIR / f'{date_str}_cn_home.md'
    if not archive_path.exists():
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        archive_path = ARCHIVE_DIR / f'{yesterday}_cn_home.md'
        date_str = yesterday
    if not archive_path.exists():
        print("  无归档文件，退出")
        return
    
    print(f"  读取归档: {archive_path}")
    md_text = archive_path.read_text(encoding='utf-8')
    articles = parse_archive_md(md_text)
    print(f"  解析到 {len(articles)} 篇文章")
    
    cache = load_image_cache()
    urls_to_fetch = [a['url'] for a in articles if a.get('url') and a['url'] not in cache]
    images = dict(cache)
    
    if urls_to_fetch:
        print(f"  用 Playwright+Stealth 获取 {len(urls_to_fetch)} 张配图...")
        new_images = asyncio.run(fetch_images_playwright(urls_to_fetch))
        images.update(new_images)
    # Use full-size images instead of cropped social variant
    for url in list(images.keys()):
        if images[url] and "/social" in images[url]:
            images[url] = images[url].replace("/social", "/large")
        cache.update(new_images)
        save_image_cache(cache)
    
    has_img = sum(1 for a in articles if images.get(a.get('url', '')))
    print(f"  配图: {has_img}/{len(articles)} 篇有图")
    
    html = generate_html(articles, date_str, "午间简报", images)
    WEB_DIR.mkdir(exist_ok=True)
    page_path = WEB_DIR / f'wsj-{date_str}.html'
    page_path.write_text(html, encoding='utf-8')
    print(f"  ✅ 页面: {page_path}")
    
    json_path = WEB_DIR / f'wsj-{date_str}.json'
    json_path.write_text(json.dumps({
        'date': date_str,
        'generatedAt': datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00'),
        'articles': articles, 'images': images,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    
    existing_pages = [f.name for f in WEB_DIR.glob('wsj-*.html')]
    (WEB_DIR / 'wsj-index.html').write_text(generate_index(existing_pages), encoding='utf-8')
    
    cleanup_old_files()
    git_push()
    
    print(f"\n  🌐 今日: {SITE_BASE}/wsj-{date_str}.html")
    print(f"  🌐 索引: {SITE_BASE}/wsj-index.html")

if __name__ == '__main__':
    main()
