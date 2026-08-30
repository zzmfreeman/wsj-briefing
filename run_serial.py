#!/usr/bin/env python3
"""直接串行跑LLM摘要+生成HTML，绕过ThreadPoolExecutor死锁"""
import json, sys, os, re, subprocess, time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

SH_TZ = timezone(timedelta(hours=8))
sys.path.insert(0, "/Users/zzm/wsj-briefing")

os.chdir("/Users/zzm/wsj-briefing")

# 1. 采集
print("=== 采集 ===")
env = os.environ.copy()
env['SKIP_SAVE_DEDUP'] = '1'
result = subprocess.run(["python3", "remote_collect.py"], capture_output=True, text=True, timeout=600, env=env)
match = re.search(r'===COLLECT_RESULT_START===\n(.*?)\n===COLLECT_RESULT_END===', result.stdout, re.DOTALL)
if not match:
    print("采集失败")
    sys.exit(1)
data = json.loads(match.group(1))
articles = data['articles']
print(f"采集: {len(articles)}篇")

# 2. LLM摘要 — 串行，不用ThreadPool
print("\n=== LLM摘要（串行）===")
from generate_and_publish import llm_call, _make_summary_prompt, _process_one_summary, generate_ai_summaries

# 直接调用 generate_ai_summaries（它内部用ThreadPoolExecutor）
# 但加超时保护
import generate_and_publish as gp

# Monkey-patch: 用串行替代ThreadPool
def patched_generate(articles):
    to_process = []
    for a in articles:
        prompt = _make_summary_prompt(a)
        to_process.append((a, prompt))
    
    completed = 0
    failed = 0
    for a, prompt in to_process:
        try:
            t0 = time.time()
            result = llm_call(prompt, max_tokens=8000)
            t1 = time.time()
            result = result.strip()
            if result.startswith("```"):
                result = re.sub(r'^```(?:json)?\s*', '', result)
                result = re.sub(r'\s*```$', '', result).strip()
            try:
                parsed = json.loads(result)
                bullets = parsed.get("bullets", [])
                insight = parsed.get("insight", "")
                parts = []
                if bullets:
                    parts.append("|||BULLETS|||" + "|||".join(bullets))
                if insight:
                    parts.append("|||INSIGHT|||" + insight)
                combined = "\n".join(parts)
                if combined:
                    a['ai_summary'] = combined
                    completed += 1
                    print(f"  [{completed}/{len(articles)}] {a.get('title','')[:35]}... ✓ ({t1-t0:.1f}s)")
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            # Fallback: treat as plain text
            result = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', result)
            result = re.sub(r'[{}"\[\]]', '', result).strip()
            a['ai_summary'] = result[:300] if result else ""
            if a['ai_summary']:
                completed += 1
                print(f"  [{completed}/{len(articles)}] {a.get('title','')[:35]}... ✓ fallback ({t1-t0:.1f}s)")
            else:
                a['ai_summary'] = "|||BULLETS|||核心事实：摘要生成失败|||关键细节：请阅读原文|||影响：详见报道|||INSIGHT|||"
                failed += 1
                print(f"  [{completed+failed}/{len(articles)}] {a.get('title','')[:35]}... ✗ empty")
        except Exception as e:
            a['ai_summary'] = "|||BULLETS|||核心事实：摘要生成失败|||关键细节：请阅读原文|||影响：详见报道|||INSIGHT|||"
            failed += 1
            print(f"  [{completed+failed}/{len(articles)}] {a.get('title','')[:35]}... ✗ {str(e)[:60]}")
    
    print(f"\n摘要完成: {completed}成功, {failed}失败")
    return articles

articles = patched_generate(articles)

# 3. 翻译标题+生成导语 — 跳过，用已有lead
print("\n=== 翻译标题 ===")
from generate_and_publish import llm_call as _llm_call
import re as _re

