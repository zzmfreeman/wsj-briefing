"""
WSJ Briefing 共享配置
- 模型配置从 openclaw.json 动态读取（R5）
- Cookie 过期检测（R9）+ 自动降级
- Discord 通知统一接口（R6/R9）+ 重试
- LLM 调用 + 余额预检 + fallback + 重试
"""
import json, time, subprocess, os
from pathlib import Path
from datetime import datetime, timezone, timedelta

OPENCLAW_CONFIG = Path(os.environ.get("OPENCLAW_CONFIG", str(Path.home() / ".openclaw" / "openclaw.json")))
SCRIPT_DIR     = Path(__file__).parent
COOKIE_FILE    = Path(os.environ.get("COOKIE_FILE", str(SCRIPT_DIR / "cn_wsj_cookies.txt")))
ARCHIVE_DIR    = Path(os.environ.get("ARCHIVE_DIR", str(SCRIPT_DIR / "archive")))
ARCHIVE_DIR.mkdir(exist_ok=True)

# Discord 频道
DISCORD_CHANNEL = "1495744491586584647"  # 📰WSJ Cron thread
DISCORD_IT_CH   = "1478264573776892047"  # IT 频道（故障通知）
DISCORD_BOT_TOKEN = None  # 从 openclaw.json 读取

# ── Discord Bot Token ──────────────────────────────────
def _get_discord_bot_token():
    """从 openclaw.json 读取 Discord bot token"""
    global DISCORD_BOT_TOKEN
    if DISCORD_BOT_TOKEN is not None:
        return DISCORD_BOT_TOKEN
    try:
        cfg = json.loads(OPENCLAW_CONFIG.read_text())
        for key in ['channels', 'discord']:
            if key in cfg:
                cfg = cfg[key]
        DISCORD_BOT_TOKEN = cfg.get('token', '')
    except:
        pass
    return DISCORD_BOT_TOKEN

# ── Discord 发送（P0: 重试 3 次）──────────────────────
def _discord_api_send(channel_id, message, retries=3):
    """通过 Discord Bot API 直连发送消息，重试 retries 次"""
    token = _get_discord_bot_token()
    if not token:
        print("  [Discord] 无 bot token，跳过发送")
        return False
    payload = json.dumps({"content": message})
    for attempt in range(1, retries + 1):
        try:
            r = subprocess.run(
                ['curl', '-s', '-m', '15', '-X', 'POST',
                 '-H', f'Authorization: Bot {token}',
                 '-H', 'Content-Type: application/json',
                 '-d', payload,
                 f'https://discord.com/api/v10/channels/{channel_id}/messages'],
                capture_output=True, text=True, timeout=20
            )
            if r.returncode == 0 and '"id"' in r.stdout:
                return True
            if attempt < retries:
                print(f"  [Discord] 第{attempt}次发送失败，{5}s 后重试...")
                time.sleep(5)
        except Exception as e:
            if attempt < retries:
                print(f"  [Discord] 第{attempt}次异常: {e}，重试...")
                time.sleep(5)
            else:
                print(f"  [Discord] 发送异常: {e}")
    print(f"  [Discord] {retries}次重试均失败")
    return False

# ── 模型配置（R5：从 openclaw.json 读取）─────────────────
def get_model_config():
    """从 openclaw.json 读取 primary 模型配置。返回 (base_url, api_key, model_name) 或 None"""
    try:
        cfg = json.loads(OPENCLAW_CONFIG.read_text())
    except Exception as e:
        notify_failure(f"⚠️ WSJ Briefing：无法读取 openclaw.json：{e}")
        return None

    primary = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
    if not primary:
        notify_failure("⚠️ WSJ Briefing：openclaw.json 中未设置默认模型")
        return None

    if "/" in primary:
        provider_id, model_name = primary.split("/", 1)
    else:
        provider_id = primary
        model_name = ""

    providers = cfg.get("models", {}).get("providers", {})
    if not providers:
        providers = cfg.get("providers", {})

    matched = None
    for pid, pconf in providers.items():
        if pid == provider_id or pid.startswith(provider_id) or provider_id.startswith(pid):
            matched = (pid, pconf)
            break
    if not matched:
        for pid, pconf in providers.items():
            if "sophnet" in pid.lower() and pconf.get("apiKey"):
                matched = (pid, pconf)
                break
    if not matched:
        for pid, pconf in providers.items():
            if pconf.get("baseUrl") and pconf.get("apiKey"):
                matched = (pid, pconf)
                break

    if not matched:
        notify_failure("⚠️ WSJ Briefing：openclaw.json 中未找到可用的模型 provider 配置")
        return None

    pid, pconf = matched
    base_url = pconf.get("baseUrl", "")
    api_key  = pconf.get("apiKey", "")
    if not model_name:
        model_name = pconf.get("defaultModel", "GLM-5.1")
    if base_url and not base_url.endswith("/chat/completions"):
        base_url = base_url.rstrip("/") + "/chat/completions"

    if base_url and api_key:
        return base_url, api_key, model_name
    notify_failure(f"⚠️ WSJ Briefing：provider {pid} 缺少 baseUrl 或 apiKey")
    return None

