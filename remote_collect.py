#!/usr/bin/env python3
"""
WSJ Briefing 远程采集模块（v3 - autocli read 版）
在Mac Mini上通过SSH执行
1. cn.wsj.com 首页 → autocli read 提取文章链接+标题
2. RSS 4板块 → urllib 抓取
3. 每篇文章全文 → autocli read（cn+英文均绕过paywall）
4. og:image 配图 → 轻量HTTP请求提取meta标签
"""
import json, re, time, urllib.request, urllib.error, os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

SH_TZ = timezone(timedelta(hours=8))


def refresh_wsj_cookies():
    """Extract wsj.com cookies from Chrome's SQLite database with AES decryption.
    Must be called while Chrome is running and user is logged in to wsj.com.
    Returns number of cookies saved."""
    import subprocess, sqlite3, shutil
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Util.Padding import unpad

    # Get Chrome Safe Storage key from keychain
    cmd = (
        'security unlock-keychain -p "1024" /Users/zzm/Library/Keychains/login.keychain-db && '
        'security -q find-generic-password -w -a Chrome -s "Chrome Safe Storage"'
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    key = r.stdout.strip()
    if not key:
        print("  WARNING: Could not get Chrome key for cookie decryption")
        return 0

    # Derive AES key
    derived_key = PBKDF2(key, b"saltysalt", dkLen=16, count=1003)

    # Copy cookies db + WAL + SHM (Chrome v151 uses WAL mode)
    src_dir = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    src = os.path.join(src_dir, "Cookies")
    if not os.path.exists(src):
        print("  WARNING: Chrome cookies db not found")
        return 0
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    for suffix in ["", "-wal", "-shm"]:
        s = os.path.join(src_dir, "Cookies" + suffix)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(tmp_dir, "Cookies" + suffix))
    tmp = os.path.join(tmp_dir, "Cookies")
    os.chmod(tmp, 0o644)

    conn = sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)
    c = conn.cursor()
    rows = c.execute(
        "SELECT host_key, name, value, encrypted_value, path, is_secure, is_httponly "
        "FROM cookies WHERE host_key LIKE '%wsj.com%' OR host_key LIKE '%dowjones%'"
    ).fetchall()
    conn.close()
    shutil.rmtree(tmp_dir)

    cookies = []
    ok = 0
    for host, name, value, enc_value, path, is_secure, is_httponly in rows:
        cv = value
        if not cv and enc_value:
            enc_data = enc_value[3:] if enc_value[:3] == b"v10" else enc_value
            try:
                cipher = AES.new(derived_key, AES.MODE_CBC, IV=b" " * 16)
                decrypted = unpad(cipher.decrypt(enc_data), AES.block_size)
                # Skip first 32 bytes (SHA256 integrity hash, Chrome v10+ on macOS)
                if len(decrypted) > 32:
                    cv = decrypted[32:].decode("utf-8")
                else:
                    cv = decrypted.decode("utf-8")
                ok += 1
            except:
                cv = ""
        cookies.append({
            "name": name, "value": cv, "domain": host, "path": path,
            "secure": bool(is_secure), "httponly": bool(is_httponly)
        })

    out_path = os.path.expanduser("~/.wsj_cookies.json")
    with open(out_path, "w") as f:
        json.dump(cookies, f)
    print(f"  WSJ cookies: {ok}/{len(rows)} decrypted and saved")
    return ok
IMAGE_CACHE_FILE = Path.home() / ".openclaw/workspace/wsj-briefing/image_cache.json"
SEEN_URLS_FILE = Path.home() / ".openclaw/workspace/wsj-briefing/seen_urls.json"
# 过去 N 天已发过的文章不再重复收录
DEDUP_DAYS = 7
MAX_ARTICLE_AGE_DAYS = 3  # 只保留3天内发布的文章
AUTOCLI = "/usr/local/bin/autocli"

# Google News RSS for WSJ articles (WSJ's own RSS feeds are all dead: 401/403/404)
# Google News links redirect to wsj.com — we use them directly as article URLs
RSS_FEEDS = [
    ("💻 Tech",    "https://feeds.content.dowjones.io/public/rss/RSSWSJD"),
    ("📈 Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("🌍 World",   "https://feeds.content.dowjones.io/public/rss/RSSWorldNews"),
    ("🏢 Business", "https://feeds.content.dowjones.io/public/rss/RSSOpinion"),
]
ARTICLES_PER_SECTION = 10
CN_ARTICLES_LIMIT = 15


# ── autocli read 封装 ─────────────────────────────────
import subprocess

def autocli_read(url, timeout=30):
    """调用 autocli read，返回 markdown 内容或 None"""
    try:
        r = subprocess.run(
            [AUTOCLI, "read", url, "-f", "md"],
            capture_output=True, text=True, timeout=timeout
        )
        if r.returncode == 0 and len(r.stdout) > 100:
            return r.stdout
    except subprocess.TimeoutExpired:
        print(f"    [autocli超时] {url[:60]}")
    except Exception as e:
        print(f"    [autocli异常] {e}")
    return None

def pw_fetch_homepage(url, timeout=30):
    """用 Playwright 抓取页面，返回 (text, html)
    Stealth mode: realistic UA, viewport, locale, extra headers, script injection
    to bypass DataDome CAPTCHA on cn.wsj.com"""
    import asyncio
    from playwright.async_api import async_playwright
    
    async def _fetch():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                }
            )
            # Inject stealth script to hide automation signals
            await context.add_init_script("""
                // Hide webdriver flag
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                // Mock plugins
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                // Mock languages
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
                // Override Chrome runtime
                window.chrome = { runtime: {} };
                // Mock permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
            """)
            page = await context.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
                if not resp or resp.status != 200:
                    print(f"    [pw状态] {resp.status if resp else 'None'} for {url[:50]}")
                    await browser.close()
                    return None, None
                await page.wait_for_timeout(3000)
                text = await page.inner_text("body")
                html = await page.content()
                return text, html
            except Exception as e:
                print(f"    [pw错误] {e}")
                return None, None
            finally:
                await browser.close()
    
    return asyncio.run(_fetch())


