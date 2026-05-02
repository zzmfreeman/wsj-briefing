#!/usr/bin/env python3
"""
WSJ 合并简报 v3
✓ 并行抓取 RSS + CN
✓ 合并后去重
✓ 超时告警
✓ 模型配置从 openclaw.json 读取（R5）
✓ 模型失败通知（R6）
✓ Discord 推送精简（R8）
✓ Cookie 检测（R9）
✓ 服务状态检查 + token/费用统计（R3）
✗ 已移除 Semi Brief 汇入代码（R1）
"""
import subprocess, json, re, time, signal, os, sys, concurrent.futures
from datetime import datetime
from pathlib import Path

from config import (
    SCRIPT_DIR, ARCHIVE_DIR, DISCORD_CHANNEL, DISCORD_IT_CH,
    check_cookie_health, notify_failure, send_discord_links_only,
    get_git_version, is_cookie_degraded,
)

SKIP_ENV = {"WSJ_DISCORD_SKIP": "1"}
TIMEOUT  = 360

# ── 超时告警 ──────────────────────────────────────────────
def _sigalarm(signum, frame):
    notify_failure(f"⚠️ WSJ简报超时（>{TIMEOUT}s），任务已被强制终止。")
    sys.exit(1)
signal.signal(signal.SIGALRM, _sigalarm)
signal.alarm(TIMEOUT)

# ── 运行单个子脚本 ──────────────────────────────────────────
def run_script(name, path, env_extra=None):
    env = os.environ.copy()
    env.update(env_extra or {})
    t0 = time.time()
    try:
        r = subprocess.run(["python3", str(path)], capture_output=True, text=True, timeout=300, cwd=str(SCRIPT_DIR), env=env)
        ok = r.returncode == 0
        elapsed = int(time.time() - t0)
        out = r.stdout + r.stderr
        print(f"  {name} {'✓' if ok else '✗'} ({elapsed}s)")
        if not ok:
            print(f"    → {out[-300:]}")
            notify_failure(f"⚠️ WSJ简报：{name} 执行失败（{elapsed}s），请检查日志。")
        return ok, elapsed, out
    except subprocess.TimeoutExpired:
        print(f"  {name} 超时！ ({int(time.time()-t0)}s)")
        notify_failure(f"⚠️ WSJ简报：{name} 超时（>{300}s）。")
        return False, int(time.time()-t0), ""

# ── 清理超期seen记录 ──────────────────────────────────────
def prune_seen():
    for sf in [SCRIPT_DIR / "seen_cn_home.json", SCRIPT_DIR / "seen_articles.json"]:
        if sf.exists():
            try:
                seen = json.loads(sf.read_text())
                if len(seen) > 200:
                    sf.write_text(json.dumps(seen[-200:]))
            except: pass

# ── 服务状态检查 ──────────────────────────────────────────
def check_services():
    """检查相关服务状态，返回 Markdown 报告"""
    checks = []
    now = datetime.now().strftime('%H:%M:%S')

    # 1. Cookie 健康
    ok, msg = check_cookie_health()
    checks.append(f"{'✅' if ok else '❌'} Cookie: {msg}")

    # 2. Docker
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=5)
        checks.append(f"{'✅' if r.returncode == 0 else '❌'} Docker: {'运行中' if r.returncode == 0 else '异常'}")
    except:
        checks.append("❌ Docker: 无法检查")

    # 3. Playwright
    try:
        r = subprocess.run(["python3", "-c", "from playwright.async_api import async_playwright; print('OK')"],
                          capture_output=True, text=True, timeout=5)
        checks.append(f"{'✅' if 'OK' in r.stdout else '❌'} Playwright: {'可用' if 'OK' in r.stdout else '异常'}")
    except:
        checks.append("❌ Playwright: 无法检查")

    # 4. openclaw.json 可读 + 模型配置
    try:
        from config import get_model_config
        cfg = get_model_config()
        checks.append(f"{'✅' if cfg else '❌'} 模型配置: {cfg[2] if cfg else '读取失败'}")
    except Exception as e:
        checks.append(f"❌ 模型配置: {e}")

    # 5. GitHub Pages 可达
    try:
        import urllib.request
        req = urllib.request.Request("https://zzmfreeman.github.io/openclaw_macmini_ICnews/wsj-index.html",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            checks.append(f"{'✅' if r.status == 200 else '❌'} GitHub Pages: HTTP {r.status}")
    except Exception as e:
        checks.append(f"❌ GitHub Pages: {str(e)[:40]}")

    # 6. Git
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=3)
        checks.append(f"✅ Git: {r.stdout.strip()}")
    except:
        checks.append("❌ Git: 无法检查")

    report = f"🔍 服务状态检查 [{now}]\n" + "\n".join(f"  {c}" for c in checks)
    return report