def get_fallback_config():
    """从 openclaw.json 读取 fallback 模型（MiniMax）配置。返回 (base_url, api_key, model_name, api_format) 或 None"""
    try:
        cfg = json.loads(OPENCLAW_CONFIG.read_text())
    except:
        return None
    providers = cfg.get("models", {}).get("providers", {})
    if not providers:
        providers = cfg.get("providers", {})
    for pid, pconf in providers.items():
        if "minimax" in pid.lower():
            api_key = pconf.get("apiKey", "")
            models = pconf.get("models", [])
            model_name = models[0]["id"] if models else "MiniMax-M2.7"
            base_url = "https://api.minimaxi.com/v1/chat/completions"
            if api_key:
                return base_url, api_key, model_name, "openai"
    return None

def get_git_version():
    """获取当前 git 版本号"""
    try:
        r = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5, cwd=str(SCRIPT_DIR))
        if r.returncode == 0:
            return r.stdout.strip()
    except:
        pass
    return "unknown"

# ── P0: 余额预检 ─────────────────────────────────────
def check_primary_balance():
    """检查 primary 模型余额是否充足。返回 (ok: bool, reason: str)"""
    import urllib.request, urllib.error
    config = get_model_config()
    if not config:
        return False, "无法读取模型配置"
    base_url, api_key, model = config
    # 用一个极小请求测试余额（1 token）
    try:
        payload = json.dumps({
            'model': model, 'max_tokens': 1,
            'messages': [{'role': 'user', 'content': 'hi'}]
        }).encode('utf-8')
        req = urllib.request.Request(
            base_url, data=payload,
            headers={'Authorization': f'Bearer {api_key}', 'content-type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return True, f"{model} 余额正常"
    except urllib.error.HTTPError as e:
        if e.code == 402:
            return False, f"{model} 余额不足(402)"
        return False, f"{model} HTTP {e.code}"
    except Exception as e:
        return False, f"{model} 检查失败: {e}"

# ── Cookie 检测（R9 + P0: 自动降级标志）────────────────
_cookie_degraded = False

def check_cookie_health():
    """检测 cookie 文件是否有效。返回 (ok: bool, message: str)"""
    global _cookie_degraded
    if not COOKIE_FILE.exists():
        _cookie_degraded = True
        return False, "Cookie 文件不存在"

    now_ts = time.time()
    total = valid = 0
    earliest_exp = float('inf')

    for line in COOKIE_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        parts = line.split('\t')
        if len(parts) < 7:
            continue
        total += 1
        try:
            exp = int(parts[4])
            if exp > 0:
                if exp > now_ts:
                    valid += 1
                    earliest_exp = min(earliest_exp, exp)
        except:
            valid += 1

    if total == 0:
        _cookie_degraded = True
        return False, "Cookie 文件为空"
    if valid == 0:
        _cookie_degraded = True
        return False, f"所有 {total} 条 cookie 已过期"

    _cookie_degraded = False
    if earliest_exp != float('inf'):
        hours_left = (earliest_exp - now_ts) / 3600
        if hours_left < 24:
            return True, f"⚠️ Cookie 将在 {hours_left:.1f}h 后过期"
    return True, f"Cookie 有效（{valid}/{total} 条）"

def is_cookie_degraded():
    """Cookie 是否已失效（用于 P0 自动降级）"""
    return _cookie_degraded

# ── Discord 通知（R6/R9）───────────────────────────────
def notify_failure(msg, channel=None):
    """故障通知：发到 IT 频道"""
    ch = channel or DISCORD_IT_CH
    _discord_api_send(ch, msg)

def send_discord_links_only(stats_text, links_text=""):
    """R8：Discord 推送精简版（只发链接+统计，不发摘要全文）"""
    msg = stats_text
    if links_text:
        msg += "\n" + links_text
    ok = _discord_api_send(DISCORD_CHANNEL, msg)
    if ok:
        print(f"  [Discord] ✅ 已发送")
    else:
        # Fallback: 写入待发送文件
        _PENDING_FILE = SCRIPT_DIR / "_pending_discord.json"
        try:
            pending = []
            if _PENDING_FILE.exists():
                pending = json.loads(_PENDING_FILE.read_text())
            pending.append({"channel": DISCORD_CHANNEL, "message": msg})
            _PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False))
            print(f"  [Discord] ⚠️ API失败，已写入待发送队列")
        except Exception as e:
            print(f"  [Discord] 写入待发送失败: {e}")