def pw_fetch_og_image(url, timeout=15):
    """用 Playwright 提取 og:image（单篇，HTTP 请求被 wsj.com 401 挡掉）"""
    import asyncio
    from playwright.async_api import async_playwright
    
    async def _fetch():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )
            page = await context.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
                if not resp:
                    await browser.close()
                    return ""
                await page.wait_for_timeout(2000)
                html = await page.content()
                import re
                # og:image
                m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
                if not m:
                    m = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
                if not m:
                    m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
                if m:
                    img = m.group(1)
                    if "/social" in img:
                        img = img.replace("/social", "/large")
                    return img
                return ""
            except Exception as e:
                return ""
            finally:
                await browser.close()
    
    return asyncio.run(_fetch())


def pw_batch_og_images(urls, timeout=15):
    """批量用 Playwright 提取 og:image（一个 browser session 处理所有 URL）
    返回 {url: image_url} 字典
    策略：先试原始URL（带cookies）→ 401则试cn.wsj.com对应slug → 最后试文章URL的hash查wsj图片CDN"""
    import asyncio, json as _json, os as _os
    from playwright.async_api import async_playwright
    import re as _re

    # Load wsj.com cookies from Chrome (if available)
    wsj_cookies = []
    cookie_file = _os.path.expanduser("~/.wsj_cookies.json")
    if _os.path.exists(cookie_file):
        try:
            with open(cookie_file) as f:
                raw_cookies = _json.load(f)
            for c in raw_cookies:
                if c.get("value"):
                    wsj_cookies.append({
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".wsj.com"),
                        "path": c.get("path", "/"),
                        "secure": c.get("secure", False),
                        "httpOnly": c.get("httponly", False),
                    })
        except:
            pass

    async def _batch():
        results = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )
            # Inject wsj.com cookies
            if wsj_cookies:
                await context.add_cookies(wsj_cookies)
                print(f"  Loaded {len(wsj_cookies)} wsj cookies for og:image extraction")
            page = await context.new_page()
            for url in urls:
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
                    if not resp:
                        results[url] = ""
                        continue
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    
                    # If 401, try cn.wsj.com equivalent
                    if resp.status == 401 and "wsj.com" in url:
                        # Extract slug from wsj.com URL
                        slug = ""
                        if "/articles/" in url:
                            slug = url.split("/articles/")[-1]
                        elif "/politics/" in url or "/economy/" in url or "/world/" in url or "/business/" in url or "/tech/" in url or "/markets/" in url:
                            parts = url.split("/")
                            slug = parts[-1] if parts else ""
                        
                        if slug:
                            cn_url = f"https://cn.wsj.com/articles/{slug}"
                            try:
                                resp2 = await page.goto(cn_url, wait_until="domcontentloaded", timeout=timeout*1000)
                                if resp2 and resp2.status == 200:
                                    await page.wait_for_timeout(2000)
                                    html = await page.content()
                            except:
                                pass
                    
                    # Extract og:image
                    m = _re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, _re.I)
                    if not m:
                        m = _re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, _re.I)
                    if not m:
                        m = _re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, _re.I)
                    if m:
                        img = m.group(1)
                        if "/social" in img:
                            img = img.replace("/social", "/large")
                        results[url] = img
                    else:
                        # Last resort: look for any wsj image CDN URLs in HTML
                        wsj_imgs = _re.findall(r'https://images\.wsj\.net/im-\d+[^"\'<>\s]*', html)
                        if wsj_imgs:
                            results[url] = wsj_imgs[0]
                        else:
                            results[url] = ""
                except Exception:
                    results[url] = ""
            await browser.close()
        return results

    return asyncio.run(_batch())


