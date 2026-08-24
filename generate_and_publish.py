#!/usr/bin/env python3
"""生成WSJ日报HTML并发布到GitHub Pages"""
import json, subprocess, re, sys, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wsj_style_html import generate_html, generate_index

SH_TZ = timezone(timedelta(hours=8))

# LiteLLM proxy settings (local Mac Mini, handles routing + retries + rate limiting)
LITELLM_BASE = 'http://localhost:4000/v1'
LITELLM_KEY = 'sk-litellm-master-zzm'
LITELLM_MODEL = 'deepseek-v4-pro'
LITELLM_FALLBACK_MODELS = ["kimi-k3", "glm-5.2", "qwen3.7-max"]


def llm_call(prompt, system_msg="你是一名专业的财经信息分析师", max_tokens=2000, temperature=0.7):
    """调用 LiteLLM 代理（glm-5.2 → deepseek-v4-pro → kimi-k3 → qwen3.7-max fallback链）
    400内容审核失败时自动切换到不审查政治内容的模型"""
    import urllib.request
    import urllib.error

    def _try_model(model, timeout=120):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # Claude/GPT models via Sophnet: use top-level system param
        if model in ("ac-op-4-8-aws",):
            body["messages"] = [{"role": "user", "content": prompt}]
            body["system"] = system_msg
        req = urllib.request.Request(
            f"{LITELLM_BASE}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {LITELLM_KEY}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()

    # Try primary model via LiteLLM
    all_models = [LITELLM_MODEL] + LITELLM_FALLBACK_MODELS
    last_err = None
    for model in all_models:
        try:
            result = _try_model(model)
            if result and len(result.strip()) > 2:
                return result
            # Empty or too-short response, try next model
            print(f"      LiteLLM {model} returned empty/short response, trying next...")
            last_err = f"Empty response from {model}"
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode()[:200]
            except:
                pass
            last_err = f"HTTP {e.code}: {err_body}"
            print(f"      LiteLLM {model} {last_err}")
        except Exception as e:
            last_err = str(e)
            print(f"      LiteLLM {model} error: {e}")
    
    raise RuntimeError(f"All LiteLLM models failed. Last error: {last_err}")




def postprocess_lead(lead):
    # v37b: 清理 ai_summary 的格式标记
    if lead and '|||BULLETS|||' in lead:
        after_b = lead.split('|||BULLETS|||', 1)[1]
        bullets_part = after_b.split('|||INSIGHT|||')[0] if '|||INSIGHT|||' in after_b else after_b
        lead = bullets_part.split('|||')[0].strip()
    """Post-process lead text: strip markdown headers, clean whitespace."""
    if not lead:
        return lead
    # Strip markdown headers (## Title, # Title)
    lead = re.sub(r"^#{1,6}\s+", "", lead, flags=re.MULTILINE)
    # Strip markdown links: [text](url) -> text
    lead = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", lead)
    # Strip leading source citations
    for prefix in ["据《华尔街日报》报道，", "据《华尔街日报》独家报道，", "根据《华尔街日报》报道，",
                    "《华尔街日报》报道，", "报道称，", "据报道，", "据悉，"]:
        if lead.startswith(prefix):
            lead = lead[len(prefix):].strip()
    # v37c: Strip bullet prefixes from ai_summary extraction
    lead = re.sub(r"^\u8981\u70b9[\d]+[\uff1a\u3001]\s*", "", lead)
    # Strip leading/trailing whitespace and newlines
    lead = lead.strip()
    # Collapse multiple spaces
    lead = re.sub(r"\s+", " ", lead)
    return lead


def translate_rss_description_as_lead(rss_summary, title):
    """Translate RSS description to Chinese and use as lead.
    This avoids LLM hallucination from generating leads from AI summaries."""
    if not rss_summary or len(rss_summary) < 20:
        return None
    prompt = f"""将以下英文新闻描述翻译为简体中文，作为新闻导语。要求：
1. 忠实原文，不要添加任何原文没有的信息
2. 50-150字
3. 直接陈述事实，不要用"据《华尔街日报》报道"等来源引用
4. 只输出翻译后的文字，不要解释

标题：{title}

描述：
{rss_summary}"""
    try:
        result = llm_call(prompt, max_tokens=2000, temperature=0.3,
                         system_msg="你是一名专业的财经翻译，只负责忠实翻译，不添加任何原文没有的信息。")
        result = result.strip()
        # Clean up
        result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)
        result = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", result)  # strip markdown links
        for prefix in ["据《华尔街日报》报道，", "据《华尔街日报》独家报道，", "根据《华尔街日报》报道，",
                        "《华尔街日报》报道，", "报道称，", "据报道，", "据悉，"]:
            if result.startswith(prefix):
                result = result[len(prefix):].strip()
        result = result.strip()
        if len(result) < 30:
            return None
        if len(result) > 150:
            sentences = re.split(r"(?<=[。！？])\s*", result)
            short_result = ""
            for s in sentences:
                if len(short_result) + len(s) <= 150:
                    short_result += s
                else:
                    break
            if short_result:
                result = short_result
            else:
                result = result[:150]
        return result
    except Exception as e:
        print(f"      RSS translate lead error: {e}")
        return None