translated_count = 0
for i, a in enumerate(articles):
    title = a.get('title', '')
    # 跳过已有中文标题或cn_home文章
    if _re.search(r'[\u4e00-\u9fff]', title) or a.get('source') == 'cn_home':
        a['title_en'] = ''
        continue
    # 保存英文原文
    a['title_en'] = title
    try:
        t0 = time.time()
        result = _llm_call(
            f"任务：英文新闻标题翻译。\n输入：{title}\n输出要求：只输出简体中文翻译，不要解释，不要加引号，不要加前缀。\n参考风格：华尔街日报中文版标题风格，简洁专业。\n现在输出翻译：",
            max_tokens=2000,
            temperature=0.3,
        )
        translated = result.strip()
        # 清理前缀
        for prefix in ["中文翻译：", "中文标题：", "**中文翻译：**", "**中文标题：**",
                       "标准财经标题翻译：", "**标准财经标题译法：**", "推荐翻译：",
                       "**推荐翻译：**", "专业财经标题翻译："]:
            if translated.startswith(prefix):
                translated = translated[len(prefix):].strip()
        translated = _re.sub(r'\*+', '', translated).strip()
        translated = translated.lstrip('`>').rstrip('`').strip()
        if '\n' in translated:
            translated = translated.split('\n')[0].strip()
        # 验证有中文
        if _re.search(r'[\u4e00-\u9fff]', translated) and len(translated) < 200:
            a['title'] = translated
            translated_count += 1
            print(f"  [{i+1}/{len(articles)}] {title[:30]}... → {translated[:25]}")
        else:
            print(f"  [{i+1}/{len(articles)}] {title[:30]}... ✗ 无中文")
    except Exception as e:
        print(f"  [{i+1}/{len(articles)}] {title[:30]}... ✗ {str(e)[:50]}")

print(f"标题翻译: {translated_count}/{len(articles)}")

# 导语处理
print("\n=== 导语处理 ===")
for a in articles:
    if not a.get('lead'):
        a['lead'] = a.get('summary', '') or a.get('fulltext', '')[:200]
    a['lead_en'] = a.get('lead', '')

print(f"导语处理: {sum(1 for a in articles if a.get('lead'))}篇有导语")

# 4. 生成HTML
print("\n=== 生成HTML ===")
from wsj_style_html import generate_html, generate_index

date_str = datetime.now(SH_TZ).strftime("%Y年%m月%d日")
now = datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M HKT")
html_out = generate_html(articles, date_str, now)

out_dir = "/Users/zzm/wsj-briefing-docs"
os.makedirs(out_dir, exist_ok=True)
today = datetime.now(SH_TZ).strftime("%Y-%m-%d")
out_path = f"{out_dir}/wsj-{today}.html"
with open(out_path, "w", encoding="utf-8") as f:
    f.write(html_out)
print(f"HTML: {len(html_out)} bytes")

# 验证
print(f"\n=== 验证 ===")
print(f"field-label: {html_out.count('field-label')}")
print(f"card-thumb: {html_out.count('card-thumb')}")
print(f"figcaption: {html_out.count('figcaption')}")
print(f"核心事实: {html_out.count('核心事实')}")
print(f"洞察: {html_out.count('洞察')}")

# 5. 推送
print("\n=== 推送 ===")
docs_dir = os.path.expanduser("~/wsj-briefing/docs")
os.makedirs(docs_dir, exist_ok=True)
subprocess.run(["cp", out_path, f"{docs_dir}/wsj-{today}.html"])
idx = generate_index(articles, date_str)
with open(f"{docs_dir}/index.html", "w", encoding="utf-8") as f:
    f.write(idx)

repo = os.path.expanduser("~/wsj-briefing")
subprocess.run(["git", "-C", repo, "add", "docs/"], capture_output=True)
r = subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"], capture_output=True)
if r.returncode != 0:
    subprocess.run(["git", "-C", repo, "commit", "-m", f"v37o briefing {today}"], capture_output=True, text=True)
    subprocess.run(["git", "-C", repo, "pull", "--rebase", "origin", "main"], capture_output=True, text=True, timeout=30)
    r2 = subprocess.run(["git", "-C", repo, "push"], capture_output=True, text=True)
    if r2.returncode == 0:
        print("✓ 推送成功")
    else:
        print(f"✗ 推送失败: {r2.stderr[:200]}")
else:
    print("(无变化)")

print(f"\n✓ 完成: https://zzmfreeman.github.io/wsj-briefing/wsj-{today}.html")