def extract_lead(cleaned_text):
    """从清洗后的文本提取导语（第一段有意义的段落）和摘要正文。
    跳过时间戳、作者、版权信息等元数据。
    """
    if not cleaned_text:
        return "", ""
    
    # 清洗时间戳前缀
    # autocli 格式: "Updated Jan. 28, 2025 12:44 pm ETDeepSeek has..."
    # 或 " 2026年7月17日 16:52 CST中国领导人..."
    tz_list = r'(?:ET|EST|EDT|CST|CDT|PST|PDT|MST|MDT|GMT|UTC|BST|CET|IST|HKT|JST|KST|SGT|AEST|ACST)'
    cleaned = re.sub(r'^\s*Updated \w+\.? \d{1,2},? \d{4} \d{1,2}:\d{2} [ap]m ' + tz_list, '', cleaned_text)
    cleaned = re.sub(r'^\s*\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}\s+' + tz_list, '', cleaned)
    cleaned = re.sub(r'^\s*\w+\.? \d{1,2},? \d{4} \d{1,2}:\d{2} [ap]m ' + tz_list, '', cleaned)
    cleaned = re.sub(r'^\s*\d{1,2}:\d{2} [ap]m ' + tz_list, '', cleaned)
    
    # 清洗 cn.wsj.com 页面导航文字
    cleaned = re.sub(r'中文 英文\(附音频\) 中英对照 分享给朋友免费阅读', '', cleaned)
    cleaned = re.sub(r'中文 英文.*?免费阅读', '', cleaned)
    # 清洗 wsj.com 页面导航文字
    cleaned = re.sub(r'Gift unlocked article Listen \(\d+ min\)', '', cleaned)
    cleaned = re.sub(r'Listen \(\d+ min\)', '', cleaned)
    cleaned = re.sub(r'Gift unlocked article', '', cleaned)
    
    # 按双换行或段落分割
    paragraphs = re.split(r'\n\n+', cleaned)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    
    lead = ""
    body = ""
    found_lead = False
    
    # 跳过面包屑导航（包含 ?mod=breadcrumb 或数字列表链接）
    for i, p in enumerate(paragraphs):
        if len(p) < 30:
            continue
        
        # 跳过面包屑
        if '?mod=breadcrumb' in p or 'mod=breadcrumb' in p:
            continue
        if re.match(r'^\d+\.\s+\[', p):  # "1.  [链接]"
            continue
        # 跳过作者行（By xxx Aug. 12, 2026）— 放宽长度限制
        if re.match(r'^By\s+\w+', p) and len(p) < 200:
            continue
        # 跳过纯导航行
        if re.search(r'中文.*英文.*免费阅读', p) or 'Gift unlocked' in p or 'Listen (' in p:
            continue
        # 跳过广告行
        if 'Advertisement' in p and len(p) < 100:
            continue
        # 跳过 cn.wsj.com 导航行
        if '分享给朋友' in p and len(p) < 100:
            continue
        
        if not found_lead:
            lead = p[:500]
            # 文本级清洗：删除残留的导航文字片段
            lead = re.sub(r'中文 英文\(附音频\) 中英对照 分享给朋友免费阅读', '', lead)
            lead = re.sub(r'中文 英文.*?免费阅读', '', lead)
            lead = re.sub(r'分享给朋友免费阅读', '', lead)
            lead = re.sub(r'Gift unlocked article Listen \(\d+ min\)', '', lead)
            lead = re.sub(r'Listen \(\d+ \w+\)', '', lead)
            lead = re.sub(r'Gift unlocked article', '', lead)
            lead = re.sub(r'Advertisement', '', lead)
            # 删除作者行前缀（By xxx Aug. 12, 2026 ... ET/ CST ...）
            lead = re.sub(r'^By\s+[\w\s,\.]+(?:Aug\.|Jan\.|Feb\.|Mar\.|Apr\.|May|Jun\.|Jul\.|Aug\.|Sep\.|Oct\.|Nov\.|Dec\.)\s+\d{1,2},?\s*\d{4}\s+\d{1,2}:\d{2}\s*[ap]m\s*(?:ET|CST|EST|PST|PDT|GMT|BST|CET|HKT|JST|KST|SGT|AEST|ACST)?\s*\d*\s*', '', lead)
            lead = re.sub(r'^[\w\s/]+(?:\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}\s*(?:CST|ET|EST|EDT|PST|PDT|GMT|UTC|BST|CET|HKT|JST|KST|SGT|AEST|ACST))\s*', '', lead)
            # 删除图片来源
            lead = re.sub(r'图片来源：[^\s]+', '', lead)
            # 删除 "要点速览"
            lead = re.sub(r'要点速览\s*', '', lead)
            lead = lead.strip()
            if not lead or len(lead) < 30:
                continue
            found_lead = True
            if i + 1 < len(paragraphs):
                body_parts = paragraphs[i+1:]
                body = "\n\n".join(body_parts)[:3000]
            break
    
    if not lead and paragraphs:
        lead = paragraphs[0][:500]
    
    return lead, body


def clean_summary(text, max_len=500):
    """清洗autocli返回的正文，生成干净的摘要"""
    if not text:
        return ""
    # 1. 去掉markdown图片：![alt](url)
    text = re.sub(r'!\[[^\]]*\]\([^\)]+\)', '', text)
    # 2. 去掉markdown链接：[text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # 3. 去掉日期前缀（如"2026年7月24日 10:10 CST"）
    text = re.sub(r'^\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}\s+(CST|GMT|EST|PST|UTC)\s*', '', text)
    # 4. 逐行清洗
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        # 跳过空行和极短行
        if len(stripped) < 5:
            continue
        # 跳过面包屑导航（数字+链接 或 数字+纯文本分类名）
        if re.match(r'^\d+\.\s+\[', stripped) or re.match(r'^\d+\.\s+[^\.\!\"\'\d].{0,10}$', stripped):
            continue
        # 跳过##标题行
        if stripped.startswith('## ') or stripped.startswith('# '):
            continue
        # 跳过独立的日期行（纯日期+时间）
        if re.match(r'^\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}\s*(CST|GMT|EST|PST|UTC)?\s*$', stripped):
            continue
        # 跳过作者署名行（纯名字，无动词，很短）
        if len(stripped) < 20 and not any(c in stripped for c in ['。', '，', '.', ',', '！', '？']):
            continue
        # 跳过版权行
        if 'Copyright ©' in stripped or 'Dow Jones' in stripped:
            continue
        cleaned_lines.append(stripped)
    # 5. 合并为一段
    text = ' '.join(cleaned_lines)
    # 6. 压缩多余空格
    text = re.sub(r'\s+', ' ', text).strip()
    # 7. 在句子边界截断（不要断在词中间）
    if len(text) > max_len:
        truncated = text[:max_len]
        for sep in ['。', '，', '.', ',', '；', ';']:
            idx = truncated.rfind(sep)
            if idx > max_len // 2:
                truncated = truncated[:idx+1]
                break
        text = truncated
    return text


# ── cn.wsj.com 首页抓取 ──────────────────────────────
def normalize_url(url):
    """去除mod参数和尾部标点"""
    url = url.split("?")[0].rstrip(".,;])")
    return url