def generate_lead_from_summary(summary, title):
    """Use LLM to generate a proper lead from ai_summary (for RSS articles without fulltext)."""
    if not summary or len(summary) < 50:
        return None
    prompt = f"""请根据以下新闻摘要，重新撰写一段简洁有力的导语。要求：
1. 1-2句话，50-150字
2. 不要用"据《华尔街日报》报道"等来源引用开头
3. 不要用"报道称"等转述词
4. 直接陈述核心事实
5. 语言简洁有力，像新闻编辑写的导语

标题：{title}

摘要：
{summary}

只输出导语文字，不要解释，不要加引号。"""
    try:
        result = llm_call(prompt, max_tokens=2000, temperature=0.5,
                         system_msg="你是一名专业的新闻编辑，擅长撰写简洁有力的导语。")
        result = result.strip()
        # Clean up
        result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)
        result = result.strip()
        # Remove any leading source citations the LLM might add anyway
        for prefix in ["据《华尔街日报》报道，", "据《华尔街日报》独家报道，", "根据《华尔街日报》报道，",
                        "《华尔街日报》报道，", "报道称，", "据报道，", "据悉，"]:
            if result.startswith(prefix):
                result = result[len(prefix):].strip()
        # Validate length
        if len(result) < 30:
            return None
        if len(result) > 150:
            sentences = re.split(r"(?<=[。！？])\s*", result)
            short_result = ""
            for s in sentences:
                if len(short_result) + len(s) <= 150:
                    short_result += s
                else:
                    break
            if short_result:
                result = short_result
            else:
                result = result[:150]
        return result
    except Exception as e:
        print(f"      LLM lead-from-summary error: {e}")
        return None


def generate_lead_from_fulltext(fulltext, title):
    """Use LLM to generate a 1-2 sentence lead from fulltext."""
    if not fulltext or len(fulltext) < 50:
        return None
    # Shorten fulltext to avoid token overflow
    ft_short = fulltext[:3000] if len(fulltext) > 3000 else fulltext
    prompt = f"""Based on the following article, write a 1-2 sentence lead paragraph in Chinese. No markdown, no headers, pure text.

Title: {title}

Article content:
{ft_short}

Requirements:
1. Write 1-2 sentences summarizing the core news
2. 50-150 Chinese characters
3. Pure text only, no formatting
4. Write in Chinese"""
    try:
        result = llm_call(prompt, max_tokens=2000, temperature=0.6,
                         system_msg="你是一名专业的新闻编辑，擅长撰写简洁有力的导语。")
        result = result.strip()
        # Strip any markdown that might have been added
        result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)
        result = result.strip()
        # Validate length: 50-150 chars
        if len(result) < 50:
            return None
        if len(result) > 150:
            # Cut at sentence boundary
            sentences = re.split(r"(?<=[。！？])\s*", result)
            short_result = ""
            for s in sentences:
                if len(short_result) + len(s) <= 150:
                    short_result += s
                else:
                    break
            if short_result:
                result = short_result
            else:
                result = result[:150]
        return result
    except Exception as e:
        print(f"      LLM lead generation error: {e}")
        return None


def clip_summary(summary, max_len=250):
    """Clip summary at sentence boundary if it exceeds max_len."""
    if not summary or len(summary) <= max_len:
        return summary
    # Find sentence boundaries
    sentences = re.split(r"(?<=[。！？])\s*", summary)
    clipped = ""
    for s in sentences:
        if len(clipped) + len(s) <= max_len:
            clipped += s
        else:
            break
    # If we got something, use it; otherwise hard clip
    if clipped:
        return clipped
    return summary[:max_len]


def _make_summary_prompt(a):
    """为单篇文章构建 LLM prompt，返回结构化摘要（要点+洞察）"""
    title = a.get('title', '')
    ft = a.get('fulltext', '')
    summary = a.get('summary', '')
    if ft and ft != title and len(ft) > 50:
        content = ft[:4000] if len(ft) > 4000 else ft
        prompt = f"""请为以下文章生成三段式中文摘要。

标题：{title}

文章内容：
{content}

输出JSON格式（不要加```json标记，直接输出JSON）：
{{
  "bullets": [
    "核心事实：最关键的事实/数字/结论",
    "关键细节：最重要的背景或细节",
    "影响：对谁有什么影响"
  ],
  "insight": "一句话洞察：简短、犀利、触达本质，不复述摘要。"
}}

要求：
1. bullets 每段可多句，简洁有信息量，含关键数字/人名
2. insight 只有一句话，简短、犀利、触达本质，不复述摘要
3. 直接陈述事实，不要用"据报道""报道称"等转述词
4. 不要使用markdown链接格式"""
    elif summary and len(summary) > 10:
        desc = summary[:500]
        prompt = f"""根据以下新闻标题和简短描述，生成三段式中文摘要。

标题：{title}
描述：{desc}

输出JSON格式（不要加```json标记，直接输出JSON）：
{{
  "bullets": [
    "核心事实：最关键的事实/数字/结论",
    "关键细节：最重要的背景或细节",
    "影响：对谁有什么影响"
  ],
  "insight": "一句话洞察：简短、犀利、触达本质，不复述摘要。"
}}

要求：
1. bullets 每段可多句，简洁有信息量，含关键数字/人名
2. insight 只有一句话，简短、犀利、触达本质，不复述摘要
3. 直接陈述事实"""
    else:
        prompt = f"""根据以下新闻标题，生成三段式中文摘要。基于标题主题合理推测文章核心内容。

标题：{title}

输出JSON格式（不要加```json标记，直接输出JSON）：
{{
  "bullets": [
    "核心事实：最关键的事实/数字/结论",
    "关键细节：最重要的背景或细节",
    "影响：对谁有什么影响"
  ],
  "insight": "一句话洞察：简短、犀利、触达本质，不复述摘要。"
}}

要求：
1. bullets 每段可多句，简洁有信息量，含关键数字/人名
2. insight 只有一句话，简短、犀利、触达本质，不复述摘要"""
    return prompt


