#!/usr/bin/env python3
"""WSJ 仪表盘风格 HTML 生成器 v4
- 顶部统计栏 + 板块饼图
- 左侧板块筛选 + 搜索
- 卡片默认折叠(标题+导语+洞察)，点击展开(图片+摘要bullets)
"""

import re
from datetime import datetime, timedelta, timezone

SH_TZ = timezone(timedelta(hours=8))

SECTION_COLORS = {
    '🇨🇳 中文版':   '#c41e3a',
    '💻 Tech':     '#0066cc',
    '📈 Markets':  '#00853e',
    '🌍 World':    '#333',
    '🏢 Business': '#8b572a',
}


def _parse_summary(summary):
    """解析结构化摘要，返回 (bullets list, insight str)"""
    if not summary:
        return ([], "")

    bullets = []
    insight = ""

    if "|||BULLETS|||" in summary:
        parts = summary.split("|||INSIGHT|||")
        bullet_part = parts[0]
        insight = parts[1].strip() if len(parts) > 1 else ""

        bullet_lines = bullet_part.replace("|||BULLETS|||", "").split("|||")
        bullets = [b.strip() for b in bullet_lines if b.strip()]
        # 确保每条 bullet 有正确前缀
        prefixes = ["核心事实：", "关键细节：", "影响："]
        for i, b in enumerate(bullets):
            if i < len(prefixes) and not b.startswith(prefixes[i]):
                for p in prefixes:
                    if b.startswith(p):
                        b = b[len(p):]
                        break
                bullets[i] = prefixes[i] + b
    else:
        # Fallback: 纯文本摘要，按句号分拆为 bullets
        sentences = re.split(r'(?<=[。！？])\s*', summary)
        bullets = [s.strip() for s in sentences if len(s.strip()) > 10][:3]

    return (bullets, insight)


def _card(a, num):
    """生成单篇文章卡片（仪表盘折叠式）"""
    title = a.get('title', '')
    title_en = a.get('title_en', '')
    url = a.get('url') or a.get('link', '')
    img = a.get('image', '')
    img_thumb = a.get('image_thumb', '')  # 首页缩略图（小图）
    if not img_thumb and img:
        # 从高清图URL生成缩略图URL（WSJ im-XXX 格式）
        img_thumb = re.sub(r'width=\d+', 'width=80', img) if 'width=' in img else img + ('&' if '?' in img else '?') + 'width=80'
    img_caption = a.get('image_caption', '')
    lead = a.get('lead', '') or ''
    lead_en = a.get('lead_en', '')
    summary = a.get('ai_summary', '') or a.get('summary', '') or ''
    section = a.get('section', '')
    pub_time = a.get('published', '')
    source = a.get('source', '')

    sec_color = SECTION_COLORS.get(section, '#333')
    is_rss = source == 'rss'

    # 标题：中文主标题 + 英文副标题
    if is_rss and title_en and title_en != title:
        title_html = f'<span class="field-label">标题</span><h3 class="card-title">{title}</h3><span class="card-title-en">{title_en}</span>'
    else:
        title_html = f'<span class="field-label">标题</span><h3 class="card-title">{title}</h3>'

    # 导语：完整显示（首页可见）
    lead_html = ''
    if lead:
        lead_html = f'<span class="field-label">导语</span><p class="card-dek">{lead}</p>'
    if is_rss and lead_en and lead_en != lead:
        lead_html += f'<p class="card-dek-en">{lead_en}</p>'

    # 结构化摘要
    bullets, insight = _parse_summary(summary)

    # 图片（展开时显示）
    img_html = ''
    if img:
        cap_html = f'<figcaption>{img_caption}</figcaption>' if img_caption else ''
        img_html = f'<figure class="card-hero"><img src="{img}" alt="" loading="lazy" onerror="this.parentElement.style.display=\'none\'">{cap_html}</figure>'

    # bullets（展开时显示）
    bullets_html = ''
    if bullets:
        items = ''.join(f'<li>{b}</li>' for b in bullets)
        bullets_html = f'<ul class="card-bullets">{items}</ul>'
    elif summary:
        bullets_html = f'<p class="card-body-text">{summary}</p>'

    # 洞察（首页可见）
    insight_html = ''
    if insight:
        insight_html = f'<div class="card-insight"><span class="insight-label">洞察</span><p>{insight}</p></div>'

    # 原文链接
    link_html = f'<a href="{url}" target="_blank" class="card-cta">阅读原文 &rarr;</a>' if url else ''

    # 时间
    time_str = ''
    if pub_time:
        try:
            dt = datetime.fromisoformat(pub_time.replace('Z', '+00:00'))
            time_str = dt.astimezone(SH_TZ).strftime('%H:%M')
        except Exception:
            time_str = pub_time[:10]

    return f'''
    <div class="card" data-section="{section}" data-title="{title}">
      <div class="card-header" onclick="toggleCard(this)">
        <span class="card-num">{num:02d}</span>
        <div class="card-titles">{title_html}</div>
        <span class="card-section" style="background:{sec_color}">{section}</span>
        {f'<time class="card-time">{time_str}</time>' if time_str else ''}
        {f'<img class="card-thumb" src="{img_thumb}" loading="lazy">' if img_thumb else ''}
      </div>
      <div class="card-preview">
        {lead_html}
        {insight_html}
      </div>
      <div class="card-detail">
        {img_html}
        {bullets_html}
        {link_html}
      </div>
    </div>'''


