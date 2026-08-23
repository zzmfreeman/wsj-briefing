#!/usr/bin/env python3
"""WSJ 经典报刊风格 HTML 生成器 — 纯白底 + 衬线正文 + 红色分类 + 细灰线分隔"""

from datetime import datetime, timedelta, timezone

SH_TZ = timezone(timedelta(hours=8))

# 分类 → 颜色映射（WSJ 风格）
SECTION_COLORS = {
    '🇨🇳 中文版':   '#c00',
    '💻 Tech':     '#0066cc',
    '📈 Markets':  '#00853e',
    '🌍 World':    '#333',
    '🏢 Business': '#8b572a',
}


def _card(a, num):
    """生成单篇文章卡片 — WSJ 经典风"""
    title = a.get('title', '')
    url = a.get('url') or a.get('link', '')
    img = a.get('image', '')
    lead = a.get('lead', '') or ''
    summary = a.get('ai_summary', '') or a.get('summary', '') or ''
    section = a.get('section', '')
    pub_time = a.get('published', '')

    # 分类颜色
    sec_color = SECTION_COLORS.get(section, '#333')

    # 图片（大图 hero 风格）
    img_html = ''
    if img:
        img_html = f'<figure class="article-hero"><img src="{img}" alt="" loading="lazy" onerror="this.parentElement.remove()"></figure>'

    # 导语（粗体，灰色背景条）
    lead_html = ''
    if lead:
        lead_html = f'<p class="article-dek">{lead}</p>'

    # 摘要正文
    summary_html = ''
    if summary:
        summary_html = f'<p class="article-body">{summary}</p>'

    # 原文链接
    link_html = f'<a href="{url}" target="_blank" class="article-cta">阅读原文 &rarr;</a>' if url else ''

    # 分类标签
    sec_html = f'<span class="article-section" style="color:{sec_color};border-color:{sec_color}">{section}</span>' if section else ''

    # 发布时间
    time_html = f'<time class="article-time">{pub_time}</time>' if pub_time else ''

    return f'''
    <article class="article-card">
        {img_html}
        <div class="article-content">
            <div class="article-meta">
                {sec_html}{time_html}
            </div>
            <h2 class="article-title"><a href="{url}" target="_blank">{num}. {title}</a></h2>
            {lead_html}
            {summary_html}
            <div class="article-footer">{link_html}</div>
        </div>
    </article>'''