def _process_one_summary(args):
    """Worker: 调 LLM 生成结构化摘要，返回 (article, result_or_None)。含重试。"""
    a, prompt = args
    for attempt in range(3):
        try:
            result = llm_call(prompt, max_tokens=2000)
            result = result.strip()
            # 去除可能的 markdown json fence
            if result.startswith("```"):
                result = re.sub(r'^```(?:json)?\s*', '', result)
                result = re.sub(r'\s*```$', '', result).strip()
            # 尝试解析 JSON
            try:
                parsed = json.loads(result)
                bullets = parsed.get("bullets", [])
                insight = parsed.get("insight", "")
                # 组合为带分隔标记的文本（HTML生成时解析）
                parts = []
                if bullets:
                    parts.append("|||BULLETS|||" + "|||".join(bullets))
                if insight:
                    parts.append("|||INSIGHT|||" + insight)
                combined = "\n".join(parts)
                if combined:
                    return (a, combined)
            except (json.JSONDecodeError, TypeError):
                # Fallback: 用正则从文本中提取 bullets 和 insight
                bullets = re.findall(r'["\']([^"\']{10,80})["\']', result)
                # Filter out JSON keys
                bullets = [b for b in bullets if b not in ('bullets', 'insight') and not b.startswith('要点')]
                insight_m = re.search(r'(?:insight|"insight")\s*[:：]\s*["\'](.+?)["\']', result, re.S)
                insight = insight_m.group(1) if insight_m else ""
                if bullets:
                    parts = ["|||BULLETS|||" + "|||".join(bullets[:3])]
                    if insight:
                        parts.append("|||INSIGHT|||" + insight)
                    return (a, "\n".join(parts))
                # Last resort: treat as plain text
                result = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", result)
                result = re.sub(r'[{}"\[\]]', '', result).strip()
                for prefix in ["据《华尔街日报》报道，", "据《华尔街日报》独家报道，", "根据《华尔街日报》报道，",
                                "《华尔街日报》报道，", "报道称，", "据报道，", "据悉，"]:
                    if result.startswith(prefix):
                        result = result[len(prefix):].strip()
                result = clip_summary(result, max_len=300)
                return (a, result)
        except Exception:
            if attempt < 2:
                import time
                time.sleep(3 * (attempt + 1))
    return (a, None)


def generate_ai_summaries(articles):
    """用 LLM 并发为所有文章生成摘要（ThreadPoolExecutor, 3 workers + retry）"""
    to_process = []
    for a in articles:
        ft = a.get('fulltext', '')
        summary = a.get('summary', '')
        # Process if: has fulltext, OR has title+summary, OR has title only (title-only is valid for cn_home)
        if not a.get('ai_summary') and (ft or (a.get('title') and len(a.get('title', '')) > 4)):
            to_process.append(a)
    
    if not to_process:
        print("LLM摘要: 无须处理")
        return articles
    
    print(f"LLM摘要: 为 {len(to_process)} 篇文章生成AI摘要（3并发+重试）...")
    
    tasks = [(a, _make_summary_prompt(a)) for a in to_process]
    
    done = 0
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(_process_one_summary, t): t[0] for t in tasks}
        try:
            import concurrent.futures as _cf
            _completed = []
            try:
                for future in as_completed(futures, timeout=1800):
                    _completed.append(future)
            except _cf.TimeoutError:
                _completed = [f for f in futures if f.done()]
            for future in _completed:
                a, result = future.result(timeout=120)
                done += 1
                if result:
                    a['ai_summary'] = result
                    print(f"  [{done}/{len(tasks)}] {a.get('title', '')[:30]}... ✓")
                else:
                    print(f"  [{done}/{len(tasks)}] {a.get('title', '')[:30]}... ✗")
        except Exception as e:
            print(f"  Timeout/Error in summary generation: {e}, continuing with {done}/{len(tasks)} done")
            # Cancel remaining futures
            for f in futures:
                f.cancel()
    
    ok_count = sum(1 for a in to_process if a.get('ai_summary'))
    print(f"LLM摘要完成: {ok_count}/{len(to_process)}")
    return articles



def verify_lead_title_match(articles):
    """验证导语(lead)是否与文章标题匹配。
    检查: exact match, prefix match, multi-line title concatenation。
    错配则清空 lead，用 AI 摘要填充。"""
    title_set = set()
    title_list = []
    for a in articles:
        t = a.get('title', '').strip()
        if t:
            title_set.add(t)
            title_list.append(t)
    
    mismatch_count = 0
    for a in articles:
        lead = (a.get('lead', '') or '').strip()
        if not lead or len(lead) < 10:
            continue
        own_title = a.get('title', '').strip()
        is_mismatch = False
        
        # Check 1: lead is exactly another article's title
        if lead in title_set and lead != own_title:
            is_mismatch = True
        
        # Check 2: lead starts with another article's title (first 15 chars)
        elif len(lead) < 50:
            for t in title_list:
                if t != own_title and len(t) > 10 and lead.startswith(t[:15]):
                    is_mismatch = True
                    break
        
        # Check 3: multi-line lead where each line is another article's title
        # (catches "related articles" section titles concatenated as lead)
        if not is_mismatch and '\n' in lead:
            lines = [l.strip() for l in lead.split('\n') if l.strip()]
            if len(lines) >= 2:
                matched_lines = 0
                for line in lines:
                    for t in title_list:
                        if t != own_title and (line == t or line.startswith(t[:15])):
                            matched_lines += 1
                            break
                if matched_lines >= 2:
                    is_mismatch = True
        
        # Check 4: lead contains 2+ other article titles as substrings
        if not is_mismatch:
            match_count = 0
            for t in title_list:
                if t != own_title and len(t) > 10 and t in lead:
                    match_count += 1
            if match_count >= 2:
                is_mismatch = True
        
        if is_mismatch:
            print(f"  ✗ 导语错配: [{own_title[:30]}] lead={lead[:50]}")
            a['lead'] = ''
            mismatch_count += 1
    
    # Fallback: extract first sentence from ai_summary as lead (NOT full copy — avoid lead=summary duplication)
    for a in articles:
        if not a.get('lead') or len(a.get('lead', '')) < 10:
            if a.get('ai_summary'):
                summary_text = a['ai_summary']
                # Extract first 1-2 sentences as lead
                sentences = re.split(r'(?<=[。！？\"」"])\s*', summary_text)
                sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
                if sentences:
                    a['lead'] = sentences[0] if len(sentences[0]) > 15 else ' '.join(sentences[:2])
                else:
                    a['lead'] = summary_text[:80]
    
    if mismatch_count > 0:
        print(f"  导语错配: 清除 {mismatch_count} 个，已用AI摘要补填")
    else:
        print("  ✓ 导语-标题匹配正常")
    return articles


