#!/usr/bin/env python3
"""
WSJ 中文版简报 v6（重构版）
- 模型配置从 openclaw.json 动态读取（R5）
- 模型失败自动通知（R6）
- Cookie 过期检测（R9）
- Discord 推送精简（R8）：只发链接+统计
"""

import json, re, time, random, hashlib, asyncio, urllib.request, subprocess, os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 共享配置 ──────────────────────────────────────────
from config import (
    SCRIPT_DIR, COOKIE_FILE, ARCHIVE_DIR,
    DISCORD_CHANNEL, check_cookie_health, notify_failure,
    call_llm, send_discord_links_only,
)

SEEN_FILE = SCRIPT_DIR / "seen_cn_home.json"

ARTICLES_PER_RUN = 10
SUMMARY_VISIT_LIMIT = 999

SH_TZ = timezone(timedelta(hours=8))

# ── 工具 ──────────────────────────────────────────────
def parse_cookies(path):
    cookies = []
    if not Path(path).exists():
        return cookies
    now_ts = time.time()
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line: continue
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        elif line.startswith('#'):
            continue
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

def slug_to_title(slug):
    title = slug.rsplit('-', 1)[0].replace('-', ' ')
    return title.strip() if title.strip() else "(无标题)"

# ── Playwright 抓首页 ─────────────────────────────────
async def scrape_cn_homepage(limit=30):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    cookies = parse_cookies(COOKIE_FILE)

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 900},
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
        )
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()

        print('  访问 cn.wsj.com 首页...')
        resp = await page.goto('https://cn.wsj.com/', wait_until='domcontentloaded', timeout=30000)
        print(f'  首页状态: {resp.status}')
        await asyncio.sleep(3)

        js_articles = await page.evaluate('''
            () => {
                const results = [];
                const seen = new Set();
                document.querySelectorAll("a[href*='/articles/']").forEach(a => {
                    const url = a.href.split("?")[0];
                    if (seen.has(url)) return;
                    const text = a.innerText.trim();
                    let summary = "";
                    let pubtime = "";
                    const parent = a.closest("li,article,div,section") || a.parentElement;
                    if (parent) {
                        const textEls = parent.querySelectorAll("p, span");
                        for (const el of textEls) {
                            const t = el.innerText.trim();
                            if (t.length > 20 && t !== text) { summary = t; break; }
                        }
                        const timeEl = parent.querySelector("time,[datetime],[data-timestamp]");
                        if (timeEl) {
                            pubtime = timeEl.getAttribute("datetime") ||
                                      timeEl.getAttribute("data-timestamp") ||
                                      timeEl.innerText || "";
                        }
                    }
                    seen.add(url);
                    results.push({url, title: text, summary, pubtime});
                });
                return results;
            }
        ''')

        print(f'  JS 提取: {len(js_articles)} 篇')

        now_sh = datetime.now(SH_TZ)
        today_noon = now_sh.replace(hour=12, minute=0, second=0, microsecond=0)
        yesterday_noon = today_noon - timedelta(days=1)

        articles = []
        for card in js_articles:
            url = card['url']
            if url in [a['url'] for a in articles]:
                continue
            title = card['title'].strip()
            if len(title) < 5:
                slug = url.rsplit('/', 1)[-1]
                title = slug_to_title(slug)

            pubtime_raw = card.get('pubtime', '')
            pub_ts = None
            pub_label = ''
            if pubtime_raw:
                try:
                    import dateutil.parser
                    dt = dateutil.parser.parse(pubtime_raw)
                    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                    pub_ts = dt.timestamp()
                    dt_sh = dt.astimezone(SH_TZ)
                    pub_label = dt_sh.strftime('%m-%d %H:%M')
                except: pub_label = pubtime_raw[:16]

            if pub_ts and pub_ts < (yesterday_noon.timestamp() - 86400):
                continue

            articles.append({
                'url': url, 'title': title,
                'summary': card['summary'].strip(),
                'pub_label': pub_label, 'pubtime': pubtime_raw,
            })

        print(f'  时间过滤后: {len(articles)} 篇')

        # 逐页补摘要
        articles_with_summary = [a for a in articles if a.get('summary')]
        articles_no_summary = [a for a in articles if not a.get('summary')]

        need_summary = articles_no_summary[:min(SUMMARY_VISIT_LIMIT, len(articles_no_summary))]
        if need_summary:
            print(f"  逐页补摘要(Stealth): {len(need_summary)} 篇...")
            async with Stealth().use_async(async_playwright()) as p2:
                browser2 = await p2.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
                )
                context2 = await browser2.new_context(
                    user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                    viewport={'width': 1280, 'height': 900}, locale='zh-CN')
                await context2.add_cookies(cookies)
                page2 = await context2.new_page()
                for art in need_summary:
                    try:
                        resp = await page2.goto(art['url'], wait_until='domcontentloaded', timeout=10000)
                        if resp and resp.status == 200:
                            await asyncio.sleep(1.5)
                            try:
                                intro = await page2.eval_on_selector(
                                    'p[data-type="body"], .article-content p, article p',
                                    'el => el.innerText',
                                )
                                if intro and len(intro) > 30:
                                    art['summary'] = intro[:300]
                            except: pass
                            og_title = await page2.eval_on_selector(
                                'meta[property="og:title"]',
                                'el => el.getAttribute("content") || ""'
                            )
                            if og_title and len(og_title.strip()) > 0:
                                art["title"] = og_title.strip()
                        await asyncio.sleep(0.5)
                    except: pass
                await browser2.close()

        articles_with_summary = [a for a in articles if a.get('summary')]
        articles_no_summary = [a for a in articles if not a.get('summary')]
        final_articles = (articles_with_summary + articles_no_summary)[:limit]
        print(f'  有摘要: {len(articles_with_summary)}, 无摘要: {len(articles_no_summary)}')
        print(f'  最终: {len(final_articles)} 篇')

        await browser.close()
    return final_articles

