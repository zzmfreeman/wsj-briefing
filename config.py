"""
WSJ Briefing 共享配置
- 模型配置从 openclaw.json 动态读取（R5）
- Cookie 过期检测（R9）
- Discord 通知统一接口（R6/R9）
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
DISCORD_CHANNEL = "1478404345782472754"  # WSJ 简报频道
DISCORD_IT_CH   = "1478264573776892047"  # IT 频道（故障通知）

# ── 模型配置（R5：从 openclaw.json 读取）─────────────────
def get_model_config():
    """
    从 openclaw.json 读取当前频道默认模型配置。
    返回 (base_url, api_key, model_name)
    如果读取失败，返回 None 并通知用户。
    """
    try:
        cfg = json.loads(OPENCLAW_CONFIG.read_text())
    except Exception as e:
        notify_failure(f"⚠️ WSJ Briefing：无法读取 openclaw.json：{e}")
        return None

    # 获取 primary model 名（格式：provider-id/model-name）
    primary = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
    if not primary:
        notify_failure("⚠️ WSJ Briefing：openclaw.json 中未设置默认模型")
        return None

    # 解析 provider id 和 model name
    if "/" in primary:
        provider_id, model_name = primary.split("/", 1)
    else:
        provider_id = primary
        model_name = ""

    # providers 在 models.providers 下
    providers = cfg.get("models", {}).get("providers", {})
    # 也检查顶级 providers（兼容旧格式）
    if not providers:
        providers = cfg.get("providers", {})

    # 找匹配的 provider
    # provider_id 可能是 "custom-www-sophnet-com" 而 config 里是 "custom-www-sophnet-com-glm5"
    # 需要匹配前缀
    matched = None
    for pid, pconf in providers.items():
        if pid == provider_id or pid.startswith(provider_id) or provider_id.startswith(pid):
            matched = (pid, pconf)
            break

    # 如果没匹配到，找有 apiKey 的第一个 sophnet provider
    if not matched:
        for pid, pconf in providers.items():
            if "sophnet" in pid.lower() and pconf.get("apiKey"):
                matched = (pid, pconf)
                break

    # 最终 fallback：任何有 apiKey + baseUrl 的 provider
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

    # model_name: 如果从 primary 解析出来了就用，否则用 provider 的 defaultModel
    if not model_name:
        model_name = pconf.get("defaultModel", "GLM-5.1")

    # 确保 base_url 包含 /chat/completions 路径
    if base_url and not base_url.endswith("/chat/completions"):
        base_url = base_url.rstrip("/") + "/chat/completions"

    if base_url and api_key:
        return base_url, api_key, model_name

    notify_failure(f"⚠️ WSJ Briefing：provider {pid} 缺少 baseUrl 或 apiKey")
    return None

# ── Cookie 检测（R9）────────────────────────────────────
def check_cookie_health():
    """
    检测 cookie 文件是否有效。
    返回 (ok: bool, message: str)
    """
    if not COOKIE_FILE.exists():
        return False, "Cookie 文件不存在"

    now_ts = time.time()
    total = 0
    valid = 0
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
            valid += 1  # session cookie, no expiry

    if total == 0:
        return False, "Cookie 文件为空"

    if valid == 0:
        return False, f"所有 {total} 条 cookie 已过期"

    # 检查是否即将过期（24h 内）
    if earliest_exp != float('inf'):
        hours_left = (earliest_exp - now_ts) / 3600
        if hours_left < 24:
            return True, f"⚠️ Cookie 将在 {hours_left:.1f} 小时后过期，请尽快更新"

    return True, f"Cookie 有效（{valid}/{total} 条）"

# ── Discord 通知（R6/R9）───────────────────────────────
def notify_failure(msg, channel=None):
    """故障通知：发到 IT 频道（确保能看到）"""
    ch = channel or DISCORD_IT_CH
    try:
        subprocess.run(
            ['openclaw', 'message', 'send', '--channel', 'discord',
             '--target', ch, '--message', msg],
            capture_output=True, text=True, timeout=15
        )
    except:
        pass

def send_discord_links_only(stats_text, links_text=""):
    """
    R8：Discord 推送精简版（只发链接+统计，不发摘要全文）
    """
    msg = stats_text
    if links_text:
        msg += "\n" + links_text
    try:
        subprocess.run(
            ['openclaw', 'message', 'send', '--channel', 'discord',
             '--target', DISCORD_CHANNEL, '--message', msg],
            capture_output=True, text=True, timeout=30
        )
    except:
        pass

# ── 模型调用封装（含失败通知 R6）────────────────────────
def call_llm(prompt, max_tokens=4000, temperature=0.3, timeout=120):
    """
    调用 LLM 生成摘要。
    从 openclaw.json 读取配置（R5）。
    失败时通知用户（R6）。
    返回 (success: bool, result: str|None)
    """
    import urllib.request, urllib.error

    config = get_model_config()
    if not config:
        return False, None

    base_url, api_key, model = config

    payload = json.dumps({
        'model': model,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'messages': [{'role': 'user', 'content': prompt}]
    }).encode('utf-8')

    req = urllib.request.Request(
        base_url, data=payload,
        headers={'Authorization': f'Bearer {api_key}', 'content-type': 'application/json'}
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
            # 提取 token 用量并打印（供 run_combined_briefing.py 解析）
            usage = resp.get('usage', {})
            if usage:
                inp = usage.get('prompt_tokens', 0)
                out = usage.get('completion_tokens', 0)
                print(f'[LLM] input_tokens={inp} output_tokens={out}')
            # 兼容不同 API 响应格式
            if 'choices' in resp:
                return True, resp['choices'][0]['message']['content']
            elif 'content' in resp:
                return True, resp['content'][0]['text']
            else:
                notify_failure(f"⚠️ WSJ Briefing：模型响应格式异常：{list(resp.keys())}")
                return False, None
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:300]
        notify_failure(f"⚠️ WSJ Briefing：模型调用失败 HTTP {e.code}：{body}")
        return False, None
    except Exception as e:
        notify_failure(f"⚠️ WSJ Briefing：模型调用异常：{e}")
        return False, None
