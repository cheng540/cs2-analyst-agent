"""环境自检：确认本机（本地或云服务器）能否正常跑通全流程。

用法：
    python tools/server_check.py            # 全部检查（含发送一条企业微信测试消息）
    python tools/server_check.py --no-notify  # 跳过推送测试

检查项：
    1. 运行环境（Python 版本、时区、工作目录）
    2. 配置文件读取（API Key / 模型 / 推送地址是否就位）
    3. SteamDT 开放平台 API（大盘指数 + 单品K线）
    4. SteamDT 官网公开接口（大盘统计）
    5. DeepSeek LLM 接口（最小调用）
    6. 企业微信机器人推送
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.logging_setup import get_logger, setup_logging  # noqa: E402

log = get_logger("server_check")

OK = "✅"
FAIL = "❌"
WARN = "⚠️"


def check_env() -> bool:
    print(f"\n【1】运行环境")
    print(f"  Python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"  工作目录: {Path.cwd()}")
    print(f"  本地时间: {datetime.now():%Y-%m-%d %H:%M:%S}  时区: {datetime.now().astimezone().tzname()}")
    if sys.version_info < (3, 9):
        print(f"  {FAIL} Python 版本过低（需要 3.9+）")
        return False
    print(f"  {OK} Python 版本可用")
    return True


def check_config(cfg) -> bool:
    print(f"\n【2】配置文件")
    print(f"  配置文件: {cfg.config_path}")
    ok = True
    if cfg.has_api_key():
        print(f"  {OK} SteamDT API Key: {cfg.steamdt['api_key'][:8]}…（已配置）")
    else:
        print(f"  {FAIL} SteamDT API Key 未配置")
        ok = False
    if cfg.has_llm_key():
        print(f"  {OK} DeepSeek Key: {cfg.llm['api_key'][:8]}…（模型 {cfg.llm.get('model')}）")
    else:
        print(f"  {WARN} DeepSeek Key 未配置（AI 研判会跳过）")
    hook = str(cfg.notify.get("wecom_webhook") or "")
    if hook:
        print(f"  {OK} 企业微信推送: …{hook[-8:]}（已配置）")
    else:
        print(f"  {WARN} 企业微信推送未配置")
    print(f"  模拟盘：初始 ¥{cfg.sim.get('initial_capital')} / T+{cfg.sim.get('hold_days_min')} / "
          f"双轨={cfg.sim.get('dual_track')}")
    return ok


def check_steamdt(cfg) -> bool:
    print(f"\n【3】SteamDT 开放平台 API")
    from cs2analyst.datasource.steamdt import SteamDTClient
    client = SteamDTClient(cfg.steamdt)
    ok = True
    try:
        idx = client.broad_index()
        print(f"  {OK} 大盘指数：{idx.get('broadMarketIndex')}（较昨日 {idx.get('diffYesterdayRatio')}%）")
    except Exception as exc:
        print(f"  {FAIL} 大盘指数失败：{exc}")
        ok = False
    try:
        k = client.item_kline("AK-47 | Redline (Field-Tested)", 2)
        print(f"  {OK} 单品K线：{len(k)} 根（最新收盘 ¥{float(k['close'].iloc[-1]):.2f}）")
    except Exception as exc:
        print(f"  {FAIL} 单品K线失败：{exc}")
        ok = False
    return ok


def check_scraper(cfg) -> bool:
    print(f"\n【4】SteamDT 官网公开接口（免 Key）")
    from cs2analyst.datasource.steamdt_scraper import SteamDTScraper
    scraper = SteamDTScraper(timeout=float(cfg.steamdt.get("timeout", 15)), retry=2)
    try:
        s = scraper.market_summary()
        m = s.get("marketIndexTrend") or {}
        print(f"  {OK} 大盘统计：指数 {m.get('broadMarketIndex')}，涨跌类型 {m.get('riseFallType')}")
        return True
    except Exception as exc:
        print(f"  {FAIL} 官网统计失败：{exc}")
        return False


def check_llm(cfg) -> bool:
    print(f"\n【5】DeepSeek LLM 接口")
    if not cfg.has_llm_key():
        print(f"  {WARN} 未配置 Key，跳过")
        return True
    from cs2analyst.llm.client import LLMClient
    llm = LLMClient(cfg.llm)
    try:
        reply = llm.chat([{"role": "user", "content": "只回复两个字：在线"}],
                         temperature=0.1, max_tokens=20)
        print(f"  {OK} 模型回复：{reply.strip()[:40]}（tokens={llm.last_usage.get('total_tokens')}）")
        return True
    except Exception as exc:
        print(f"  {FAIL} LLM 调用失败：{exc}")
        return False


def check_notify(cfg, send: bool = True) -> bool:
    print(f"\n【6】企业微信机器人推送")
    hook = str(cfg.notify.get("wecom_webhook") or "")
    if not hook:
        print(f"  {WARN} 未配置，跳过")
        return True
    if not send:
        print(f"  {WARN} 已跳过（--no-notify）")
        return True
    import requests
    try:
        r = requests.post(hook, json={
            "msgtype": "markdown",
            "markdown": {"content": (
                "**CS2 分析助手 · 部署自检**\n"
                f"> 运行环境自检通过 {OK}\n"
                f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
                "本条为部署自检消息，无需回复")},
        }, timeout=20)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if data.get("errcode") == 0:
            print(f"  {OK} 测试消息已发送（请查看企业微信）")
            return True
        print(f"  {FAIL} 推送失败：{data}")
        return False
    except Exception as exc:
        print(f"  {FAIL} 推送异常：{exc}")
        return False


def main() -> int:
    send_notify = "--no-notify" not in sys.argv
    cfg = load_config()
    setup_logging(cfg.log_dir())
    print("=" * 60)
    print("CS2 饰品持仓分析 - 环境自检")
    print("=" * 60)
    results = {
        "运行环境": check_env(),
        "配置文件": check_config(cfg),
        "SteamDT API": check_steamdt(cfg),
        "官网公开接口": check_scraper(cfg),
        "DeepSeek": check_llm(cfg),
        "企业微信推送": check_notify(cfg, send_notify),
    }
    print("\n" + "=" * 60)
    for name, ok in results.items():
        print(f"  {OK if ok else FAIL} {name}")
    passed = sum(1 for v in results.values() if v)
    print(f"\n结果：{passed}/{len(results)} 项通过")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
