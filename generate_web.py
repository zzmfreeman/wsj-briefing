#!/usr/bin/env python3
"""
WSJ 简报网页生成器 v3
- WSJ 原版风格：serif 字体、深海军蓝、报纸排版
- 每篇标注产生时间（R2）
- 用 Stealth + Cookie 提取 og:image
- Git push 可选（SKIP_GIT_PUSH=1）
"""
import json, re, time, subprocess, os, sys, asyncio
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
ARCHIVE_DIR  = SCRIPT_DIR / "archive"
COOKIE_FILE  = Path(os.environ.get("COOKIE_FILE", str(SCRIPT_DIR / "cn_wsj_cookies.txt")))
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
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-blink-features=AutomationControlled'])
        context = await browser.new_context(user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36', viewport={'width': 1280, 'height': 900}, locale='zh-CN')
        if cookies: await context.add_cookies(cookies)
        page = await context.new_page()
        for i, url in enumerate(urls[:limit]):
            try:
                resp = await page.goto(url, wait_until='domcontentloaded', timeout=10000)
                if resp and resp.status == 200:
                    await asyncio.sleep(1.5)
                    og_img = await page.evaluate('''() => {
                        const el = document.querySelector('meta[property="og:image"]');
                        if (el) return el.getAttribute('content');
                        const el2 = document.querySelector('meta[name="twitter:image"]');
                        if (el2) return el2.getAttribute('content');
                        const img = document.querySelector('article figure img, .article-hero img, [data-type="image"] img');
                        if (img) return img.src;
                        return null;
                    }''')
                    if og_img and og_img.startswith('http'):
                        images[url] = og_img
                        print(f"  [img] ✓ {i+1}/{len(urls[:limit])}")
                    else:
                        images[url] = ''
                else:
                    images[url] = ''
                await asyncio.sleep(0.5)
            except Exception as e:
                images[url] = ''
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
        r'原文：<([^>]+)>', re.DOTALL)
    for m in pattern_full.finditer(md_text):
        guide_text = m.group(3).strip().lstrip('>▎网页导语：').strip()
        articles.append({'title': m.group(1), 'date': m.group(2), 'guide': guide_text, 'summary': m.group(4).strip(), 'url': m.group(5), 'has_full': True})
    # Fallback: bullet list format (• **标题** <url>)
    pattern_bullet_simple = re.compile(r'•\s*\*\*(.+?)\*\*\s*<([^>]+)>')
    for m in pattern_bullet_simple.finditer(md_text):
        title = m.group(1).strip()
        if any(a['title'] == title for a in articles): continue
        articles.append({'title': title, 'guide': '', 'summary': '', 'url': m.group(2).strip(), 'has_full': False})

    pattern_bullet = re.compile(r'•\s*\*\*【(.+?)】\*\*\s*—\s*\[([^\]]*)\s*([^]]*)\]\s*<([^>]+)>')
    for m in pattern_bullet.finditer(md_text):
        title = m.group(1)
        if any(a['title'] == title for a in articles): continue
        articles.append({'title': title, 'guide': m.group(2).strip(), 'summary': m.group(3).strip(), 'url': m.group(4), 'has_full': False})
    return articles

