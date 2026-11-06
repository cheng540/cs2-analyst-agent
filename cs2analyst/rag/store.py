"""索引的落盘、加载与检索入口。

索引是 `data/rag/index.json`（属个人数据，已在 .gitignore 内）。
文档量级只有几百篇，加载 + 建 BM25 是毫秒级，所以不搞常驻内存服务，
只在进程内按 mtime 缓存一份。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .bm25 import BM25, snippet, tokenize
from .corpus import build_documents, stats

INDEX_VERSION = 1
DEFAULT_INDEX_REL = "data/rag/index.json"
MAX_QUERY_CHARS = 200

# 「上次类似形态怎么判断的」这类提问的关键词。命中但没捞到案例时，
# 退化为「最近几条案例」并**明确标注不是同一形态**，不能让它看起来像精确匹配。
CASE_INTENT = ("上次", "以前", "历史", "类似", "案例", "复盘", "怎么判断",
               "当时", "后来", "经验", "曾经")

_CACHE: dict[str, tuple[float, "KnowledgeIndex"]] = {}
_CACHE_LOCK = threading.Lock()


class KnowledgeIndex:
    """只读索引：一次性建好 BM25，查询时打分取 top-k。"""

    def __init__(self, docs: list[dict]):
        self.docs = docs
        # 标题 + 关键词各重复一次 = 权重更高（"纪念品""T+7"这类靠标题/别名命中）
        self.bm25 = BM25([
            tokenize(" ".join([
                d.get("title", ""), d.get("title", ""),
                d.get("keywords", ""), d.get("keywords", ""),
                d.get("text", ""),
            ])) for d in docs
        ])

    def __len__(self) -> int:
        return len(self.docs)

    def search(self, query: str, k: int = 5, kind: str | None = None) -> list[dict]:
        query = str(query or "").strip()[:MAX_QUERY_CHARS]
        if not query or not self.docs:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        hits = self.bm25.top_k(tokens, k=max(k * 4, k))
        out: list[dict] = []
        for idx, score in hits:
            doc = self.docs[idx]
            if kind and doc["kind"] != kind:
                continue
            out.append({
                "id": doc["id"],
                "kind": doc["kind"],
                "title": doc["title"],
                "date": doc.get("date") or "",
                "source": doc.get("source") or "",
                "score": round(float(score), 3),
                "snippet": snippet(doc["text"], tokens),
                "text": doc["text"],
            })
            if len(out) >= k:
                break
        return out

    def recent(self, kind: str, n: int = 2) -> list[dict]:
        """按日期倒序取某类文档（用于「上次类似形态」没命中时的兜底）。"""
        picked = [d for d in self.docs if d["kind"] == kind]
        picked.sort(key=lambda d: d.get("date") or "", reverse=True)
        return [{
            "id": d["id"],
            "kind": d["kind"],
            "title": d["title"],
            "date": d.get("date") or "",
            "source": d.get("source") or "",
            "score": 0.0,
            "snippet": d["text"][:180],
            "text": d["text"],
        } for d in picked[:n]]


def index_path(root: Path, override: str | Path | None = None) -> Path:
    if override:
        p = Path(override)
        return p if p.is_absolute() else Path(root) / p
    return Path(root) / DEFAULT_INDEX_REL


def build_index(root: Path, cfg_sim: dict | None = None,
                out_path: str | Path | None = None) -> dict:
    """重建索引并落盘，返回统计信息（给 CLI 和前端显示）。"""
    root = Path(root)
    docs = build_documents(root, cfg_sim)
    target = index_path(root, out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": INDEX_VERSION,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "docs": docs,
    }
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(target)
    with _CACHE_LOCK:
        _CACHE.pop(str(target), None)
    info = stats(docs)
    info.update({"index": str(target), "built_at": payload["built_at"]})
    return info


def load_index(root: Path, out_path: str | Path | None = None,
               auto_build: bool = True, cfg_sim: dict | None = None) -> KnowledgeIndex:
    """加载索引；文件不存在且 `auto_build` 时自动构建一次。"""
    root = Path(root)
    target = index_path(root, out_path)
    if not target.exists():
        if not auto_build:
            return KnowledgeIndex([])
        build_index(root, cfg_sim, out_path)
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return KnowledgeIndex([])
    key = str(target)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        docs = payload.get("docs") or []
    except (OSError, json.JSONDecodeError):
        docs = []
    index = KnowledgeIndex(docs)
    with _CACHE_LOCK:
        _CACHE[key] = (mtime, index)
    return index


def search_knowledge(root: Path, query: str, k: int = 5, kind: str | None = None,
                     cfg_sim: dict | None = None) -> dict:
    """给问答工具用的入口：永远返回可 JSON 化结构，不抛异常。"""
    query = str(query or "").strip()[:MAX_QUERY_CHARS]
    if not query:
        return {"error": "检索词为空"}
    index = load_index(root, cfg_sim=cfg_sim)
    if not len(index):
        return {"query": query, "results": [],
                "note": "经验库为空（还没有构建索引）。可以先运行：python tools/build_rag_index.py"}
    results = index.search(query, k=k, kind=kind)
    payload: dict = {
        "query": query,
        "results": [{kk: vv for kk, vv in r.items() if kk != "text"} for r in results],
        "note": ("以上是经验库检索结果，只用于解释「以前类似情况怎么判断、后来怎么走」、"
                 "平台规则、饰品常识与官方公告。**当前价格与持仓数字必须用其他工具查**，"
                 "不得从这些片段里取数。"),
    }
    if not kind and any(word in query for word in CASE_INTENT) \
            and not any(r["kind"] == "case" for r in results):
        extra = index.recent("case", max(1, min(2, k)))
        if extra:
            keep = results[: max(0, k - len(extra))]
            payload["results"] = [
                {kk: vv for kk, vv in r.items() if kk != "text"} for r in keep + extra
            ]
            payload["fallback_note"] = (
                "没有匹配到具体形态的历史案例，上面最后几条是**最近的案例**，"
                "不是同类形态，只能当背景参考；回答时要说明这一点，不要当成精确匹配。")
    return payload