# ── Token/费用统计 ──────────────────────────────────────
def estimate_cost(output_text):
    """从子脚本输出中提取 token 用量，按模型分别估算费用"""
    # 解析 [LLM] model=xxx input_tokens=N output_tokens=M 行
    model_usage = {}  # model -> {input: int, output: int}
    for m in re.finditer(r'\[LLM\] model=(\S+) input_tokens=(\d+) output_tokens=(\d+)', output_text):
        model = m.group(1)
        inp = int(m.group(2))
        out = int(m.group(3))
        if model not in model_usage:
            model_usage[model] = {"input": 0, "output": 0}
        model_usage[model]["input"] += inp
        model_usage[model]["output"] += out

    # 模型定价 (per million tokens)
    PRICING = {
        "GLM-5.1": {"input": 0.5, "output": 1.5},      # sophnet 代理
        "MiniMax-M2.7": {"input": 0.1, "output": 0.2},  # MiniMax 官方
    }

    total_cost = 0
    lines = ["💰 Token 费用统计"]
    for model, usage in model_usage.items():
        pricing = PRICING.get(model, {"input": 0.5, "output": 1.5})
        cost = usage["input"] * pricing["input"] / 1_000_000 + usage["output"] * pricing["output"] / 1_000_000
        total_cost += cost
        lines.append(f"  {model}: in={usage['input']:,} out={usage['output']:,} → ${cost:.4f}")

    if not model_usage:
        lines.append("  (未捕获 token 数据)")

    lines.append(f"  合计: ${total_cost:.4f}")
    return "\n".join(lines), total_cost

# ── 主流程 ─────────────────────────────────────────────────
def main():
    t_total = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S CST')

    # R9: Cookie 健康检查
    cookie_ok, cookie_msg = check_cookie_health()
    print(f"  Cookie: {cookie_msg}")
    if not cookie_ok:
        notify_failure(f"⚠️ WSJ简报：Cookie 无效 - {cookie_msg}")

    # 服务状态检查
    health_report = check_services()
    print(health_report)

    # 清理旧归档 + seen
    rss_arch = SCRIPT_DIR / "archive" / f"{today}.md"
    cn_arch  = SCRIPT_DIR / "archive" / f"{today}_cn_home.md"
    for f in [rss_arch, cn_arch]:
        if f.exists(): f.unlink()
    prune_seen()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] WSJ 合并简报启动 (v3)")

    # 并行抓取（P0: Cookie 失效时跳过中文版）
    cn_ok, cn_t, cn_out = True, 0, ""
    if is_cookie_degraded():
        print("  ⚠️ Cookie 失效，跳过中文版（纯 RSS 模式）")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_rss = ex.submit(run_script, "英文RSS版", SCRIPT_DIR / "wsj_rss_briefing.py", SKIP_ENV)
        f_cn  = ex.submit(run_script, "中文版", SCRIPT_DIR / "wsj_cn_home_briefing.py", SKIP_ENV) if not is_cookie_degraded() else None
        rss_ok, rss_t, rss_out = f_rss.result()
        signal.alarm(max(1, TIMEOUT - int(time.time()-t_total)))
        if f_cn:
            cn_ok, cn_t, cn_out = f_cn.result()

    signal.alarm(0)

    if not rss_ok and not cn_ok:
        notify_failure("⚠️ WSJ简报：两个子脚本均执行失败，请检查日志。")
        return

    total_t = int(time.time() - t_total)

    # Generate WSJ web pages
    wsj_url, wsj_index = "", ""
    try:
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "generate_web.py")],
                          capture_output=True, text=True, timeout=120)
        for line in (r.stdout or "").split("\n"):
            m = re.search(r"(https://[^\s]+\.html)", line)
            if m:
                u = m.group(1)
                if "wsj-index" in u: wsj_index = u
                elif "wsj-" in u: wsj_url = u
        print(f"  WSJ web: {wsj_url[:60] if wsj_url else '?'}")
    except Exception as e:
        print(f"  Web gen failed: {e}")
        notify_failure(f"⚠️ WSJ简报：网页生成失败 - {e}")

    # Token/费用统计
    combined_output = rss_out + cn_out
    cost_report, total_cost = estimate_cost(combined_output)

    # R8: Discord 推送版本号（不发链接）
    ver = get_git_version()
    parts = [f"📊 WSJ简报 v{ver} | ⏱ {total_t}s | RSS({rss_t}s){'✓' if rss_ok else '✗'} CN({cn_t}s){'✓' if cn_ok else '✗'}"]
    parts.append(f"🕐 {generated_at}")
    if wsj_url:   parts.append(f"🌐 <{wsj_url}>")
    if wsj_index: parts.append(f"📋 <{wsj_index}>")
    send_discord_links_only("\n".join(parts))

    # 发送服务状态 + 费用统计到 WSJ 频道
    send_discord_links_only(health_report)
    send_discord_links_only(cost_report)

    print(f"\n✅ 完成！总耗时 {total_t}s，费用 ${total_cost:.4f}")
    print(cost_report)
    signal.alarm(0)

if __name__ == "__main__":
    main()
