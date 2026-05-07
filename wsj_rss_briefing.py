#!/usr/bin/env python3
"""
WSJ RSS 简报 v2（重构版）
- 模型配置从 openclaw.json 动态读取（R5）
- 模型失败自动通知（R6）
- Discord 推送精简（R8）
"""

import json, re, time, random, hashlib, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from itertools import groupby

from config import (
    SCRIPT_DIR, ARCHIVE_DIR,
    DISCORD_CHANNEL, notify_failure,
    call_llm, send_discord_links_only, get_git_version,
)

SEEN_FILE = SCRIPT_DIR / "seen_articles.json"
ARTICLES_PER_RUN = 60

RSS_FEEDS = [
    ("📈 Markets",  "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("🌍 World",    "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
    ("💻 Tech",     "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("🏢 Business", "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml"),
]

# ── 去重 ──────────────────────────────────────────────
def load_seen():
    if SEEN_FILE.exists():
        try: return set(json.loads(SEEN_FILE.read_text()))
        except: return set()
    return set()

def save_seen(s):
    SEEN_FILE.write_text(json.dumps(list(s)[-500:]))

def art_id(url):
    return hashlib.md5(url.encode()).hexdigest()

# ── RSS ───────────────────────────────────────────────
def fetch_rss(url):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; RSS/2.0)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
        root = ET.fromstring(raw)
        items = []
        for item in root.findall(".//item"):
            def t(tag):
                el = item.find(tag)
                return el.text.strip() if el is not None and el.text else ""
            desc = re.sub(r"<[^>]+>", "", t("description"))
            items.append({
                "title":     t("title"),
                "link":      t("link"),
                "summary":   desc[:300],
                "published": t("pubDate"),
            })
        return items
    except Exception as e:
        print(f"  [RSS 失败] {url}: {e}")
        return []

# ── LLM 生成摘要 ─────────────────────────────────────
def generate_briefing(articles):
    today = datetime.now().strftime("%Y年%m月%d日")

    article_lines = []
    for i, a in enumerate(articles, 1):
        article_lines.append(
            f"[{i}] 板块：{a['section']}\n"
            f"标题：{a['title']}\n"
            f"摘要：{a['summary']}\n"
            f"链接：{a['link']}"
        )

    prompt = f"""你是一名专业的财经信息分析师。以下是今日《华尔街日报》（WSJ）RSS 抓取的 {len(articles)} 篇文章标题与摘要。

请生成一份**结构化中文简报**（用于归档），严格遵守以下格式：

📰 **WSJ 日报 · {today}**

**💻 Tech（科技）— 重点板块**
（科技类每篇必须全部列出，每篇格式：）
**【标题】**
>▎网页摘要：（原文摘要，一字不改抄录自RSS输入，用灰色引用块呈现）
**▎AI摘要：**（150-250字连贯段落）
原文：<链接>

**📈 Markets（市场）**
**【标题】**
>▎网页摘要：（原文摘要，一字不改）
**▎AI摘要：**（100-180字）
原文：<链接>

**🌍 World（国际）**
**【标题】**
>▎网页摘要：（原文摘要，一字不改）
**▎AI摘要：**（100-150字）
原文：<链接>

**🏢 Business（商业）**
**【标题】**
>▎网页摘要：（原文摘要，一字不改）
**▎AI摘要：**（100-150字）
原文：<链接>

---
**💡 今日重点**
> 1. [优先科技/AI/芯片相关洞察]
> 2. [市场或地缘政治洞察]
> 3. [跨板块综合研判]

要求：
- Tech 板块放最前，所有科技类文章全部列出
- 网页摘要必须 verbatim 复制输入的"摘要"字段
- AI摘要用流畅中文段落，不用要点列表
- 链接用 <链接> 格式
- 总长度不超过 4000 字

文章列表：
---
{chr(10).join(article_lines)}
"""

    ok, result = call_llm(prompt, max_tokens=2500, temperature=0.3, timeout=90)
    return result

# ── 主流程 ────────────────────────────────────────────
def run():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] WSJ RSS 简报启动 (v2)")
    t0 = datetime.now()

    seen = load_seen()
    all_articles = []

    for section, rss_url in RSS_FEEDS:
        print(f"  抓取 {section} RSS...")
        items = fetch_rss(rss_url)
        for item in items:
            url = item["link"]
            if not url or art_id(url) in seen:
                continue
            all_articles.append({**item, "section": section})
        new_count = 0
        for item in items:
            url = item["link"]
            if url and art_id(url) not in seen:
                new_count += 1
        print(f"    → {new_count} 篇新文章")

    t1 = datetime.now()
    rss_delta = (t1 - t0).total_seconds()
    print(f"  RSS抓取耗时: {rss_delta:.1f}s")

    all_articles = all_articles[:ARTICLES_PER_RUN]

    if not all_articles:
        print("  无新文章，跳过")
        send_discord_links_only("📭 WSJ RSS 简报：今日暂无新文章")
        return

    for a in all_articles:
        seen.add(art_id(a["link"]))
    save_seen(seen)

    print(f"  共 {len(all_articles)} 篇，调用 LLM 生成简报...")
    briefing = generate_briefing(all_articles)

    t2 = datetime.now()
    llm_delta = (t2 - t1).total_seconds()
    print(f"  LLM调用耗时: {llm_delta:.1f}s")

    if not briefing:
        # Fallback
        today = datetime.now().strftime("%Y年%m月%d日")
        lines = [f"📰 **WSJ 日报 · {today}**\n"]
        sorted_arts = sorted(all_articles, key=lambda x: x["section"])
        for section, grp in groupby(sorted_arts, key=lambda x: x["section"]):
            lines.append(f"\n**{section}**")
            for a in list(grp):
                lines.append(f"**{a['title']}**")
                lines.append(f"{a['summary'][:120]}...")
                lines.append(f"<{a['link']}>")
        briefing = "\n".join(lines)

    # 归档
    today_str = datetime.now().strftime("%Y-%m-%d")
    archive_path = ARCHIVE_DIR / f"{today_str}.md"
    archive_path.write_text(briefing, encoding="utf-8")
    print(f"  归档: {archive_path}")

    # R8: Discord 只发链接+统计
    web_url = f"https://zzmfreeman.github.io/openclaw_macmini_ICnews/wsj-{today_str}.html"
    ver = get_git_version()
    stats = f"📰 WSJ RSS v{ver} | {len(all_articles)} 篇 | 🌐 <{web_url}>"
    send_discord_links_only(stats)

    t3 = datetime.now()
    total_delta = (t3 - t0).total_seconds()
    print(f"  总耗时: {total_delta:.1f}s")

    print("  ✅ 完成！")
    return briefing

if __name__ == "__main__":
    run()