def verify_image_title_match(articles):
    """用 LLM 验证图片与文章标题是否语义匹配。
    - 有 alt 的图片：直接比对 alt vs 标题
    - 无 alt 的图片（og:image）：用图片 URL 里的 im-ID 无法判断，跳过
    不匹配的图片清除，并尝试重新分配给更合适的文章。"""
    # 只验证有图且有 alt 的文章（alt = 图片描述）
    to_verify = [(i, a) for i, a in enumerate(articles) if a.get('image') and a.get('image_alt', '').strip()]
    
    if not to_verify:
        print("图片验证: 无可验证项（图片无 alt 描述或无图）")
        return articles
    
    print(f"图片验证: 比对 {len(to_verify)} 对 (alt vs 标题)...")
    
    # 构建批量验证 prompt
    pairs = []
    for idx, a in to_verify:
        title = (a.get('title', '') or '')[:60]
        alt = (a.get('image_alt', '') or '')[:80]
        pairs.append(f"{idx}. 标题: {title} | 图片描述: {alt}")
    
    prompt = f"""判断以下每篇文章的标题与其配图描述(alt text)是否匹配。图片描述的内容应与标题主题相关。

{chr(10).join(pairs)}

只返回不匹配的编号（数字），用逗号分隔。如果全部匹配，返回"全部匹配"。不要解释。"""
    
    mismatch_ids = set()
    try:
        result = llm_call(prompt, max_tokens=2000, temperature=0.3,
                         system_msg="你是一名图片匹配验证助手。判断图片描述与文章标题是否语义相关。")
        result = result.strip()
        
        if "全部匹配" in result or "全部" in result:
            print(f"  ✓ 全部 {len(to_verify)} 对匹配")
            return articles
        
        # 解析不匹配的编号
        for m in re.findall(r'\d+', result):
            mismatch_ids.add(int(m))
        
    except Exception as e:
        print(f"  [验证异常] {e}，跳过验证")
        return articles
    
    # 清除不匹配的图片，收集被清除的 (alt, image) 供重分配
    cleared_images = []  # [{alt, src}] 等待重分配
    cleared_count = 0
    for idx, a in to_verify:
        if idx in mismatch_ids:
            alt_val = (a.get('image_alt', '') or '')[:60]
            title_val = (a.get('title', '') or '')[:40]
            print(f"  ✗ 不匹配: [{title_val}] 图片描述={alt_val}")
            cleared_images.append({'alt': a.get('image_alt', ''), 'src': a.get('image', '')})
            a['image'] = ''
            a['image_alt'] = ''
            cleared_count += 1
    
    print(f"  清除: {cleared_count} 张不匹配图片")
    
    # 尝试重分配：把清除的图片匹配到无图文章
    if cleared_images:
        imageless = [a for a in articles if not a.get('image')]
        if imageless and len(cleared_images) > 0:
            # 构建重分配 prompt
            match_pairs = []
            for ci, c in enumerate(cleared_images):
                match_pairs.append(f"图片{ci}: {c['alt'][:60]}")
            for ai, a in enumerate(imageless):
                match_pairs.append(f"文章{ai}: {(a.get('title', '') or '')[:60]}")
            
            reassign_prompt = f"""以下是被清除的图片描述和没有图片的文章标题。请为每张图片找到最匹配的文章。
只返回匹配结果，格式：图片序号->文章序号（如 0->3, 1->5）。无法匹配的返回 图片序号->无。

{chr(10).join(match_pairs)}"""
            
            try:
                result2 = llm_call(reassign_prompt, max_tokens=2000, temperature=0.3,
                                  system_msg="你是一名图片匹配助手。")
                # 解析匹配结果
                for m in re.finditer(r'(\d+)\s*->\s*(\d+|无)', result2):
                    ci = int(m.group(1))
                    target = m.group(2)
                    if target != '无' and ci < len(cleared_images):
                        ai = int(target)
                        if ai < len(imageless):
                            imageless[ai]['image'] = cleared_images[ci]['src']
                            imageless[ai]['image_alt'] = cleared_images[ci]['alt']
                            print(f"  ↻ 重分配: 图片{ci} -> 文章{ai} [{imageless[ai].get('title', '')[:25]}]")
            except Exception as e:
                print(f"  [重分配异常] {e}")
    
    # ── CN 图片池补图 (扩展版) ──
    # 收集所有有 alt 的 CN 图片（alt 就是文章标题），给无图的文章匹配（包括 cn_home 和 rss）
    cn_images = [(a.get('image', ''), a.get('image_alt', '')) for a in articles 
                 if a.get('source') == 'cn_home' and a.get('image') and a.get('image_alt', '').strip()]
    # Also collect RSS images as potential pool
    rss_images = [(a.get('image', ''), a.get('image_alt', '') or a.get('title', '')) for a in articles
                  if a.get('source') != 'cn_home' and a.get('image')]
    all_pool_images = cn_images + rss_images
    # Deduplicate by image URL
    seen_urls = set()
    unique_pool = []
    for src, alt in all_pool_images:
        if src not in seen_urls:
            seen_urls.add(src)
            unique_pool.append((src, alt))
    
    # All articles without images (both cn_home and rss)
    all_imageless = [a for a in articles if not a.get('image')]
    
    if unique_pool and all_imageless:
        print(f"  图片池补图: {len(unique_pool)} 张可用图片 -> {len(all_imageless)} 篇无图文章")
        # 用 LLM 匹配图片描述到文章标题
        match_lines = []
        for ci, (src, alt) in enumerate(unique_pool):
            match_lines.append(f"图片{ci}: {alt[:50]}")
        for ai, a in enumerate(all_imageless[:20]):  # limit for token efficiency
            match_lines.append(f"文章{ai}: {(a.get('title', '') or '')[:50]}")
        
        pool_prompt = f"""以下是图片描述和没有图片的文章标题。请为每篇文章找到最匹配的图片。
只返回匹配结果，格式：文章序号->图片序号（如 0->3, 1->5）。无法匹配的返回 文章序号->无。

{chr(10).join(match_lines)}"""
        
        try:
            result3 = llm_call(pool_prompt, max_tokens=2000, temperature=0.3,
                              system_msg="你是一名图片匹配助手。")
            reassigned = 0
            for m in re.finditer(r'(\d+)\s*->\s*(\d+|无)', result3):
                ai = int(m.group(1))
                target = m.group(2)
                if target != '无' and ai < len(all_imageless):
                    ci = int(target)
                    if ci < len(unique_pool):
                        src, alt = unique_pool[ci]
                        already_used = any(a.get('image') == src for a in all_imageless)
                        if not already_used:
                            all_imageless[ai]['image'] = src
                            all_imageless[ai]['image_alt'] = alt
                            reassigned += 1
            if reassigned > 0:
                print(f"  ↻ 图片池补图: {reassigned} 篇文章获得配图")
        except Exception as e:
            print(f"  [CN图片池补图异常] {e}")
    
    final_has_img = sum(1 for a in articles if a.get('image'))
    print(f"  最终: {final_has_img}/{len(articles)} 篇有图")
    return articles
    
    print(f"图片验证: 比对 {len(to_verify)} 对 (alt vs 标题)...")
    
    # 构建批量验证 prompt
    pairs = []
    for idx, a in to_verify:
        title = (a.get('title', '') or '')[:60]
        alt = (a.get('image_alt', '') or '')[:80]
        pairs.append(f"{idx}. 标题: {title} | 图片描述: {alt}")
    
    prompt = f"""判断以下每篇文章的标题与其配图描述(alt text)是否匹配。图片描述的内容应与标题主题相关。

{chr(10).join(pairs)}

只返回不匹配的编号（数字），用逗号分隔。如果全部匹配，返回"全部匹配"。不要解释。"""
    
    mismatch_ids = set()
    try:
        result = llm_call(prompt, max_tokens=2000, temperature=0.3,
                         system_msg="你是一名图片匹配验证助手。判断图片描述与文章标题是否语义相关。")
        result = result.strip()
        
        if "全部匹配" in result or "全部" in result:
            print(f"  ✓ 全部 {len(to_verify)} 对匹配")
            return articles
        
        # 解析不匹配的编号
        for m in re.findall(r'\d+', result):
            mismatch_ids.add(int(m))
        
    except Exception as e:
        print(f"  [验证异常] {e}，跳过验证")
        return articles
    
    # 清除不匹配的图片，收集被清除的 (alt, image) 供重分配
    cleared_images = []  # [{alt, src}] 等待重分配
    cleared_count = 0
    for idx, a in to_verify:
        if idx in mismatch_ids:
            alt_val = (a.get('image_alt', '') or '')[:60]
            title_val = (a.get('title', '') or '')[:40]
            print(f"  ✗ 不匹配: [{title_val}] 图片描述={alt_val}")
            cleared_images.append({'alt': a.get('image_alt', ''), 'src': a.get('image', '')})
            a['image'] = ''
            a['image_alt'] = ''
            cleared_count += 1
    
    print(f"  清除: {cleared_count} 张不匹配图片")
    
    # 尝试重分配：把清除的图片匹配到无图文章
    if cleared_images:
        imageless = [a for a in articles if not a.get('image')]
        if imageless and len(cleared_images) > 0:
            # 构建重分配 prompt
            match_pairs = []
            for ci, c in enumerate(cleared_images):
                match_pairs.append(f"图片{ci}: {c['alt'][:60]}")
            for ai, a in enumerate(imageless):
                match_pairs.append(f"文章{ai}: {(a.get('title', '') or '')[:60]}")
            
            reassign_prompt = f"""以下是被清除的图片描述和没有图片的文章标题。请为每张图片找到最匹配的文章。
只返回匹配结果，格式：图片序号->文章序号（如 0->3, 1->5）。无法匹配的返回 图片序号->无。

{chr(10).join(match_pairs)}"""
            
            try:
                result2 = llm_call(reassign_prompt, max_tokens=2000, temperature=0.3,
                                  system_msg="你是一名图片匹配助手。")
                # 解析匹配结果
                for m in re.finditer(r'(\d+)\s*->\s*(\d+|无)', result2):
                    ci = int(m.group(1))
                    target = m.group(2)
                    if target != '无' and ci < len(cleared_images):
                        ai = int(target)
                        if ai < len(imageless):
                            imageless[ai]['image'] = cleared_images[ci]['src']
                            imageless[ai]['image_alt'] = cleared_images[ci]['alt']
                            print(f"  ↻ 重分配: 图片{ci} -> 文章{ai} [{imageless[ai].get('title', '')[:25]}]")
            except Exception as e:
                print(f"  [重分配异常] {e}")
    
    final_has_img = sum(1 for a in articles if a.get('image'))
    print(f"  最终: {final_has_img}/{len(articles)} 篇有图")
    return articles