def scrape_cn_homepage(limit=30):
    """用 Playwright 抓 cn.wsj.com 首页，提取文章链接+标题+描述+配图
    解析策略：使用 Playwright DOM API 提取卡片结构，每张卡片 1 文 1 图"""
    print("  抓取 cn.wsj.com 首页 (Playwright)...")
    
    async def _scrape():
        import asyncio
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            page = await context.new_page()
            try:
                resp = await page.goto("https://cn.wsj.com/", wait_until="domcontentloaded", timeout=30000)
                if not resp or resp.status != 200:
                    await browser.close()
                    print("  cn.wsj.com 首页失败")
                    return []
                await page.wait_for_timeout(3000)
                
                # Use DOM API: find <a> tags that contain <img> from images.wsj.net
                JS_SCRAPE = r"""
                () => {
                    const nav_words = new Set([
                        "SKIP TO MAIN CONTENT", "The Wall Street Journal", "订阅", "登录", "华尔街日报",
                        "中文 (Chinese)", "简体版", "更多", "首页", "国际", "中国", "金融市场", "经济",
                        "商业", "科技", "生活与理财", "专栏与观点", "视频", "专题报道", "广告", "独家报道"
                    ]);
                    
                    const articles = [];
                    const seen_urls = new Set();
                    
                    const all_links = document.querySelectorAll('a');
                    for (const link of all_links) {
                        const href = link.href || '';
                        if (href.indexOf('/articles/') === -1) continue;
                        
                        const img = link.querySelector('img');
                        if (!img) continue;
                        
                        const url = href.split('?')[0].replace(/[.,;\)\]]+$/, '');
                        if (seen_urls.has(url)) continue;
                        seen_urls.add(url);
                        
                        const img_src = img.src || '';
                        if (img_src.indexOf('images.wsj.net') === -1) continue;
                        
                        let title = (img.alt || '').replace(/^Image thumbnail for article titled\s*/i, '');
                        if (!title || title.length < 4) {
                            const parent = link.parentElement;
                            if (parent) {
                                const spans = parent.querySelectorAll('span, h1, h2, h3, p');
                                for (const s of spans) {
                                    const t = s.textContent.trim();
                                    if (t.length >= 4 && t.length <= 100 && !nav_words.has(t)) {
                                        title = t;
                                        break;
                                    }
                                }
                            }
                        }
                        
                        if (title && title.length >= 4 && title.length <= 100) {
                            articles.push({
                                url: url,
                                title: title,
                                image: img_src.replace(/&amp;/g, '&'),
                                summary: ''
                            });
                        }
                    }
                    
                    return articles;
                }
                """
                articles_data = await page.evaluate(JS_SCRAPE)
                
                await browser.close()
                return articles_data[:limit]
            except Exception as e:
                print(f"  [pw错误] {e}")
                await browser.close()
                return []
    
    import asyncio
    articles = asyncio.run(_scrape())
    has_img = sum(1 for a in articles if a.get("image"))
    unique_img = len(set(a.get("image", "") for a in articles if a.get("image")))
    print(f"  cn.wsj.com: {len(articles)} 篇（{has_img} 篇有图, {unique_img} 张独立图片）")
    return articles


# ── Cookie 加载 ──────────────────────────────────────
def _load_wsj_cookies():
    """Load wsj.com cookies from ~/.wsj_cookies.json for Playwright injection."""
    try:
        cookie_file = os.path.expanduser("~/.wsj_cookies.json")
        with open(cookie_file) as f:
            raw = json.load(f)
        cookies = []
        for c in raw:
            if c.get("value"):
                cookies.append({
                    "name": c["name"], "value": c["value"],
                    "domain": c.get("domain", ".wsj.com"),
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", False),
                    "httpOnly": c.get("httponly", False),
                })
        return cookies
    except Exception:
        return []


# ── Playwright + cookies 批量正文抓取 ──────────────────
def _pw_fetch_fulltext_batch(url_pairs, cookies=None, timeout=30):
    """用 Playwright + cookies 并发抓取文章正文。
    url_pairs: [(art, url), ...] — 只用 url
    返回 [(text, html, og_image), ...]
    """
    import asyncio
    from playwright.async_api import async_playwright
    
    async def _batch():
        results = [("", "", "", "", "")] * len(url_pairs)
        
        async def _fetch_one(idx, url, context):
            try:
                page = await context.new_page()
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
                await page.wait_for_timeout(2000)
                status = resp.status if resp else 0
                html = await page.content()
                
                # 提取正文
                text = ""
                article_match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)
                if article_match:
                    article_text = re.sub(r'<[^>]+>', ' ', article_match.group(1))
                    article_text = re.sub(r'\s+', ' ', article_text).strip()
                    if len(article_text) > 200 and "dd=" not in article_text[:100]:
                        text = article_text
                
                if not text:
                    paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
                    p_text = " ".join(re.sub(r'<[^>]+>', '', p) for p in paragraphs)
                    p_text = re.sub(r'\s+', ' ', p_text).strip()
                    if len(p_text) > 200 and "dd=" not in p_text[:100]:
                        text = p_text
                
                # 提取 og:image
                og_m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
                if not og_m:
                    og_m = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
                og_image = og_m.group(1) if og_m else ""
                
                # 提取 dek（文章自带的副标题/导语）
                dek = ""
                # wsj.com: <h2 data-testid="dek-block">...</h2>
                dek_m = re.search(r'<h2[^>]*data-testid="dek-block"[^>]*>(.*?)</h2>', html, re.DOTALL)
                if not dek_m:
                    # cn.wsj.com: 可能用 <p class="dek"> 或 meta description
                    dek_m = re.search(r'<p[^>]*class="[^"]*dek[^"]*"[^>]*>(.*?)</p>', html, re.DOTALL)
                if not dek_m:
                    # fallback: og:description meta tag
                    dek_m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html, re.I)
                if dek_m:
                    dek = re.sub(r'<[^>]+>', '', dek_m.group(1)).strip()
                
                # 提取发布时间 (article:published_time 或 datePublished)
                pub_time = ""
                pt_m = re.search(r'<meta[^>]+property="article:published_time"[^>]+content="([^"]+)"', html, re.I)
                if not pt_m:
                    pt_m = re.search(r'<meta[^>]+name="article:published_time"[^>]+content="([^"]+)"', html, re.I)
                if not pt_m:
                    pt_m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
                if not pt_m:
                    pt_m = re.search(r'<meta[^>]+name="publish.date"[^>]+content="([^"]+)"', html, re.I)
                if not pt_m:
                    # cn.wsj.com: 从正文中提取日期 (如 "2026年7月24日 10:10 CST")
                    pt_m = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}\s+(?:CST|GMT|EST|PST|UTC))', text[:500])
                if not pt_m:
                    # wsj.com: "Updated Aug. 21, 2026 10:00 am ET"
                    pt_m = re.search(r'(?:Updated\s+)?(\w+\.?\s+\d{1,2},?\s+\d{4}\s+\d{1,2}:\d{2}\s*[ap]m\s+(?:ET|CST|EST|PST|PDT|GMT|UTC))', text[:500])
                if pt_m:
                    pub_time = pt_m.group(1).strip()
                
                await page.close()
                results[idx] = (text, html, og_image, dek, pub_time)
                print(f"    [{status}] {url[-50:]}: {len(text)} chars og={'Y' if og_image else 'N'} dek={'Y' if dek else 'N'}")
            except Exception as e:
                print(f"    ERROR {url[-50:]}: {e}")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            if cookies:
                await context.add_cookies(cookies)
            
            sem = asyncio.Semaphore(5)
            async def _limited(idx, url):
                async with sem:
                    await _fetch_one(idx, url, context)
            
            await asyncio.gather(*[_limited(i, url) for i, (_, url) in enumerate(url_pairs)])
            await browser.close()
        
        return results
    
    return asyncio.run(_batch())