def generate_html(articles, date_str, generated_at):
    """
    生成 WSJ 风格 HTML 页面
    文章已在调用方按权重排序（时间+主题），此处保持原序
    """

    article_num = 0
    all_cards = []
    for a in articles:
        article_num += 1
        all_cards.append(_card(a, article_num))

    cards_html = '\n'.join(all_cards)

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WSJ日报 — {date_str}</title>
    <style>
        /* ── Reset ── */
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

        /* ── WSJ Palette ── */
        :root {{
            --wsj-red: #c00;
            --wsj-black: #111;
            --wsj-text: #333;
            --wsj-gray: #666;
            --wsj-light: #999;
            --wsj-rule: #ddd;
            --wsj-bg: #fff;
            --wsj-dek-bg: #f5f5f5;
            --max-w: 720px;
        }}

        body {{
            font-family: Georgia, 'Times New Roman', Times, serif;
            background: var(--wsj-bg);
            color: var(--wsj-text);
            line-height: 1.65;
            -webkit-font-smoothing: antialiased;
        }}

        /* ── Masthead ── */
        .masthead {{
            max-width: var(--max-w);
            margin: 0 auto;
            padding: 24px 0 12px;
            border-bottom: 3px double var(--wsj-black);
        }}
        .masthead-inner {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
        }}
        .masthead h1 {{
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            font-size: 28px;
            font-weight: 700;
            letter-spacing: -0.5px;
            color: var(--wsj-black);
            text-transform: uppercase;
        }}
        .masthead h1 span {{
            color: var(--wsj-red);
        }}
        .masthead .date {{
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            font-size: 12px;
            color: var(--wsj-light);
            text-transform: uppercase;
            letter-spacing: 1px;
        }}

        /* ── Container ── */
        .container {{
            max-width: var(--max-w);
            margin: 0 auto;
            padding: 0 16px;
        }}

        /* ── Article Card ── */
        .article-card {{
            padding: 20px 0;
            border-bottom: 1px solid var(--wsj-rule);
        }}
        .article-card:last-child {{
            border-bottom: none;
        }}

        /* Hero image */
        .article-hero {{
            margin: 0 0 12px;
        }}
        .article-hero img {{
            width: 100%;
            height: auto;
            display: block;
        }}

        /* Meta: section tag + time */
        .article-meta {{
            margin-bottom: 6px;
        }}
        .article-section {{
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border: 1px solid;
            padding: 1px 6px;
            margin-right: 8px;
        }}
        .article-time {{
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            font-size: 11px;
            color: var(--wsj-light);
        }}

        /* Title */
        .article-title {{
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            font-size: 20px;
            font-weight: 700;
            line-height: 1.25;
            margin: 0 0 8px;
            color: var(--wsj-black);
        }}
        .article-title a {{
            color: inherit;
            text-decoration: none;
        }}
        .article-title a:hover {{
            color: var(--wsj-red);
        }}

        /* Dek (lead) */
        .article-dek {{
            font-size: 15px;
            font-weight: 600;
            color: var(--wsj-text);
            line-height: 1.5;
            margin: 0 0 8px;
            padding: 8px 12px;
            background: var(--wsj-dek-bg);
            border-left: 3px solid var(--wsj-red);
        }}

        /* Body (summary) */
        .article-body {{
            font-size: 14.5px;
            color: var(--wsj-gray);
            line-height: 1.65;
            margin: 0 0 8px;
        }}

        /* CTA */
        .article-footer {{
            margin-top: 4px;
        }}
        .article-cta {{
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            font-size: 12px;
            font-weight: 600;
            color: var(--wsj-red);
            text-decoration: none;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .article-cta:hover {{
            text-decoration: underline;
        }}

        /* ── Footer ── */
        .page-footer {{
            max-width: var(--max-w);
            margin: 0 auto;
            padding: 16px 0 40px;
            border-top: 3px double var(--wsj-black);
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            font-size: 11px;
            color: var(--wsj-light);
            text-align: center;
        }}

        /* ── Mobile ── */
        @media (max-width: 600px) {{
            .masthead h1 {{ font-size: 22px; }}
            .article-title {{ font-size: 17px; }}
            .article-dek {{ font-size: 14px; }}
        }}
    </style>
</head>
<body>
    <header class="masthead">
        <div class="masthead-inner">
            <h1><span>WSJ</span> 日报</h1>
            <span class="date">{date_str}</span>
        </div>
    </header>

    <main class="container">
        {cards_html}
    </main>

    <footer class="page-footer">
        Generated at {generated_at} &middot; Source: Wall Street Journal
    </footer>
</body>
</html>'''


def generate_index(articles, date_str):
    """索引页 — 同样采用 WSJ 风格"""
    items = []
    for i, a in enumerate(articles, 1):
        title = a.get('title', '')
        url = a.get('url') or a.get('link', '')
        section = a.get('section', '')
        items.append(
            f'<li><a href="{url}" target="_blank">{i}. {title}</a>'
            f' <span style="color:#999;font-size:12px">{section}</span></li>'
        )

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WSJ日报 — 索引</title>
<style>
body {{ font-family: Georgia, serif; padding: 20px; max-width: 720px; margin: 0 auto; background: #fff; color: #333; }}
h1 {{ font-family: Helvetica Neue, Arial, sans-serif; font-size: 24px; font-weight: 700; border-bottom: 3px double #111; padding-bottom: 8px; }}
h1 span {{ color: #c00; }}
p {{ font-family: Helvetica Neue, Arial, sans-serif; font-size: 12px; color: #999; }}
li {{ list-style: none; margin: 6px 0; padding: 8px 0; border-bottom: 1px solid #ddd; }}
a {{ color: #111; text-decoration: none; font-size: 14px; }}
a:hover {{ color: #c00; }}
</style>
</head>
<body>
<h1><span>WSJ</span> 日报</h1>
<p>{date_str} &middot; {len(articles)}篇文章</p>
<ul>
{''.join(items)}
</ul>
</body>
</html>'''
