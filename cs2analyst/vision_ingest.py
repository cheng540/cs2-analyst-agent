"""截图 → 持仓候选：用 deepseek-flash 的**原生多模态**读图，再交给确定性校验。

分工（很重要）：
    · 模型只做「把图读成三列文本」——读字、抄数字，不做判断；
    · 名称反查、磨损档检查、总价/数量规则，全部走 `inventory_parse` 的确定性逻辑；
    · 最终仍要人在预览表里确认一遍才写库。

为什么不让模型直接输出 JSON 持仓：
    模型给的是"它以为"，解析器给的是"可复现的规则"。中间隔一层，错在哪一望便知；
    而且模型偶尔会编造磨损档或把总价当单价，这些恰好是解析器会报警的地方。

同一个 key 就能跑：项目的 `llm.model` 默认 `deepseek-flash`（V4.1 Flash），
它本身支持图片输入（content 用 block 数组传 image_url），不需要另配视觉模型。
"""
from __future__ import annotations

import base64
import re
from typing import Any

from .config import AppConfig
from .logging_setup import get_logger
from .inventory_parse import parse_inventory_text

log = get_logger(__name__)

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 6 * 1024 * 1024          # 单图 6MB（base64 前）
_OK_PREFIX = re.compile(r"^data:image/(png|jpe?g|webp|gif);base64,", re.IGNORECASE)

VISION_PROMPT = """你是 CS2 饰品库存截图的转录工具。逐条读出图中的饰品，不要总结、不要解释、不要判断。

输出要求（严格遵守）：
- 每件饰品一行，三列用制表符（Tab）分隔：英文名<TAB>数量<TAB>单价
- 英文名必须是 Steam 官方名并带磨损档，例如：AK-47 | Redline (Field-Tested)
- 数量只写整数；单价只写人民币数字（去掉 ¥ 和千分位逗号）
- 图上没有或看不清的列写 ?，不要猜
- 只输出这些行：不要表头、不要代码块、不要任何说明文字
- 如果图中没有饰品，只输出一行：NONE"""


class VisionError(RuntimeError):
    """识图失败（配置缺失 / 图片不合法 / 模型没读出东西）。"""


def _to_data_url(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise VisionError("图片内容为空")
    if text.startswith("data:"):
        if not _OK_PREFIX.match(text):
            raise VisionError("只支持 PNG / JPEG / WebP / GIF 截图")
        payload = text.split(",", 1)[1] if "," in text else ""
        if not payload:
            raise VisionError("图片数据不完整")
        size = len(payload) * 3 // 4
        if size > MAX_IMAGE_BYTES:
            raise VisionError(f"单张图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB，请压缩后再传")
        return text
    # 纯 base64：按 JPEG 处理
    payload = re.sub(r"\s+", "", text)
    if len(payload) * 3 // 4 > MAX_IMAGE_BYTES:
        raise VisionError(f"单张图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB，请压缩后再传")
    try:
        base64.b64decode(payload, validate=True)
    except Exception:
        raise VisionError("图片 base64 解析失败，请重新截图") from None
    return "data:image/jpeg;base64," + payload


def build_messages(images: list[Any]) -> list[dict]:
    """构造 OpenAI 兼容的多模态消息（content 是 block 数组）。"""
    if not images:
        raise VisionError("没有收到图片")
    if len(images) > MAX_IMAGES:
        raise VisionError(f"一次最多识别 {MAX_IMAGES} 张图")
    blocks: list[dict] = [{"type": "text", "text": VISION_PROMPT}]
    for raw in images:
        blocks.append({"type": "image_url", "image_url": {"url": _to_data_url(raw)}})
    return [{"role": "user", "content": blocks}]


def call_model(cfg: AppConfig, images: list[Any]) -> tuple[str, dict]:
    """调用模型读图。单独抽出来，测试里可以整体替换掉，不花钱。"""
    from .llm.client import LLMClient

    if not cfg.has_llm_key():
        raise VisionError("还没有配置 DeepSeek API Key，无法识图。可以先用「粘贴文本」导入。")
    client = LLMClient(cfg.llm, profile="chat")
    if not client.enabled:
        raise VisionError("DeepSeek 未启用（llm.enabled=false 或 Key 为空）")
    text = client.chat(build_messages(images), temperature=0.1, max_tokens=2000)
    return text, dict(client.last_usage or {})


def _rows_from_reply(reply: str) -> list[str]:
    """把模型输出清洗成「名称 数量 单价」行，供 parse_inventory_text 复用。"""
    text = re.sub(r"```[a-zA-Z]*|```", "", str(reply or "")).strip()
    if not text or text.strip().upper() == "NONE":
        return []
    rows: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) >= 2:
            name = cols[0]
            extras = []
            for col in cols[1:3]:
                if col and col not in ("?", "-", "—", "无"):
                    extras.append(col)
            rows.append(" ".join([name] + extras) if name else "")
        else:
            rows.append(line)
    return [r for r in rows if r]


def images_to_items(cfg: AppConfig, images: list[Any], lookup: dict[str, str]) -> dict:
    """截图列表 → 候选条目（结构与粘贴解析完全一致，前端可以复用同一张预览表）。"""
    reply, usage = call_model(cfg, images)
    rows = _rows_from_reply(reply)
    if not rows:
        raise VisionError("模型说图里没有识别到饰品。换一张更清晰的库存截图试试，"
                          "或者改用「粘贴文本」导入。")

    result = parse_inventory_text("\n".join(rows), lookup)
    if not result["items"]:
        raise VisionError("从模型输出里没能解析出条目，换张图或改用粘贴导入。")

    result["source"] = "vision"
    result["vision_raw"] = str(reply)[:2000]
    result["usage"] = usage
    result["note"] = ("这是**模型读图**的结果，比粘贴文本更容易看错（磨损档、总价尤其容易错）。"
                      "请逐条核对英文名、数量、单价后再导入。")
    return result