# ── autocli read 全文抓取 ────────────────────────────
def fetch_fulltext_batch(articles, timeout=30):
    """批量抓全文
    策略：
    - 先用 autocli（Chrome 扩展有登录态）
    - autocli 失败时，用 Playwright + cookies 抓取 wsj.com/cn.wsj.com 正文
    """
    print(f"  全文抓取 {len(articles)} 篇...")
    
    # 加载 wsj.com cookies
    wsj_cookies = _load_wsj_cookies()
    
    # 分两阶段：先 autocli 批量，失败的收集起来用 Playwright 批量
    pw_needed = []
    ok = 0
    
    for art in articles:
        url = art.get("url") or art.get("link", "")
        text = None
        html = None
        
        # autocli 太慢且经常超时，全部用 Playwright + cookies
        md = None
        if md:
            lines = md.split("\n")
            body_lines = []
            in_body = False
            for line in lines:
                if line.startswith("---") and not in_body:
                    in_body = True
                    continue
                if in_body:
                    body_lines.append(line)
            body = "\n".join(body_lines).strip()
            if len(body) > 200:
                text = body
                try:
                    import json
                    data = json.loads(md)
                    if data.get("textContent"):
                        text = data["textContent"]
                        if data.get("title"):
                            art["title"] = data["title"]
                except:
                    pass
        
        if text and len(text) > 200:
            # autocli 成功，直接处理
            art["fulltext"] = text[:4000]
            lead, remaining = extract_lead(text)
            art["lead"] = lead
            cleaned_body = clean_summary(remaining or text, max_len=1000)
            art["summary"] = cleaned_body
            ok += 1
        else:
            # autocli 失败，加入 Playwright 队列
            pw_needed.append(art)
    
    # Playwright + cookies 批量抓取
    if pw_needed:
        print(f"  autocli 成功 {ok}/{len(articles)}，Playwright+cookies 抓取 {len(pw_needed)} 篇...")
        pw_results = _pw_fetch_fulltext_batch(
            [(art, art.get("url") or art.get("link", "")) for art in pw_needed],
            cookies=wsj_cookies, timeout=timeout
        )
        for art, (text, html, og_image, dek, pub_time) in zip(pw_needed, pw_results):
            if text and len(text) > 200:
                art["fulltext"] = text[:4000]
                # 优先使用文章自带的 dek 作为导语
                if dek and len(dek) > 10:
                    art["lead"] = dek[:500]
                    art["lead_en"] = dek[:500]  # 保存英文原文供双语显示
                    _, remaining = extract_lead(text)
                else:
                    lead, remaining = extract_lead(text)
                    art["lead"] = lead
                    art["lead_en"] = lead  # fallback 正文首段也保存
                cleaned_body = clean_summary(remaining or text, max_len=1000)
                art["summary"] = cleaned_body
                ok += 1
            else:
                # Playwright 也失败，fallback 到 summary
                home_summary = art.get("summary", "")
                if home_summary and "dd=" not in home_summary and "captcha" not in home_summary.lower() and len(home_summary) > 50:
                    art["fulltext"] = home_summary
                    art["lead"] = home_summary[:200]
                    art["lead_en"] = home_summary[:200]  # 保存英文原文
                else:
                    title = art.get("title", "")
                    art["fulltext"] = title
                    art["lead"] = title
                ok += 1
            # 用 og:image 补图
            if og_image and not art.get("image"):
                art["image"] = og_image
            # 用提取的发布时间补全（cn.wsj.com 首页文章无 published 字段）
            if pub_time and not art.get("published"):
                art["published"] = pub_time
    
    print(f"  全文完成: {ok}/{len(articles)}")
    return articles


