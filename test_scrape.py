#!/usr/bin/env python3
"""测试首页采集的URL-标题匹配"""
import remote_collect as rc
import asyncio, json

articles = asyncio.run(rc.scrape_cn_homepage_cdp(limit=30))
print(f"Total articles: {len(articles)}")
for a in articles[:5]:
    t = a.get("title", "")[:40]
    u = a.get("url", "")[:80]
    print(f"  title: {t}")
    print(f"  url:   {u}")
    print()

# 找币圈
for a in articles:
    if "币圈" in a.get("title", "") or "彩礼" in a.get("title", ""):
        print("=== 币圈文章 ===")
        print(f"title: {a.get('title', '')}")
        print(f"url:   {a.get('url', '')}")
        print(f"summary: {a.get('summary', '')[:80]}")
        print()

# 找一汽
for a in articles:
    if "一汽" in a.get("title", "") or "重组" in a.get("title", ""):
        print("=== 一汽文章 ===")
        print(f"title: {a.get('title', '')}")
        print(f"url:   {a.get('url', '')}")
        print()

# 找URL里包含"一汽"或"重组"的
import urllib.parse
for a in articles:
    u = urllib.parse.unquote(a.get("url", ""))
    if "一汽" in u or "重组" in u:
        print("=== URL含一汽 ===")
        print(f"title: {a.get('title', '')}")
        print(f"url:   {u}")
        print()