# ── 模型调用封装（P0: 余额预检 + fallback + 重试）────
def _call_openai(base_url, api_key, model, prompt, max_tokens, temperature, timeout):
    """OpenAI chat/completions 格式调用"""
    import urllib.request, urllib.error
    url = base_url
    if not url.endswith("/chat/completions"):
        url = url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        'model': model, 'max_tokens': max_tokens, 'temperature': temperature,
        'messages': [{'role': 'user', 'content': prompt}]
    }).encode('utf-8')
    req = urllib.request.Request(
        url, data=payload,
        headers={'Authorization': f'Bearer {api_key}', 'content-type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
        usage = resp.get('usage', {})
        if usage:
            inp = usage.get('prompt_tokens', 0)
            out = usage.get('completion_tokens', 0)
            print(f'[LLM] model={model} input_tokens={inp} output_tokens={out}')
        if 'choices' in resp:
            return resp['choices'][0]['message']['content']
        elif 'content' in resp:
            return resp['content'][0]['text']
        else:
            raise ValueError(f"响应格式异常：{list(resp.keys())}")

def call_llm(prompt, max_tokens=4000, temperature=0.3, timeout=120):
    """
    调用 LLM 生成摘要。
    P0: 余额预检 → primary → fallback → 重试
    返回 (success: bool, result: str|None)
    """
    import urllib.error

    # ── P0: 余额预检 ──
    balance_ok, balance_msg = check_primary_balance()
    if balance_ok:
        config = get_model_config()
        if config:
            base_url, api_key, model = config
            # 重试 2 次
            for attempt in range(1, 3):
                try:
                    result = _call_openai(base_url, api_key, model, prompt, max_tokens, temperature, timeout)
                    return True, result
                except urllib.error.HTTPError as e:
                    print(f"[LLM] Primary {model} attempt {attempt} failed: HTTP {e.code}")
                    if attempt < 2:
                        time.sleep(2)
                except Exception as e:
                    print(f"[LLM] Primary {model} attempt {attempt} failed: {e}")
                    if attempt < 2:
                        time.sleep(2)
    else:
        print(f"[LLM] 余额预检: {balance_msg}，跳过 primary")

    # ── Fallback ──
    fb = get_fallback_config()
    if fb:
        base_url, api_key, model, api_fmt = fb
        print(f"[LLM] Fallback to {model}")
        for attempt in range(1, 3):
            try:
                result = _call_openai(base_url, api_key, model, prompt, max_tokens, temperature, timeout)
                return True, result
            except urllib.error.HTTPError as e:
                print(f"[LLM] Fallback {model} attempt {attempt} failed: HTTP {e.code}")
                if attempt < 2:
                    time.sleep(2)
            except Exception as e:
                print(f"[LLM] Fallback {model} attempt {attempt} failed: {e}")
                if attempt < 2:
                    time.sleep(2)
        notify_failure(f"⚠️ WSJ Briefing：Fallback {model} 2次重试均失败")
    else:
        notify_failure("⚠️ WSJ Briefing：Primary 不可用且无 fallback")

    return False, None