# ── WSJ Tech RSS 兜底 ───────────────────────────────
WSJ_TECH_RSS = "https://feeds.a.dj.com/rss/RSSWSJD.xml"

def fetch_wsj_tech_rss(limit=6, max_age_days=7):
    try:
        from email.utils import parsedate_to_datetime
        req = urllib.request.Request(WSJ_TECH_RSS, headers={"User-Agent": "Mozilla/5.0 (compatible; RSS/2.0)"})
        with urllib.request.urlopen(req, timeout=15) as r: raw = r.read()
        root = ET.fromstring(raw)
        items = []
        for item in root.findall(".//item"):
            if len(items) >= limit: break
            def t(tag):
                el = item.find(tag)
                return el.text.strip() if el is not None and el.text else ""
            desc = re.sub(r"<[^>]+>", "", t("description"))
            pub_str = t("pubDate")
            try:
                pub_dt = parsedate_to_datetime(pub_str).replace(tzinfo=None)
                if (datetime.now() - pub_dt).total_seconds() > max_age_days * 86400: continue
            except: pass
            items.append({'url': t('link').split('?')[0], 'title': t('title'),
                         'summary': desc[:300], 'pub_label': t('pubDate')[:16],
                         'is_tech': True, 'source': 'WSJ Tech RSS'})
        print(f"  [兜底] WSJ Tech RSS 补充 {len(items)} 篇")
        return items
    except Exception as e:
        print(f"  [兜底] RSS 失败: {e}")
        return []