# ── og:image HTTP 提取 ────────────────────────────────
def fetch_og_image(url):
    """用HTTP请求提取og:image，不依赖Playwright"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept-Language': 'en,zh-CN;q=0.9',
        'Accept': 'text/html,application/xhtml+xml',
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            # 只读前8KB就够了，og:image通常在head里
            html = r.read(8192).decode('utf-8', errors='ignore')
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if not m:
            m = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m:
            img = m.group(1)
            if '/social' in img:
                img = img.replace('/social', '/large')
            return img
    except:
        pass
    return ''


# ── RSS 抓取 ─────────────────────────────────────────

def resolve_google_news_urls_batch(gn_urls, timeout=15):
    """Batch resolve Google News article URLs to wsj.com URLs using Playwright.
    One browser session for all URLs. Returns {gn_url: wsj_url} dict."""
    if not gn_urls:
        return {}
    import asyncio
    from playwright.async_api import async_playwright

    async def _batch():
        results = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            page = await context.new_page()
            for gn_url in gn_urls:
                try:
                    # Use networkidle first — Google News does client-side JS redirect
                    resp = await page.goto(gn_url, wait_until="domcontentloaded", timeout=timeout*1000)
                    # Wait for JS redirect — check URL every 2 seconds for up to 20s
                    final_url = gn_url
                    for _ in range(10):
                        await page.wait_for_timeout(2000)
                        current = page.url
                        if "wsj.com" in current:
                            final_url = current.split("?")[0]
                            break
                        # Also check if page has a wsj.com link (Google News interstitial)
                        if "news.google.com" in current:
                            try:
                                link = await page.evaluate("""() => {
                                    const a = document.querySelector('a[href*="wsj.com"]');
                                    return a ? a.href : '';
                                }""")
                                if link and "wsj.com" in link:
                                    final_url = link.split("?")[0]
                                    break
                            except:
                                pass
                    results[gn_url] = final_url
                except Exception:
                    results[gn_url] = gn_url
            await browser.close()
        return results
    return asyncio.run(_batch())


def resolve_google_news_url(gn_url, timeout=5):
    """Resolve a single Google News URL (wrapper for batch function)."""
    return resolve_google_news_urls_batch([gn_url], timeout=timeout).get(gn_url, gn_url)


def fetch_rss(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; RSS/2.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
        root = ET.fromstring(raw)
        items = []
        for item in root.findall(".//item"):
            def t(tag):
                el = item.find(tag)
                return el.text.strip() if el is not None and el.text else ""
            # Clean title: Google News appends " - WSJ" etc.
            title = t("title")
            for suffix in [" - WSJ", " - Wall Street Journal", " - 华尔街日报"]:
                if title.endswith(suffix):
                    title = title[:-len(suffix)].strip()
            desc = re.sub(r"<[^>]+>", "", t("description"))
            link = t("link")
            if "?mod=" in link:
                link = link.split("?mod=")[0]
            media = item.find("{http://search.yahoo.com/mrss/}content")
            img_url = media.get("url") if media is not None else ""
            # Filter MarketWatch images
            if img_url and ("marketwatch.com" in img_url or "mktw.net" in img_url):
                img_url = ""
            # Google News links redirect to wsj.com when clicked — use directly
            items.append({
                "title": title, "link": link,
                "summary": desc[:300], "published": t("pubDate"),
                "image": img_url,
            })
        # Filter MarketWatch articles
        items = [i for i in items if "marketwatch.com" not in i["link"]]
        return items
    except Exception as e:
        print(f"  [RSS失败] {url}: {e}")
        return []


def collect_rss():
    result = []
    for section, rss_url in RSS_FEEDS:
        print(f"  RSS {section}...")
        items = fetch_rss(rss_url)
        seen = set()
        sec_arts = []
        for item in items:
            link = item["link"]
            if not link or link in seen:
                continue
            # Final MarketWatch filter (triple insurance)
            if "marketwatch.com" in link:
                continue
            seen.add(link)
            sec_arts.append({**item, "section": section, "source": "rss", "url": link})
        taken = sec_arts[:ARTICLES_PER_SECTION]
        result.extend(taken)
        print(f"    → {len(items)} 篇，取 {len(taken)} 篇")
    return result


# ── 配图缓存 ──────────────────────────────────────────
def load_image_cache():
    if IMAGE_CACHE_FILE.exists():
        try:
            return json.loads(IMAGE_CACHE_FILE.read_text())
        except:
            return {}
    return {}

def save_image_cache(cache):
    try:
        IMAGE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))
    except:
        pass


# ── 批量配图 ─────────────────────────────────────────
def enrich_images(articles):
    """对所有没配图的文章提取og:image
    策略：先解析Google News URL → 查缓存 → HTTP og:image → Playwright 批量 og:image"""
    cache = load_image_cache()
    count = 0
    
    # Step 1: Resolve Google News URLs to wsj.com URLs
    gn_articles = [a for a in articles if not a.get("image") and "news.google.com" in (a.get("url") or a.get("link", ""))]
    if gn_articles:
        gn_urls = [a.get("url") or a.get("link", "") for a in gn_articles]
        print(f"  解析Google News URL: {len(gn_urls)} 篇...")
        resolved = resolve_google_news_urls_batch(gn_urls, timeout=10)
        for art in gn_articles:
            gn_url = art.get("url") or art.get("link", "")
            wsj_url = resolved.get(gn_url)
            if wsj_url and wsj_url != gn_url:
                art["resolved_url"] = wsj_url
                print(f"    {gn_url[:50]}... → {wsj_url[:50]}...")
    
    to_fetch = [a for a in articles if not a.get("image")]
    if not to_fetch:
        save_image_cache(cache)
        has_img = sum(1 for a in articles if a.get("image"))
        print(f"  配图完成: {has_img}/{len(articles)} 篇有图")
        return
    
    print(f"  配图提取 {len(to_fetch)} 篇...")
    
    # 第一轮：缓存 + HTTP og:image
    pw_needed = []
    for art in to_fetch:
        # Prefer resolved wsj.com URL for og:image extraction
        url = art.get("resolved_url") or art.get("url") or art.get("link", "")
        if not url:
            continue
        if url in cache:
            art["image"] = cache[url]
            if art["image"]:
                count += 1
            continue
        # HTTP og:image（快，但 wsj.com 会 401）
        img = fetch_og_image(url)
        if img:
            art["image"] = img
            cache[url] = img
            count += 1
        else:
            pw_needed.append((art, url))
    
    # 第二轮：Playwright 批量提取 wsj.com 文章的 og:image
    if pw_needed:
        pw_urls = [url for _, url in pw_needed]
        print(f"  Playwright 批量提取 {len(pw_urls)} 篇 og:image...")
        results = pw_batch_og_images(pw_urls, timeout=12)
        for art, url in pw_needed:
            img = results.get(url, "")
            art["image"] = img
            cache[url] = img
            if img:
                count += 1

    save_image_cache(cache)
    has_img = sum(1 for a in articles if a.get("image"))
    print(f"  配图完成: {has_img}/{len(articles)} 篇有图")


# ── 跨日去重 ──────────────────────────────────────────
def load_seen_urls():
    """加载过去 DEDUP_DAYS 天内已发过的文章 URL"""
    if SEEN_URLS_FILE.exists():
        try:
            data = json.loads(SEEN_URLS_FILE.read_text())
            cutoff = (datetime.now() - timedelta(days=DEDUP_DAYS)).timestamp()
            # 只保留 DEDUP_DAYS 天内的记录
            fresh = {k: v for k, v in data.items() if v > cutoff}
            return fresh
        except:
            return {}
    return {}

def save_seen_urls(articles):
    """保存本次采集的文章 URL 到去重文件"""
    data = load_seen_urls()
    now = datetime.now().timestamp()
    for a in articles:
        url = a.get("url") or a.get("link", "")
        if url:
            data[url] = now
    # 清理过期记录
    cutoff = (datetime.now() - timedelta(days=DEDUP_DAYS)).timestamp()
    data = {k: v for k, v in data.items() if v > cutoff}
    SEEN_URLS_FILE.write_text(json.dumps(data, ensure_ascii=False))
    print(f"  去重库: {len(data)} 条记录")


# ── wsj.com 登录 cookie 提取 ─────────────────────────
def get_chrome_cookies_for_wsj():
    """从 Chrome cookie 库提取 wsj.com 的登录 cookie
    需要安装 browser-cookie3: pip3 install browser-cookie3
    首次使用需要 security unlock-keychain 授权"""
    try:
        # 先解锁 keychain，让 browser-cookie3 能解密 cookie
        import subprocess
        subprocess.run(
            ["security", "unlock-keychain", "-p", "", 
             os.path.expanduser("~/Library/Keychains/login.keychain-db")],
            capture_output=True, timeout=5
        )
        
        import browser_cookie3
        cj = browser_cookie3.chrome(domain_name="wsj.com")
        cookies = []
        for c in cj:
            cookies.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            })
        # 也提取 cn.wsj.com 的 cookie
        try:
            cj_cn = browser_cookie3.chrome(domain_name="cn.wsj.com")
            for c in cj_cn:
                cookies.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path or "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                })
        except:
            pass
        return cookies
    except Exception as e:
        print(f"  [cookie提取失败] {e}")
        return []


def pw_fetch_article_with_cookies(url, timeout=15):
    """用 Playwright + Chrome 登录 cookie 抓取文章页，返回 (text, html, og_image)
    适用于 wsj.com 和 cn.wsj.com 的付费墙文章"""
    import asyncio
    from playwright.async_api import async_playwright
    
    async def _fetch():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN" if "cn.wsj.com" in url else "en-US",
            )
            # 注入 Chrome 登录 cookie
            cookies = get_chrome_cookies_for_wsj()
            if cookies:
                try:
                    await context.add_cookies(cookies)
                except Exception as e:
                    print(f"    [cookie注入失败] {e}")
            
            page = await context.new_page()
            try:
                resp = await page.goto(url, wait_until="networkidle", timeout=timeout*1000)
                if not resp or resp.status != 200:
                    await browser.close()
                    return None, None, ""
                await page.wait_for_timeout(3000)
                text = await page.inner_text("body")
                html = await page.content()
                
                # 提取 og:image
                og_image = ""
                m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
                if m:
                    og_image = m.group(1)
                    if "/social" in og_image:
                        og_image = og_image.replace("/social", "/large")
                
                return text, html, og_image
            except Exception as e:
                print(f"    [pw_cookie错误] {e}")
                return None, None, ""
            finally:
                await browser.close()
    
    return asyncio.run(_fetch())


# ── 主采集函数 ───────────────────────────────────────
def collect_all():
    print(f"[{datetime.now(SH_TZ).strftime('%H:%M:%S')}] WSJ 采集启动")
    t0 = time.time()

    # 加载去重库
    seen_urls = load_seen_urls()
    dedup_count = len(seen_urls)
    print(f"  去重库: {dedup_count} 条历史记录")

    # 1. cn.wsj.com 首页 (stealth Playwright)
    print("  === cn.wsj.com ===")
    cn_articles = scrape_cn_homepage(limit=CN_ARTICLES_LIMIT)
    for a in cn_articles:
        a['section'] = '🇨🇳 中文版'
        a['source'] = 'cn_home'
    
    # 去重：过滤掉已发过的文章
    before = len(cn_articles)
    cn_articles = [a for a in cn_articles if a.get("url") not in seen_urls]
    removed = before - len(cn_articles)
    if removed:
        print(f"  去重: 移除 {removed} 篇已发文章")

    # 2. cn.wsj.com 全文（有 cookie 的用 Playwright，否则跳过）
    if cn_articles:
        # 尝试用 cookie 方式获取全文
        cn_articles = fetch_fulltext_batch(cn_articles, timeout=30)

    # 3. RSS (Google News WSJ feeds)
    print("  === RSS (Google News WSJ) ===")
    rss_articles = collect_rss()
    
    # RSS 去重
    before = len(rss_articles)
    rss_articles = [a for a in rss_articles if (a.get("link") or a.get("url")) not in seen_urls]
    removed = before - len(rss_articles)
    if removed:
        print(f"  去重: 移除 {removed} 篇已发RSS文章")

    # 4. RSS 文章也抓正文（Playwright + cookies）
    if rss_articles:
        rss_articles = fetch_fulltext_batch(rss_articles, timeout=30)
    
    # 5. 所有文章配图
    all_articles = cn_articles + rss_articles

    # ── URL 去重：同一 URL 只保留第一篇（跨 RSS feed 重复） ──
    seen_in_batch = set()
    deduped = []
    for a in all_articles:
        url = a.get('url') or a.get('link', '')
        if url and url not in seen_in_batch:
            seen_in_batch.add(url)
            deduped.append(a)
    if len(deduped) < len(all_articles):
        print(f"  URL去重: {len(all_articles)} → {len(deduped)}（移除 {len(all_articles) - len(deduped)} 篇跨 feed 重复）")
    all_articles = deduped

    # ── 日期过滤：只保留 MAX_ARTICLE_AGE_DAYS 天内的文章 ──
    from email.utils import parsedate_to_datetime
    now = datetime.now(SH_TZ)
    cutoff = now - timedelta(days=MAX_ARTICLE_AGE_DAYS)

    def _parse_published_date(a):
        """解析文章发布日期，返回 datetime。解析失败返回 None。"""
        pub = a.get('published', '')
        if not pub:
            return None  # cn.wsj.com 首页文章无 published 字段
        try:
            return parsedate_to_datetime(pub).astimezone(SH_TZ)
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(pub.replace('Z', '+00:00'))
            return dt.astimezone(SH_TZ)
        except Exception:
            pass
        try:
            # cn.wsj.com 格式: "2026年7月24日 10:10 CST"
            m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})\s+(CST|GMT|EST|PST|UTC)', pub)
            if m:
                tz_map = {'CST': timezone(timedelta(hours=8)), 'GMT': timezone.utc,
                          'EST': timezone(timedelta(hours=-5)), 'PST': timezone(timedelta(hours=-8)), 'UTC': timezone.utc}
                tz = tz_map.get(m.group(6), SH_TZ)
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                              int(m.group(4)), int(m.group(5)), tzinfo=tz)
                return dt.astimezone(SH_TZ)
        except Exception:
            pass
        try:
            # wsj.com 格式: "Aug. 21, 2026 10:00 am ET"
            m = re.match(r'(\w+)\.?\s+(\d{1,2}),?\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*(am|pm)\s+(ET|CST|EST|PST|PDT|GMT|UTC)', pub)
            if m:
                months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
                mon = months.get(m.group(1)[:3], 1)
                hour = int(m.group(4))
                if m.group(6) == 'pm' and hour < 12:
                    hour += 12
                tz_map = {'ET': timezone(timedelta(hours=-5)), 'CST': timezone(timedelta(hours=8)),
                          'EST': timezone(timedelta(hours=-5)), 'PST': timezone(timedelta(hours=-8)),
                          'PDT': timezone(timedelta(hours=-7)), 'GMT': timezone.utc, 'UTC': timezone.utc}
                tz = tz_map.get(m.group(7), SH_TZ)
                dt = datetime(int(m.group(3)), mon, int(m.group(2)), hour, int(m.group(5)), tzinfo=tz)
                return dt.astimezone(SH_TZ)
        except Exception:
            pass
        return None

    before_filter = len(all_articles)
    # 只过滤有日期且超过 MAX_ARTICLE_AGE_DAYS 的文章
    # 无日期的 cn.wsj.com 文章保留（首页推荐默认最新），但排序时给中性分
    all_articles = [a for a in all_articles if not (dt := _parse_published_date(a)) or dt >= cutoff]
    removed_age = before_filter - len(all_articles)
    if removed_age:
        print(f"  日期过滤: 移除 {removed_age} 篇超过 {MAX_ARTICLE_AGE_DAYS} 天的文章")

    # ── 统一排序权重 ──
    # 时间分 (0-100) + 主题分 (0-30) + cn首页加权 (+10)
    # 无日期的 cn.wsj.com 文章给中性时间分 50（相当于 24-48h），不保底 100
    def _article_score(a):
        # 时间分
        pub_dt = _parse_published_date(a)
        if pub_dt is None:
            time_score = 50  # 无日期 → 中性分，不加分也不扣分
        else:
            age_hours = (now - pub_dt).total_seconds() / 3600
            if age_hours <= 12:
                time_score = 100
            elif age_hours <= 24:
                time_score = 80
            elif age_hours <= 48:
                time_score = 50
            else:
                time_score = 20

        # 主题分
        title = a.get('title', '') + ' ' + (a.get('ai_summary', '') or '')
        topic_score = 5  # 默认
        tech_kw = ['AI', '芯片', '科技', '技术', '人工智能', '数据', '算法', '机器人', '量子',
                     '华为', '英伟达', 'OpenAI', 'DeepSeek', '谷歌', '苹果', '微软', '特斯拉',
                     '互联网', '半导体', '算力', '大模型', '开源', '网络安全', '黑客']
        pol_kw = ['特朗普', '习近平', '政治', '政策', '制裁', '关税', '中美', '美中',
                   '台湾', '地缘', '战争', '军事', '外交', '白宫', '国会', '拜登',
                   '俄乌', '伊朗', '中东', '南海', '核', '导弹', '北约', 'NATO']
        fin_kw = ['市场', '并购', 'IPO', '股票', '债券', '比特币', '投资', '基金',
                   '央行', '利率', 'GDP', '通胀']
        for kw in tech_kw:
            if kw in title:
                topic_score = 30
                break
        if topic_score < 30:
            for kw in pol_kw:
                if kw in title:
                    topic_score = 25
                    break
        if topic_score < 25:
            for kw in fin_kw:
                if kw in title:
                    topic_score = 15
                    break

        # cn 首页加权
        cn_bonus = 10 if a.get('source') == 'cn_home' else 0

        return -(time_score + topic_score + cn_bonus)  # 负数用于降序

    all_articles.sort(key=_article_score)

    # 6. 去重记录不在采集阶段保存！
    # save_seen_urls 只在 generate_and_publish.py 成功发布后调用
    # 否则同一天重跑会导致所有文章被过滤

    elapsed = time.time() - t0
    has_img = sum(1 for a in all_articles if a.get('image'))
    print(f"\n  采集总计: CN {len(cn_articles)} + RSS {len(rss_articles)} = {len(all_articles)} 篇")
    print(f"  配图: {has_img}/{len(all_articles)} 篇有图")
    print(f"  采集耗时: {elapsed:.1f}s")

    result = {
        "date": datetime.now(SH_TZ).strftime("%Y年%m月%d日"),
        "article_count": len(all_articles),
        "articles": all_articles,
    }
    print("\n===COLLECT_RESULT_START===")
    print(json.dumps(result, ensure_ascii=False))
    print("===COLLECT_RESULT_END===")


if __name__ == "__main__":
    collect_all()
