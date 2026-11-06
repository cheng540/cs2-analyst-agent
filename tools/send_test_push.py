"""手动发送一条测试推送（用于确认企业微信机器人链路是否正常）。

用法：
    python tools/send_test_push.py            # 发送测试消息
    python tools/send_test_push.py "自定义内容"
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cs2analyst.config import load_config  # noqa: E402
from cs2analyst.notify.wecom import send  # noqa: E402


def main() -> int:
    cfg = load_config()
    hook = str(cfg.notify.get("wecom_webhook") or "").strip()
    if not hook:
        print("❌ 未配置企业微信机器人地址（config.yaml 的 notify.wecom_webhook）")
        return 1
    custom = " ".join(sys.argv[1:]).strip()
    text = custom or (
        f"**推送链路测试** ✅\n"
        f"> 如果你能看到这条消息，说明**推送完全正常**\n\n"
        f"**以后每天 17:30 你会自动收到：**\n"
        f"- 大盘环境（趋势 / 风险 / 量能）\n"
        f"- 持仓建议：需要动作的标的 + 理由\n"
        f"- A / B 双轨模拟盘净值\n"
        f"- AI 研判摘要 + 当日 V社更新要点\n\n"
        f"测试时间：{datetime.now():%Y-%m-%d %H:%M:%S}"
    )
    ok = send(text, hook)
    print("✅ 发送成功，请查看企业微信" if ok else "❌ 发送失败，请检查 webhook 地址")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