def generate_html(articles, date_str, period_label, images, generated_at):
    """WSJ 原版风格：serif 字体、深海军蓝、报纸排版、细线分隔"""
    tech_kw = ['科技', 'AI', '人工智能', '芯片', '半导体', '互联网', '苹果', '谷歌', '微软',
               '英伟达', '特斯拉', 'Meta', 'OpenAI', '字节', '大模型', '机器人', '数据中心',
               'DeepSeek', '自动驾驶', '英特尔', 'SpaceX', 'Cursor']
    tech_articles, headline_articles, other_articles = [], [], []
    for a in articles:
        text = a.get('title', '') + a.get('guide', '') + a.get('summary', '')
        if a.get('has_full'):
            if any(kw in text for kw in tech_kw): tech_articles.append(a)
            elif len(headline_articles) < 6: headline_articles.append(a)
            else: other_articles.append(a)
        else: other_articles.append(a)

    def article_card(a, idx):
        img_url = images.get(a.get('url', ''), '')
        img_html = f'<img src="{img_url}" alt="{a["title"]}" loading="lazy" class="article-img">' if img_url else ''
        url_html = f'<a href="{a["url"]}" target="_blank" rel="noopener" class="article-link">阅读原文 →</a>' if a.get('url') else ''
        date_tag = f'<span class="date-tag">{a.get("date", "")}</span>' if a.get('date') else ''
        if a.get('has_full'):
            guide_class = 'guide-missing' if a.get('guide') == '[原文导言未提取]' else 'guide-text'
            return f'''
            <article class="article-card">
                {img_html}
                <div class="article-header">
                    <h3>{a["title"]}</h3>
                    {date_tag}
                </div>
                <div class="{guide_class}">{a["guide"]}</div>
                <div class="ai-summary">{a["summary"]}</div>
                <div class="article-footer">{url_html}</div>
            </article>'''
        else:
            return f'''
            <article class="article-card compact">
                {img_html}
                <div class="article-header">
                    <h4>{a["title"]}</h4>
                    {date_tag}
                </div>
                <p class="bullet-guide">{a.get("guide", "")} {a.get("summary", "")}</p>
                <div class="article-footer">{url_html}</div>
            </article>'''

    tech_html = '\n'.join(article_card(a, i) for i, a in enumerate(tech_articles))
    headline_html = '\n'.join(article_card(a, i) for i, a in enumerate(headline_articles))
    other_html = '\n'.join(article_card(a, i) for i, a in enumerate(other_articles))

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WSJ 中文简报 · {date_str}</title>
<style>
/* WSJ 原版风格 */
:root {{
  --wsj-navy: #0274B6;
  --wsj-dark: #111111;
  --wsj-border: #D4D4D4;
  --wsj-bg: #F7F7F7;
  --wsj-card-bg: #FFFFFF;
  --wsj-text: #333333;
  --wsj-text-light: #666666;
  --wsj-accent: #0274B6;
  --wsj-serif: "Exchange", "Georgia", "Times New Roman", serif;
  --wsj-sans: "Retina", "Helvetica Neue", "Arial", sans-serif;
}}
body {{
  font-family: var(--wsj-serif);
  background: var(--wsj-bg);
  color: var(--wsj-text);
  max-width: 780px;
  margin: 0 auto;
  padding: 20px 16px;
  line-height: 1.65;
}}
/* Header */
.site-header {{
  border-bottom: 3px solid var(--wsj-dark);
  padding-bottom: 12px;
  margin-bottom: 24px;
}}
.site-header h1 {{
  font-family: var(--wsj-serif);
  font-size: 1.6em;
  font-weight: 700;
  color: var(--wsj-dark);
  margin: 0 0 4px 0;
  letter-spacing: -0.02em;
}}
.site-header .subtitle {{
  font-family: var(--wsj-sans);
  font-size: 0.85em;
  color: var(--wsj-text-light);
}}
.site-header .generated-at {{
  font-family: var(--wsj-sans);
  font-size: 0.75em;
  color: #999;
  margin-top: 4px;
}}
/* Section headers */
h2 {{
  font-family: var(--wsj-sans);
  font-size: 1.05em;
  font-weight: 700;
  color: var(--wsj-dark);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  border-bottom: 1px solid var(--wsj-border);
  padding-bottom: 6px;
  margin-top: 32px;
  margin-bottom: 16px;
}}
/* Article cards */
.article-card {{
  background: var(--wsj-card-bg);
  border-bottom: 1px solid var(--wsj-border);
  padding: 16px 0;
  margin: 0;
}}
.article-card.compact {{ padding: 12px 0; }}
.article-img {{
  width: 100%;
  height: auto;
  object-fit: contain;
  border-radius: 2px;
  margin-bottom: 12px;
  background: #f0f0f0;
}}
.article-header {{
  margin-bottom: 8px;
}}
.article-header h3 {{
  font-family: var(--wsj-serif);
  font-size: 1.15em;
  font-weight: 700;
  color: var(--wsj-dark);
  margin: 0 0 4px 0;
  line-height: 1.3;
}}
.article-header h4 {{
  font-family: var(--wsj-serif);
  font-size: 1em;
  font-weight: 600;
  color: var(--wsj-dark);
  margin: 0 0 4px 0;
}}
.date-tag {{
  font-family: var(--wsj-sans);
  font-size: 0.75em;
  color: var(--wsj-text-light);
  display: inline-block;
}}
.guide-text {{
  font-family: var(--wsj-serif);
  font-style: italic;
  color: var(--wsj-text-light);
  font-size: 0.92em;
  padding-left: 12px;
  border-left: 2px solid var(--wsj-accent);
  margin: 8px 0;
  line-height: 1.55;
}}
.guide-missing {{
  font-family: var(--wsj-sans);
  color: #999;
  font-size: 0.85em;
  font-style: italic;
  padding: 4px 0;
}}
.ai-summary {{
  font-family: var(--wsj-serif);
  font-size: 0.92em;
  color: var(--wsj-text);
  line-height: 1.6;
  margin: 10px 0;
}}
.bullet-guide {{
  font-family: var(--wsj-serif);
  color: var(--wsj-text-light);
  font-size: 0.88em;
  line-height: 1.5;
}}
.article-footer {{
  margin-top: 8px;
}}
.article-link {{
  font-family: var(--wsj-sans);
  font-size: 0.8em;
  color: var(--wsj-accent);
  text-decoration: none;
  font-weight: 500;
}}
.article-link:hover {{ text-decoration: underline; }}
/* Footer */
.site-footer {{
  border-top: 2px solid var(--wsj-dark);
  margin-top: 40px;
  padding: 16px 0;
  text-align: center;
  font-family: var(--wsj-sans);
  font-size: 0.8em;
  color: #999;
}}
.site-footer a {{
  color: var(--wsj-accent);
  text-decoration: none;
}}
.site-footer a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<header class="site-header">
  <h1>📰 华尔街日报中文简报</h1>
  <div class="subtitle">{date_str} · {period_label} · 来源 cn.wsj.com</div>
  <div class="generated-at">生成时间：{generated_at}</div>
