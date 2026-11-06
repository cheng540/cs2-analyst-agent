"""每日推送：把当日结论推到手机（默认企业微信机器人）。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import requests

from ..logging_setup import get_logger

log = get_logger(__name__)

WECOM_LIMIT = 4000  # 企业微信 markdown 内容上限约 4096 字节


def _short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def build_daily_message(result: dict) -> str:
    """由流水线结果生成推送正文（企业微信 markdown）。"""
    date_str = str(result.get("run_time", ""))[:10] or datetime.now().strftime("%Y-%m-%d")
    market = result.get("market") or {}
    portfolio = result.get("portfolio") or {}
    lines: list[str] = [f"**CS2 持仓日报 · {date_str}**"]

    lines.append(f"大盘：{market.get('trend', '-')} / {market.get('risk', '-')}")
    lines.append(f"组合：市值 ¥{portfolio.get('total_value', 0):,.2f}，"
                 f"盈亏 {portfolio.get('pnl_pct', 0):+.2f}%")

    # 建议分布
    decisions = result.get("decisions") or []
    if decisions:
        from collections import Counter
        cnt = Counter(d.get("action") for d in decisions)
        parts = [f"{label} {cnt.get(action, 0)}"
                 for action, label in [("CLEAR", "清仓"), ("REDUCE", "减仓"),
                                       ("HOLD", "持有"), ("ADD", "加仓"), ("BUY", "买入")]
                 if cnt.get(action)]
        lines.append("")
        lines.append(f"**持仓建议**：{' | '.join(parts)}")
        # 需要动作的标的
        for d in decisions:
            if d.get("action") in ("CLEAR", "REDUCE", "BUY", "ADD"):
                name = d.get("name_cn") or d.get("name", "")
                reason = _short("；".join(d.get("reasons") or []), 60)
                lines.append(f"> {d.get('type', '')} {name}")
                if reason:
                    lines.append(f">   {reason}")

    # 模拟盘
    sim = result.get("sim") or {}
    tracks = sim.get("tracks") or {}
    if tracks:
        lines.append("")
        lines.append("**模拟盘（各 ¥%s）**" % f"{tracks.get('A', {}).get('snapshot', {}).get('initial_capital', 0):,.0f}")
        for label in sorted(tracks):
            snap = tracks[label].get("snapshot") or {}
            name = tracks[label].get("name", "")
            tcount = (tracks[label].get("entry") or {}).get("trades", 0)
            lines.append(f"> {label} 轨（{name}）：¥{snap.get('net_value', 0):,.2f}"
                         f"（{snap.get('return_pct', 0):+.2f}%），"
                         f"持仓 {len(snap.get('positions') or [])} 项，今日 {tcount} 笔")

    # AI 研判
    judgement = sim.get("judgement") or {}
    if judgement.get("market_view"):
        lines.append("")
        lines.append("**AI 研判**")
        lines.append(f"> {_short(judgement['market_view'], 150)}")
    if judgement.get("portfolio_advice"):
        lines.append(f"> {_short(judgement['portfolio_advice'], 150)}")

    # 情报
    intel = sim.get("intel_summary") or {}
    if intel.get("top_news"):
        lines.append("")
        lines.append(f"**今日情报**（更新 {intel.get('news_count', 0)} 条 / 异动 "
                     f"{intel.get('anomaly_count', 0)} 条）")
        for title in intel["top_news"][:3]:
            lines.append(f"> · {_short(title, 50)}")

    report = result.get("report_path")
    if report:
        lines.append("")
        lines.append("完整报告见下方附件（Word）")
    msg = "\n".join(lines)
    if len(msg.encode("utf-8")) > WECOM_LIMIT:
        msg = msg.encode("utf-8")[:WECOM_LIMIT - 3].decode("utf-8", "ignore") + "…"
    return msg


def _webhook_key(webhook: str) -> str:
    """从 webhook 地址里取出 key 参数。"""
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(webhook).query)
    return (qs.get("key") or [""])[0]


def send(payload_text: str, webhook: str, msgtype: str = "markdown") -> bool:
    """发送文本/markdown 到企业微信机器人。"""
    try:
        payload = {"msgtype": msgtype, msgtype: {"content": payload_text}}
        resp = requests.post(webhook, json=payload, timeout=20)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if data.get("errcode") == 0:
            log.info("企业微信推送成功（%d 字节）", len(payload_text.encode("utf-8")))
            return True
        log.warning("企业微信推送失败：%s", json.dumps(data, ensure_ascii=False)[:200])
        return False
    except Exception as exc:
        log.warning("企业微信推送异常：%s", exc)
        return False


def send_file(webhook: str, file_path, max_mb: float = 20.0) -> bool:
    """把文件（Word 报告）作为附件推送到企业微信。

    流程：先 upload_media 换 media_id，再发 file 类型消息。
    """
    from pathlib import Path
    path = Path(file_path)
    if not path.exists():
        log.warning("附件不存在，跳过：%s", path)
        return False
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > max_mb:
        log.warning("附件过大（%.1f MB > %.0f MB），跳过", size_mb, max_mb)
        return False
    key = _webhook_key(webhook)
    if not key:
        log.warning("无法从 webhook 解析 key，跳过附件推送")
        return False
    try:
        with open(path, "rb") as fh:
            resp = requests.post(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media",
                params={"key": key, "type": "file"},
                files={"media": (path.name, fh, "application/octet-stream")},
                timeout=120)
        data = resp.json()
        media_id = data.get("media_id")
        if not media_id:
            log.warning("附件上传失败：%s", json.dumps(data, ensure_ascii=False)[:200])
            return False
        r2 = requests.post(webhook, json={"msgtype": "file", "file": {"media_id": media_id}},
                           timeout=30)
        d2 = r2.json()
        if d2.get("errcode") == 0:
            log.info("报告附件已推送：%s（%.0f KB）", path.name, path.stat().st_size / 1024)
            return True
        log.warning("附件消息发送失败：%s", json.dumps(d2, ensure_ascii=False)[:200])
        return False
    except Exception as exc:
        log.warning("附件推送异常：%s", exc)
        return False


def notify_daily(cfg_notify: dict, result: dict) -> bool:
    """按配置推送日报摘要 + Word 报告附件（失败不影响主流程）。"""
    if not cfg_notify or not cfg_notify.get("enabled", True):
        return False
    hook = str(cfg_notify.get("wecom_webhook") or "").strip()
    if not hook:
        log.debug("未配置推送地址，跳过")
        return False
    if cfg_notify.get("only_if_actions"):
        actions = {d.get("action") for d in (result.get("decisions") or [])}
        tracks = ((result.get("sim") or {}).get("tracks") or {})
        has_trade = any((t.get("entry") or {}).get("trades", 0) > 0 for t in tracks.values())
        if not (actions & {"BUY", "ADD", "REDUCE", "CLEAR"}) and not has_trade:
            log.info("无买卖动作且 only_if_actions=true，跳过推送")
            return False
    ok = send(build_daily_message(result), hook)
    # 附带 Word 报告文件（可在手机上直接打开）
    if cfg_notify.get("attach_report", True) and result.get("report_path"):
        try:
            send_file(hook, result["report_path"])
        except Exception as exc:
            log.warning("附件推送失败（不影响文字推送）: %s", exc)
    return ok
