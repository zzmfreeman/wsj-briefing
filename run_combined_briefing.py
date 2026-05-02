#!/usr/bin/env python3
"""
WSJ 合并简报 v2（重构版）
✓ 并行抓取 RSS + CN
✓ 合并后去重
✓ 超时发 Discord 告警
✓ 模型配置从 openclaw.json 读取（R5）
✓ 模型失败通知（R6）
✓ Discord 推送精简（R8）
✓ Cookie 检测（R9）
✗ 已移除 Semi Brief 汇入代码（R1：独立项目）
"""
import subprocess, json, re, time, signal, os, sys, concurrent.futures
from datetime import datetime
from pathlib import Path

from config import (
    SCRIPT_DIR, ARCHIVE_DIR, DISCORD_CHANNEL,
    check_cookie_health, notify_failure, send_discord_links_only,
)

SKIP_ENV = {"WSJ_DISCORD_SKIP": "1"}
TIMEOUT  = 360

# ── 超时告警 ──────────────────────────────────────────────
class TimeoutException(Exception):
    pass

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
        r = subprocess.run(
            ["python3", str(path)],
            capture_output=True, text=True, timeout=300,
            cwd=str(SCRIPT_DIR), env=env
        )
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
                    print(f"  seen记录已精简至200条")
            except Exception as e:
                print(f"  seen精简失败: {e}")

# ── 主流程 ─────────────────────────────────────────────────
def main():
    t_total = time.time()
    today    = datetime.now().strftime("%Y-%m-%d")

    # R9: Cookie 健康检查
    cookie_ok, cookie_msg = check_cookie_health()
    print(f"  Cookie: {cookie_msg}")
    if not cookie_ok:
        notify_failure(f"⚠️ WSJ简报：Cookie 无效 - {cookie_msg}")

    # 清理旧归档 + seen
    rss_arch = SCRIPT_DIR / "archive" / f"{today}.md"
    cn_arch  = SCRIPT_DIR / "archive" / f"{today}_cn_home.md"
    for f in [rss_arch, cn_arch]:
        if f.exists(): f.unlink()
    prune_seen()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] WSJ 合并简报启动 (v2)")

    # 并行抓取
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_rss = ex.submit(run_script, "英文RSS版", SCRIPT_DIR / "wsj_rss_briefing.py", SKIP_ENV)
        f_cn  = ex.submit(run_script, "中文版", SCRIPT_DIR / "wsj_cn_home_briefing.py", SKIP_ENV)
        rss_ok, rss_t, rss_out = f_rss.result()
        signal.alarm(max(1, TIMEOUT - int(time.time()-t_total)))
        cn_ok,  cn_t,  cn_out  = f_cn.result()

    signal.alarm(0)

    if not rss_ok and not cn_ok:
        notify_failure("⚠️ WSJ简报：两个子脚本均执行失败，请检查日志。")
        return

    # 统计
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
                if "wsj-index" in u:
                    wsj_index = u
                elif "wsj-" in u:
                    wsj_url = u
        print(f"  WSJ web: {wsj_url[:60] if wsj_url else '?'}")
    except Exception as e:
        print(f"  Web gen failed: {e}")
        notify_failure(f"⚠️ WSJ简报：网页生成失败 - {e}")

    # R8: Discord 只发链接+统计，不发摘要全文
    parts = [f"📊 WSJ简报 | ⏱ {total_t}s | RSS({rss_t}s){'✓' if rss_ok else '✗'} CN({cn_t}s){'✓' if cn_ok else '✗'}"]
    if wsj_url:   parts.append(f"🌐 今日: <{wsj_url}>")
    if wsj_index: parts.append(f"📋 索引: <{wsj_index}>")
    send_discord_links_only("\n".join(parts))

    print(f"\n✅ 完成！总耗时 {total_t}s")
    signal.alarm(0)

if __name__ == "__main__":
    main()