</header>

<h2>💻 科技资讯</h2>
{tech_html if tech_articles else '<p style="color:#999;font-style:italic">今日无科技类报道</p>'}

<h2>🔥 今日头条</h2>
{headline_html if headline_articles else '<p style="color:#999;font-style:italic">今日无重点头条</p>'}

<h2>📋 其他资讯</h2>
{other_html if other_articles else '<p style="color:#999;font-style:italic">今日无其他资讯</p>'}

<footer class="site-footer">
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
body{{font-family:"Exchange","Georgia",serif;max-width:600px;margin:0 auto;padding:20px;background:#F7F7F7;color:#333}}
h1{{font-size:1.4em;border-bottom:3px solid #111;padding-bottom:10px;color:#111}}
ul{{list-style:none;padding:0}}
li{{border-bottom:1px solid #D4D4D4;padding:10px 0}}
a{{font-family:"Retina","Helvetica Neue",sans-serif;color:#0274B6;text-decoration:none;font-weight:500;font-size:0.95em}}
a:hover{{text-decoration:underline}}
</style></head>
<body><h1>📰 WSJ 中文简报索引</h1><ul>{"".join(items)}</ul>
<footer style="text-align:center;color:#999;margin-top:30px;font-family:sans-serif;font-size:0.8em">🤖 Powered by OpenClaw</footer>
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
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S CST')
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
    new_images = {}
    if urls_to_fetch:
        print(f"  用 Playwright+Stealth 获取 {len(urls_to_fetch)} 张配图...")
        new_images = asyncio.run(fetch_images_playwright(urls_to_fetch))
        images.update(new_images)
    for url in list(images.keys()):
        if images[url] and "/social" in images[url]:
            images[url] = images[url].replace("/social", "/large")
        cache.update(new_images)
        save_image_cache(cache)
    has_img = sum(1 for a in articles if images.get(a.get('url', '')))
    print(f"  配图: {has_img}/{len(articles)} 篇有图")
    html = generate_html(articles, date_str, "午间简报", images, generated_at)
    WEB_DIR.mkdir(exist_ok=True)
    page_path = WEB_DIR / f'wsj-{date_str}.html'
    page_path.write_text(html, encoding='utf-8')
    print(f"  ✅ 页面: {page_path}")
    json_path = WEB_DIR / f'wsj-{date_str}.json'
    json_path.write_text(json.dumps({'date': date_str, 'generatedAt': generated_at, 'articles': articles, 'images': images}, ensure_ascii=False, indent=2), encoding='utf-8')
    existing_pages = [f.name for f in WEB_DIR.glob('wsj-*.html')]
    (WEB_DIR / 'wsj-index.html').write_text(generate_index(existing_pages), encoding='utf-8')
    cleanup_old_files()
    git_push()
    print(f"\n  🌐 今日: {SITE_BASE}/wsj-{date_str}.html")
    print(f"  🌐 索引: {SITE_BASE}/wsj-index.html")

if __name__ == '__main__':
    main()