if __name__ == "__main__":
    # 刷新 wsj.com cookies（从 Chrome 提取，用于突破付费墙获取 og:image）
    print("=== 刷新WSJ Cookies ===")
    try:
        from remote_collect import refresh_wsj_cookies
        refresh_wsj_cookies()
    except Exception as e:
        print(f"  Cookie刷新失败: {e}")
    
    # 采集 (seen_urls.json is managed by remote_collect.py — load + save, NEVER clear)
    # Pitfall #17a: clearing seen_urls.json at startup destroys 7-day dedup history
    
    print("=== 采集文章 ===")
    env = os.environ.copy()
    env['SKIP_SAVE_DEDUP'] = '1'
    result = subprocess.run(['python3', 'remote_collect.py'], capture_output=True, text=True, timeout=1800, env=env)
    match = re.search(r'===COLLECT_RESULT_START===\n(.*?)\n===COLLECT_RESULT_END===', result.stdout, re.DOTALL)
    if not match:
        print('采集失败', file=sys.stderr)
        print(result.stdout[:500], file=sys.stderr)
        sys.exit(1)

    data = json.loads(match.group(1))
    print(f"采集: {data['date']}, {data['article_count']}篇文章")

    articles = data['articles']

    # 最低文章数保护: <5篇说明采集异常，不发布空HTML
    MIN_ARTICLES = 3
    if len(articles) < MIN_ARTICLES:
        print(f"采集到 {len(articles)} 篇文章 (最低要求 {MIN_ARTICLES} 篇)，跳过发布。可能原因: cn.wsj.com未更新/去重库过大/反爬拦截。")
        sys.exit(1)
    today = data['date']
    gen_at = datetime.now(SH_TZ).strftime('%Y-%m-%d %H:%M HKT')

    # 生成 AI 摘要
    print("\n=== 生成AI摘要 ===")
    articles = generate_ai_summaries(articles)

    # RSS fallback: 并发翻译标题+摘要
    rss_no_ft = [a for a in articles if a.get('source') == 'rss' and not a.get('ai_summary')]
    if rss_no_ft:
        print(f"RSS fallback: 并发翻译 {len(rss_no_ft)} 篇（3并发+重试）...")
        
        def _translate_one(a):
            title = a.get('title', '')
            summary = a.get('summary', '')
            prompt = f"将以下英文新闻标题和摘要翻译为简体中文。\\n\\n标题：{title}\\n摘要：{summary}\\n\\n输出格式：\\n中文标题：\\n中文摘要："
            for attempt in range(3):
                try:
                    result = llm_call(prompt, max_tokens=2000, temperature=0.3, system_msg="你是一名专业财经翻译员")
                    lines = [l.strip() for l in result.split("\\n") if l.strip()]
                    if len(lines) >= 2:
                        title_cn = lines[0]
                        for prefix in ["中文标题：", "中文标题:", "**中文标题：**"]:
                            if title_cn.startswith(prefix):
                                title_cn = title_cn[len(prefix):].strip()
                        title_cn = re.sub(r'\\*+', '', title_cn).strip()
                        a['title'] = title_cn
                        summary_cn = lines[1]
                        for prefix in ["中文摘要：", "中文摘要:", "**中文摘要：**"]:
                            if summary_cn.startswith(prefix):
                                summary_cn = summary_cn[len(prefix):].strip()
                        a['ai_summary'] = summary_cn
                    elif len(lines) == 1:
                        a['ai_summary'] = lines[0]
                    return (a, True)
                except Exception:
                    if attempt < 2:
                        import time
                        time.sleep(3 * (attempt + 1))
            return (a, False)
        
        done = 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = {pool.submit(_translate_one, a): a for a in rss_no_ft}
            import concurrent.futures as _cf
            _completed = []
            try:
                for future in as_completed(futures, timeout=1800):
                    _completed.append(future)
            except _cf.TimeoutError:
                _completed = [f for f in futures if f.done()]
            for future in _completed:
                a, ok = future.result()
                done += 1
                status = "✓" if ok else "✗"
                print(f"  [{done}/{len(rss_no_ft)}] {a.get('title', '')[:30]}... {status}")
        ok_count = sum(1 for a in rss_no_ft if a.get('ai_summary'))
        print(f"RSS翻译完成: {ok_count}/{len(rss_no_ft)}")

    # 翻译有全文AI摘要但标题还是英文的RSS文章（并发）
    rss_eng_title = [a for a in articles if a.get('source') == 'rss' and a.get('ai_summary') and not a.get('title_translated')]
    if rss_eng_title:
        print(f"RSS标题翻译: {len(rss_eng_title)} 篇（3并发+重试）...")
        
        def _translate_title_one(a):
            title = a.get('title', '')
            for attempt in range(3):
                try:
                    result = llm_call(
                        f"任务：英文新闻标题翻译。\n输入：{title}\n输出要求：只输出简体中文翻译，不要解释，不要加引号，不要加前缀。\n参考风格：华尔街日报中文版标题风格，简洁专业。\n现在输出翻译：",
                        max_tokens=2000,
                        temperature=0.3,
                    )
                    translated = result.strip()
                    for prefix in ["中文翻译：", "中文标题：", "**中文翻译：**", "**中文标题：**",
                                   "标准财经标题翻译：", "**标准财经标题译法：**", "推荐翻译：",
                                   "**推荐翻译：**", "专业财经标题翻译："]:
                        if translated.startswith(prefix):
                            translated = translated[len(prefix):].strip()
                    translated = re.sub(r'\*+', '', translated).strip()
                    translated = translated.lstrip('`>「"').rstrip('`」"').strip()
                    if '\n' in translated:
                        translated = translated.split(chr(10))[0].strip()
                    for marker in ['（注', '💡', '📊', '📌', '💡 *']:
                        idx = translated.find(marker)
                        if idx > 5:
                            translated = translated[:idx].strip()
                    # 验证翻译结果必须包含中文字符（防止 LLM 返回 meta-text）
                    if not re.search(r'[\u4e00-\u9fff]', translated):
                        print(f"    [标题翻译] 无中文字符, attempt={attempt}: {translated[:50]}")
                        continue
                    # v37: 拒绝 LLM reasoning text（thinking 模型泄漏内部推理）
                    if re.search(r'The user wants|I need to translate|I should translate|让我翻译|用户想要', translated, re.IGNORECASE):
                        print(f"    [标题翻译] reasoning text, attempt={attempt}: {translated[:50]}")
                        continue
                    if translated and len(translated) < 200:
                        a['title_en'] = a.get('title', '')  # 保存英文原文
                        a['title'] = translated
                        a['title_translated'] = True
                        return (a, translated)
                    else:
                        print(f"    [标题翻译] 结果被过滤: len={len(translated)}, title={title[:40]}")
                        return (a, None)
                except Exception as e:
                    print(f"    [标题翻译] 异常 attempt={attempt}: {e}")
                    if attempt < 2:
                        import time
                        time.sleep(3 * (attempt + 1))
                    else:
                        print(f"    [标题翻译] 3次全部失败: {title[:40]}")
            return (a, None)
        
        done = 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = {pool.submit(_translate_title_one, a): a for a in rss_eng_title}
            import concurrent.futures as _cf
            _completed = []
            try:
                for future in as_completed(futures, timeout=1800):
                    _completed.append(future)
            except _cf.TimeoutError:
                _completed = [f for f in futures if f.done()]
            for future in _completed:
                a, translated = future.result()
                done += 1
                if translated:
                    print(f"  [{done}/{len(rss_eng_title)}] {a.get('title', '')[:30]}... → {translated[:20]}")
                else:
                    print(f"  [{done}/{len(rss_eng_title)}] ✗")
        ok_count = sum(1 for a in rss_eng_title if a.get('title_translated'))
        print(f"RSS标题翻译完成: {ok_count}/{len(rss_eng_title)}")

    # 排序已在 remote_collect.py 的 collect_all() 中统一完成（时间+主题权重）
    # 此处不再重复排序

    # Lead post-processing: strip markdown headers from existing leads
    print("\n=== 导语后处理 ===")
    for a in articles:
        if a.get('lead'):
            a['lead'] = postprocess_lead(a['lead'])

    # 翻译 RSS 文章的英文导语为中文（保留双语）
    # 确保 RSS 文章有 lead_en（英文原文）
    for a in articles:
        if a.get('source') == 'rss' and a.get('lead') and not a.get('lead_en'):
            a['lead_en'] = a['lead']  # lead 本身就是英文
    rss_dek_to_translate = [a for a in articles if a.get('lead_en') and not a.get('lead_zh') and a.get('source') == 'rss']
    if rss_dek_to_translate:
        print(f"RSS导语翻译: {len(rss_dek_to_translate)} 篇...")
        def _translate_dek_one(a):
            dek_en = a.get('lead_en', '')
            for attempt in range(3):
                try:
                    result = llm_call(
                        f"将以下英文新闻导语翻译为简体中文。只输出翻译后的文本，不要解释，不要加引号。\n\n{dek_en}",
                        max_tokens=2000,
                        temperature=0.3,
                    )
                    translated = result.strip()
                    if translated and len(translated) < 500:
                        a['lead_zh'] = translated
                        a['lead'] = translated  # 主 lead 字段用中文
                        return (a, translated)
                except Exception as e:
                    if attempt < 2:
                        import time; time.sleep(3 * (attempt + 1))
            return (a, None)
        done = 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_translate_dek_one, a): a for a in rss_dek_to_translate}
            import concurrent.futures as _cf
            _completed = []
            try:
                for future in as_completed(futures, timeout=600):
                    _completed.append(future)
            except _cf.TimeoutError:
                _completed = [f for f in futures if f.done()]
            for future in _completed:
                a, translated = future.result()
                done += 1
                if translated:
                    print(f"  [{done}/{len(rss_dek_to_translate)}] {a.get('title', '')[:30]}... ✓")
                else:
                    print(f"  [{done}/{len(rss_dek_to_translate)}] ✗")
        ok_count = sum(1 for a in rss_dek_to_translate if a.get('lead_zh'))
        print(f"RSS导语翻译完成: {ok_count}/{len(rss_dek_to_translate)}")

    # LLM-based lead generation for articles with fulltext but no good lead
    # Also include RSS articles without fulltext — use ai_summary as input for lead generation
    print("\n=== LLM导语生成 ===")
    llm_lead_candidates = []
    for a in articles:
        ft = a.get('fulltext', '')
        lead = a.get('lead', '')
        title = a.get('title', '')
        # Need lead if: empty, too short, or just a copy of the title
        needs_lead = not lead or len(lead) < 10 or lead.strip() == title.strip()
        if needs_lead and ft and len(ft) > 100 and ft != title:
            # Has fulltext: generate lead from fulltext
            llm_lead_candidates.append(('fulltext', a))
        elif needs_lead and a.get('source') == 'rss':
            # RSS articles without fulltext: translate RSS description as lead
            # (don't use LLM to generate from ai_summary — that's two layers of hallucination)
            raw_summary = a.get('summary', '')
            if raw_summary and len(raw_summary) > 20:
                llm_lead_candidates.append(('rss_translate', a))
            elif a.get('ai_summary') and len(a.get('ai_summary', '')) > 50:
                # No RSS description: use ai_summary first sentence as lead (less hallucination than LLM rewrite)
                ai_sum = a.get('ai_summary', '')
                sentences = re.split(r'(?<=[。！？"」"])\s*', ai_sum)
                sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
                if sentences:
                    lead = sentences[0] if len(sentences[0]) > 20 else ' '.join(sentences[:2])
                    if len(lead) > 150:
                        lead = lead[:150].rstrip('，；：、') + '...'
                    a['lead'] = lead
                    continue
        elif needs_lead and a.get('source') == 'cn_home' and a.get('ai_summary') and len(a.get('ai_summary', '')) > 50:
            # cn_home articles with no fulltext (401): use ai_summary first sentence as lead
            ai_sum = a.get('ai_summary', '')
            sentences = re.split(r'(?<=[。！？"」"])\s*', ai_sum)
            sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
            if sentences:
                lead = sentences[0] if len(sentences[0]) > 20 else ' '.join(sentences[:2])
                if len(lead) > 150:
                    lead = lead[:150].rstrip('，；：、') + '...'
                a['lead'] = lead
                continue
        elif needs_lead and a.get('ai_summary') and len(a.get('ai_summary', '')) > 50:
            llm_lead_candidates.append(('summary', a))
    if llm_lead_candidates:
        print(f"  为 {len(llm_lead_candidates)} 篇文章生成LLM导语...")
        done = 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            def _gen_lead(item):
                source_type, a = item
                if source_type == 'fulltext':
                    return (a, generate_lead_from_fulltext(a.get('fulltext', ''), a.get('title', '')))
                elif source_type == 'rss_translate':
                    return (a, translate_rss_description_as_lead(a.get('summary', ''), a.get('title', '')))
                else:
                    return (a, generate_lead_from_summary(a.get('ai_summary', ''), a.get('title', '')))
            futures = {pool.submit(_gen_lead, item): item for item in llm_lead_candidates}
            import concurrent.futures as _cf
            _completed = []
            try:
                for future in as_completed(futures, timeout=1800):
                    _completed.append(future)
            except _cf.TimeoutError:
                _completed = [f for f in futures if f.done()]
            for future in _completed:
                a, lead = future.result()
                done += 1
                if lead:
                    a['lead'] = lead
                    print(f"  [{done}/{len(llm_lead_candidates)}] {a.get('title', '')[:30]}... ✓")
                else:
                    print(f"  [{done}/{len(llm_lead_candidates)}] {a.get('title', '')[:30]}... ✗")
        ok_count = sum(1 for _, a in llm_lead_candidates if a.get('lead'))
        print(f"  LLM导语完成: {ok_count}/{len(llm_lead_candidates)}")

    # Lead fallback: 从 ai_summary 提取首句作为导语（仅在LLM导语也失败时）
    for a in articles:
        if not a.get('lead') or len(a.get('lead', '')) < 10:
            if a.get('ai_summary'):
                summary_text = a['ai_summary']
                sentences = re.split(r'(?<=[。！？"」"])\s*', summary_text)
                sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
                if sentences:
                    a['lead'] = sentences[0] if len(sentences[0]) > 15 else ' '.join(sentences[:2])
                else:
                    a['lead'] = summary_text[:80]
            elif a.get('fulltext', ''):
                a['lead'] = a.get('fulltext', '')[:100]

    # Final lead post-processing pass
    for a in articles:
        if a.get('lead'):
            a['lead'] = postprocess_lead(a['lead'])

    # 导语-标题匹配验证
    print("\n=== 导语验证 ===")
    articles = verify_lead_title_match(articles)

    # 图片-标题匹配验证
    print("\n=== 图片验证 ===")
    articles = verify_image_title_match(articles)

    # 生成 HTML
    print("\n=== 生成HTML ===")
    html = generate_html(articles, today, gen_at)
    img_count = html.count('class="article-img"')
    print(f"HTML大小: {len(html)} bytes")
    print(f"配图数量: {img_count}")

    # 写入本地
    today_str = datetime.now(SH_TZ).strftime('%Y-%m-%d')
    local_docs = os.path.expanduser('~/wsj-briefing-docs')
    os.makedirs(local_docs, exist_ok=True)
    html_file = f'wsj-{today_str}.html'
    with open(f'{local_docs}/{html_file}', 'w', encoding='utf-8') as f:
        f.write(html)
    
    # 更新索引
    pages = sorted([
        p for p in os.listdir(local_docs) 
        if p.startswith('wsj-') and p.endswith('.html') and p != 'index.html'
    ], reverse=True)
    with open(f'{local_docs}/index.html', 'w', encoding='utf-8') as f:
        f.write(generate_index(articles, today))
    print(f"本地索引: {len(pages)}个页面")

    # 推到GitHub仓库
    print("\n=== 发布到GitHub Pages ===")
    wsj_repo = os.path.expanduser('~/.openclaw/workspace/wsj-briefing')
    docs_in_repo = f'{wsj_repo}/docs'
    os.makedirs(docs_in_repo, exist_ok=True)
    
    for p in [html_file, 'index.html']:
        subprocess.run(['cp', f'{local_docs}/{p}', f'{docs_in_repo}/{p}'], capture_output=True)

    r = subprocess.run(['git', '-C', wsj_repo, 'add', 'docs/'], capture_output=True, text=True)
    r = subprocess.run(['git', '-C', wsj_repo, 'diff', '--cached', '--quiet'], capture_output=True)
    if r.returncode != 0:
        subprocess.run(
            ['git', '-C', wsj_repo, 'commit', '-m', f'update briefing {today_str}'],
            capture_output=True, text=True
        )
        r2 = subprocess.run(['git', '-C', wsj_repo, 'push'], capture_output=True, text=True)
        if r2.returncode == 0:
            print("✓ 推送成功")
        else:
            print(f"✗ 推送失败: {r2.stderr[:200]}", file=sys.stderr)
    else:
        print("(无变化)")

    # Save dedup DB only after successful publish
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _dt, timedelta as _td
    _seen_file = _Path.home() / '.openclaw/workspace/wsj-briefing/seen_urls.json'
    _data = {}
    if _seen_file.exists():
        try:
            _data = _json.loads(_seen_file.read_text())
        except:
            _data = {}
    _now = _dt.now().timestamp()
    for _a in articles:
        _url = _a.get('url') or _a.get('link', '')
        if _url:
            _data[_url] = _now
    _cutoff = (_dt.now() - _td(days=7)).timestamp()
    _data = {k: v for k, v in _data.items() if v > _cutoff}
    _seen_file.write_text(_json.dumps(_data, ensure_ascii=False))
    print(f"  去重记录已保存: {len(_data)} 条")
    
    print(f"\n✓ 完成: https://zzmfreeman.github.io/wsj-briefing/{html_file}")