def generate_html(articles, date_str, generated_at):
    """生成仪表盘风格 HTML 页面"""

    article_num = 0
    all_cards = []
    for a in articles:
        article_num += 1
        all_cards.append(_card(a, article_num))

    cards_html = '\n'.join(all_cards)

    # 统计板块分布
    sections = {}
    for a in articles:
        s = a.get('section', '')
        sections[s] = sections.get(s, 0) + 1

    # 饼图 CSS conic-gradient
    total = len(articles)
    pie_parts = []
    angle = 0
    for sec in SECTION_COLORS:
        if sec not in sections:
            continue
        count = sections[sec]
        pct = count / total if total else 0
        color = SECTION_COLORS.get(sec, '#333')
        end_angle = angle + pct * 360
        pie_parts.append(f"{color} {angle}deg {end_angle}deg")
        angle = end_angle
    for sec, count in sections.items():
        if sec in SECTION_COLORS:
            continue
        pct = count / total if total else 0
        color = SECTION_COLORS.get(sec, '#333')
        end_angle = angle + pct * 360
        pie_parts.append(f"{color} {angle}deg {end_angle}deg")
        angle = end_angle
    pie_css = " ".join(pie_parts)

    # 板块筛选按钮（按SECTION_COLORS定义顺序排列）
    filter_btns = '<button class="filter-btn active" data-filter="all">全部</button>'
    for sec in SECTION_COLORS:
        if sec in sections:
            count = sections[sec]
            filter_btns += f'<button class="filter-btn" data-filter="{sec}">{sec} <span class="filter-count">{count}</span></button>'
    for sec, count in sections.items():
        if sec not in SECTION_COLORS:
            filter_btns += f'<button class="filter-btn" data-filter="{sec}">{sec} <span class="filter-count">{count}</span></button>'

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WSJ日报 — {date_str}</title>
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

        :root {{
            --bg: #f0f2f5;
            --card-bg: #fff;
            --text: #1a1a1a;
            --text-sec: #666;
            --text-light: #999;
            --accent: #c41e3a;
            --border: #e0e0e0;
            --shadow: 0 1px 4px rgba(0,0,0,0.06);
            --shadow-hover: 0 4px 16px rgba(0,0,0,0.12);
            --insight-bg: #f0f4ff;
            --insight-border: #4a7ab5;
            --dek-bg: #f5f5f5;
        }}

        body {{
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
        }}

        /* ── 顶部统计栏 ── */
        .dashboard-header {{
            background: var(--card-bg);
            border-bottom: 1px solid var(--border);
            padding: 14px 24px;
            position: sticky;
            top: 0;
            z-index: 100;
            display: flex;
            align-items: center;
            gap: 20px;
            flex-wrap: wrap;
        }}
        .dashboard-header h1 {{
            font-size: 22px;
            font-weight: 700;
            color: var(--accent);
            letter-spacing: -0.3px;
        }}
        .dashboard-header h1 span {{
            color: var(--text);
        }}
        .stat-pill {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 13px;
            color: var(--text-sec);
        }}
        .stat-pill strong {{
            color: var(--text);
            font-size: 16px;
        }}

        /* 饼图 */
        .pie-chart {{
            width: 32px;
            height: 32px;
            border-radius: 50%;
            background: conic-gradient({pie_css});
            position: relative;
            flex-shrink: 0;
        }}
        .pie-chart::after {{
            content: '';
            position: absolute;
            top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            width: 16px; height: 16px;
            border-radius: 50%;
            background: var(--card-bg);
        }}

        .search-box {{
            margin-left: auto;
            padding: 6px 14px;
            border: 1px solid var(--border);
            border-radius: 20px;
            font-size: 13px;
            width: 220px;
            outline: none;
            transition: border-color 0.15s;
        }}
        .search-box:focus {{
            border-color: var(--accent);
        }}

        /* ── 主布局 ── */
        .dashboard-body {{
            display: flex;
            max-width: 1200px;
            margin: 0 auto;
            padding: 12px;
        }}

        /* 左侧筛选栏 */
        .sidebar {{
            width: 180px;
            flex-shrink: 0;
            position: sticky;
            top: 64px;
            align-self: flex-start;
            padding: 14px;
            background: var(--card-bg);
            border-radius: 8px;
            box-shadow: var(--shadow);
        }}
        .sidebar h3 {{
            font-size: 11px;
            text-transform: uppercase;
            color: var(--text-light);
            letter-spacing: 1px;
            margin-bottom: 8px;
        }}
        .filter-btn {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            width: 100%;
            text-align: left;
            padding: 6px 10px;
            margin-bottom: 3px;
            border: none;
            background: none;
            font-size: 13px;
            cursor: pointer;
            border-radius: 4px;
            color: var(--text-sec);
            transition: all 0.15s;
        }}
        .filter-btn:hover {{
            background: var(--bg);
        }}
        .filter-btn.active {{
            background: var(--accent);
            color: #fff;
            font-weight: 600;
        }}
        .filter-btn.active .filter-count {{
            color: rgba(255,255,255,0.8);
        }}
        .filter-count {{
            font-size: 11px;
            color: var(--text-light);
            background: var(--bg);
            padding: 1px 6px;
            border-radius: 8px;
        }}
        .filter-btn.active .filter-count {{
            background: rgba(255,255,255,0.2);
        }}

        /* ── 卡片流 ── */
        .card-stream {{
            flex: 1;
            padding: 0 12px;
            min-width: 0;
        }}

        .card {{
            background: var(--card-bg);
            border-radius: 8px;
            box-shadow: var(--shadow);
            margin-bottom: 10px;
            overflow: hidden;
            transition: box-shadow 0.2s;
        }}
        .card:hover {{
            box-shadow: var(--shadow-hover);
        }}
        .card.hidden {{
            display: none;
        }}

        /* 卡片头部（可点击展开） */
        .card-header {{
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 12px 16px;
            cursor: pointer;
            transition: background 0.15s;
        }}
        .card-header:hover {{
            background: #fafafa;
        }}
        .card-num {{
            font-size: 22px;
            font-weight: 800;
            color: var(--border);
            min-width: 30px;
            flex-shrink: 0;
        }}
        .card-titles {{
            flex: 1;
            min-width: 0;
        }}
        .card-title {{
            font-size: 15px;
            font-weight: 600;
            line-height: 1.3;
            color: var(--text);
            overflow: hidden;
            text-overflow: ellipsis;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
        }}
        .card-title-en {{
            font-size: 12px;
            font-style: italic;
            color: var(--text-light);
            display: block;
            margin-top: 2px;
        }}
        .card-section {{
            font-size: 10px;
            font-weight: 600;
            color: #fff;
            padding: 2px 8px;
            border-radius: 10px;
            white-space: nowrap;
            flex-shrink: 0;
        }}
        .card-time {{
            font-size: 11px;
            color: var(--text-light);
            white-space: nowrap;
            flex-shrink: 0;
        }}

        /* 首页可见区域：导语 + 洞察 */
        .card-preview {{
            padding: 0 16px 10px;
        }}

        /* 导语 */
        .card-dek {{
            font-size: 14px;
            font-weight: 500;
            color: var(--text);
            line-height: 1.55;
            padding: 8px 12px;
            background: var(--dek-bg);
            border-left: 3px solid var(--accent);
            border-radius: 0 4px 4px 0;
            margin-bottom: 8px;
        }}
        .card-dek-en {{
            font-size: 12px;
            font-style: italic;
            color: var(--text-sec);
            line-height: 1.5;
            padding: 4px 12px 4px 15px;
            border-left: 3px solid var(--border);
            margin-bottom: 8px;
        }}

        /* 洞察 */
        .card-insight {{
            padding: 8px 12px;
            background: var(--insight-bg);
            border-left: 3px solid var(--insight-border);
            border-radius: 0 4px 4px 0;
        }}
        .insight-label {{
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--insight-border);
            display: block;
            margin-bottom: 2px;
        }}
        .card-insight p {{
            font-size: 13px;
            color: #2a2a2a;
            line-height: 1.5;
            margin: 0;
        }}

        /* 展开区域：图片 + bullets + 链接 */
        .card-detail {{
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.3s ease;
            padding: 0 16px;
        }}
        .card.expanded .card-detail {{
            max-height: 1000px;
            padding: 0 16px 12px;
        }}

        /* 图片 */
        .card-hero {{
            margin: 0 0 10px;
        }}
        .card-hero img {{
            width: 100%;
            height: auto;
            display: block;
            border-radius: 4px;
        }}

        /* 字段标注 */
        .field-label {{
            display: inline-block;
            font-size: 10px;
            font-weight: 700;
            color: var(--text-light);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 3px;
        }}

        /* 首页缩略图 */
        .card-thumb {{
            width: 48px;
            height: 48px;
            border-radius: 4px;
            object-fit: cover;
            flex-shrink: 0;
        }}

        /* 图片说明 */
        .card-hero figcaption {{
            font-size: 12px;
            color: var(--text-sec);
            font-style: italic;
            line-height: 1.4;
            padding: 4px 0 0;
            margin-bottom: 8px;
        }}

        /* Bullets */
        .card-bullets {{
            list-style: none;
            margin-bottom: 10px;
            padding: 0;
        }}
        .card-bullets li {{
            position: relative;
            padding-left: 16px;
            margin-bottom: 6px;
            font-size: 13.5px;
            color: var(--text);
            line-height: 1.55;
        }}
        .card-bullets li::before {{
            content: '';
            position: absolute;
            left: 0;
            top: 8px;
            width: 5px;
            height: 5px;
            border-radius: 50%;
            background: var(--accent);
        }}

        .card-body-text {{
            font-size: 14px;
            color: var(--text-sec);
            line-height: 1.6;
            margin-bottom: 8px;
        }}

        /* CTA */
        .card-cta {{
            font-size: 11px;
            font-weight: 600;
            color: var(--accent);
            text-decoration: none;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .card-cta:hover {{
            text-decoration: underline;
        }}

        /* ── 页脚 ── */
        .page-footer {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 16px 24px 40px;
            font-size: 11px;
            color: var(--text-light);
            text-align: center;
        }}

        @media (max-width: 768px) {{
            .sidebar {{
                display: none;
            }}
            .dashboard-body {{
                padding: 8px;
            }}
            .card-stream {{
                padding: 0;
            }}
            .search-box {{
                width: 140px;
            }}
            .card-title {{
                font-size: 14px;
            }}
        }}
    </style>
</head>
<body>
    <header class="dashboard-header">
        <h1>WSJ <span>日报</span></h1>
        <div class="stat-pill"><strong>{total}</strong> 篇文章</div>
        <div class="pie-chart"></div>
        <input class="search-box" type="text" placeholder="搜索标题..." oninput="filterSearch(this.value)">
    </header>

    <div class="dashboard-body">
        <aside class="sidebar">
            <h3>板块筛选</h3>
            {filter_btns}
        </aside>
        <div class="card-stream">
            {cards_html}
        </div>
    </div>

    <footer class="page-footer">
        Generated at {generated_at} &middot; Source: Wall Street Journal
    </footer>

    <script>
        function toggleCard(header) {{
            const card = header.parentElement;
            card.classList.toggle('expanded');
        }}

        // 板块筛选
        document.querySelectorAll('.filter-btn').forEach(btn => {{
            btn.onclick = function() {{
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                this.classList.add('active');
                const filter = this.dataset.filter;
                document.querySelectorAll('.card').forEach(c => {{
                    if (filter === 'all' || c.dataset.section === filter) {{
                        c.classList.remove('hidden');
                    }} else {{
                        c.classList.add('hidden');
                    }}
                }});
            }};
        }});

        // 搜索
        function filterSearch(q) {{
            q = q.toLowerCase();
            const activeFilter = document.querySelector('.filter-btn.active').dataset.filter;
            document.querySelectorAll('.card').forEach(c => {{
                const title = c.dataset.title.toLowerCase();
                const sectionMatch = (activeFilter === 'all' || c.dataset.section === activeFilter);
                const titleMatch = (!q || title.indexOf(q) !== -1);
                if (sectionMatch && titleMatch) {{
                    c.classList.remove('hidden');
                }} else {{
                    c.classList.add('hidden');
                }}
            }});
        }}
    </script>
</body>
</html>'''


def generate_index(articles, date_str):
    """索引页"""
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