# ── LLM 生成摘要（使用 config.call_llm）───────────────
def generate_briefing(articles):
    today = datetime.now().strftime('%Y年%m月%d日')

    tech_keywords = ['科技', 'AI', '人工智能', '芯片', '半导体', '科技股', '互联网',
                     '苹果', '谷歌', '微软', '英伟达', '特斯拉', 'Meta', 'OpenAI',
                     '字节', '阿里', '腾讯', '华为', '小米', '大模型', '机器人',
                     'DeepSeek', '量子', '自动驾驶', '数据中心']
    for a in articles:
        title = a.get('title', '') + a.get('summary', '')
        a['is_tech'] = any(kw in title for kw in tech_keywords)

    tech_arts = [a for a in articles if a.get('is_tech')]
    other_arts = [a for a in articles if not a.get('is_tech')]

    nl = '\n'
    def fmt_art(a, is_tech):
        tag = '[科技]' if is_tech else '[其他]'
        summary_status = "✅有导言" if a.get('summary') else "❌缺导言"
        t = f"{tag} {summary_status} 时间：{a.get('pub_label','未知')} 标题：{a['title']}"
        t += nl + f"导言：{a.get('summary','')[:250]}"
        t += nl + f"链接：{a['url']}"
        return t
    _fmt_tech  = nl.join(fmt_art(a, True)  for a in tech_arts)
    _fmt_other = nl.join(fmt_art(a, False) for a in other_arts)

    prompt = f"""你是一名专业财经分析师，以下是今日《华尔街日报》中文版（cn.wsj.com）首页 {len(articles)} 篇文章。

**重要规则**：
- 每篇文章标注了 "✅有导言" 或 "❌缺导言"
- ✅有导言的文章：网页导语必须 verbatim 复制"导言"字段，一字不改
- ❌缺导言的文章：根据标题和链接推断文章内容，在网页导语处写一段推断导语（30-60字，用据推断：开头），AI摘要照常写完整版本

请生成一份专业中文简报（用于归档），格式如下：

📰 **WSJ中文版 · {today}**
> 来源：cn.wsj.com 首页

**💻 科技资讯 — 重点板块**
（所有标注为科技类的文章必须全部列出，每篇格式：）
**【标题】** `MM-DD`
>▎网页导语：（原文导语，一字不改抄录）
**▎AI摘要：**（150-250字连贯段落）
原文：<链接>

**🔥 今日头条（非科技）**
（选出最多5篇最重要的非科技文章，格式同上）

**📋 其他资讯**
（其余文章每篇也要有导语和摘要，格式同科技/头条）
**【标题】** `MM-DD`
>▎网页导语：（推断导语或原文导语）
**▎AI摘要：**（50-100字）
原文：<链接>

要求：
- 链接用 <链接> 格式
- 总长度不超过 2500 字

科技类文章（{len(tech_arts)} 篇）：
{_fmt_tech}

其他文章（{len(other_arts)} 篇）：
{_fmt_other}
"""

    ok, result = call_llm(prompt, max_tokens=4000, temperature=0.3, timeout=450)
    return result

# ── 主流程 ────────────────────────────────────────────
def run():
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] WSJ中文版简报启动 (v6)")

    # R9: Cookie 健康检查
    cookie_ok, cookie_msg = check_cookie_health()
    print(f"  Cookie: {cookie_msg}")
    if not cookie_ok:
        notify_failure(f"⚠️ WSJ中文版简报：Cookie 无效 - {cookie_msg}，请更新 cn_wsj_cookies.txt")

    articles = asyncio.run(scrape_cn_homepage(ARTICLES_PER_RUN))

    if not articles:
        print('  无新文章')
        notify_failure('⚠️ WSJ中文版简报：首页无新文章。')
        return None

    tech_count = sum(1 for a in articles if a.get('is_tech'))
    print(f'  科技类: {tech_count} 篇')
    if tech_count < 2:
        rss_tech = fetch_wsj_tech_rss(limit=6)
        existing_urls = {a['url'] for a in articles}
        rss_tech = [a for a in rss_tech if a['url'] not in existing_urls]
        articles = rss_tech + articles
        print(f'  补充后共 {len(articles)} 篇')

    print(f'  共 {len(articles)} 篇，调用 LLM 生成摘要...')
    briefing = generate_briefing(articles)

    if not briefing:
        today = datetime.now().strftime('%Y年%m月%d日')
        lines = [f'📰 **WSJ中文版 · {today}**\n']
        for a in articles[:15]:
            lines.append(f"• **{a['title']}** <{a['url']}>")
        briefing = '\n'.join(lines)
        # R6: 已由 call_llm 内部通知，此处不再重复

    # 归档
    today_str = datetime.now().strftime('%Y-%m-%d')
    archive_path = ARCHIVE_DIR / f'{today_str}_cn_home.md'
    archive_path.write_text(briefing, encoding='utf-8')
    print(f'  归档: {archive_path}')

    # 保存元数据
    articles_meta = []
    for a in articles:
        if a.get('url'):
            meta_entry = {'url': a['url'], 'title': a['title']}
            if a.get('pubtime'):
                meta_entry['pubtime'] = a.get('pubtime', '')
            if a.get('pub_label'):
                meta_entry['pub_label'] = a.get('pub_label', '')
            articles_meta.append(meta_entry)
    meta_path = ARCHIVE_DIR / f'{today_str}_articles_meta.json'
    meta_path.write_text(json.dumps(articles_meta, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'  元数据: {meta_path} ({len(articles_meta)} 篇)')

    # R8: Discord 只发链接+统计，不发摘要全文
    web_url = f"https://zzmfreeman.github.io/openclaw_macmini_ICnews/wsj-{today_str}.html"
    stats = f"📰 WSJ中文版 | {len(articles)} 篇 | 🌐 <{web_url}>"
    send_discord_links_only(stats)

    print('  ✅ 完成！')
    return briefing

if __name__ == '__main__':
    run()
