"""BM25 检索 + 中文 bigram 分词。

为什么不用向量库（2026-10 决策）：服务器是 2C2G，DeepSeek 没有 embedding 接口，
本地跑 embedding 模型在 2G 内存上很勉强。语料量级只有「几百篇」，BM25 足够，
而且纯 Python、零依赖、索引可读可 diff。

中文分词没有用 jieba：多一个依赖、要多带一份词典。这里用 bigram + 单字：
    "跌破支撑" → 跌破 / 破支 / 支撑 / 跌 / 破 / 支 / 撑
召回够用，缺点是会带来一点噪声——所以 BM25 的 IDF 会自然压低高频组合的权重。
"""
from __future__ import annotations

import math
import re
from collections import Counter

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.+]*")

K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    """中英混排文本 → token 列表（小写、去标点）。"""
    text = str(text or "")
    if not text:
        return []
    tokens: list[str] = []
    for m in _CJK_RUN.finditer(text):
        run = m.group()
        if len(run) == 1:
            tokens.append(run)
            continue
        # bigram：主特征
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        # 单字：兜底召回（"刀""箱"这类单字查询）
        tokens.extend(run)
    for m in _WORD.finditer(text):
        tok = m.group().lower()
        if len(tok) > 1 or tok.isdigit():
            tokens.append(tok)
    return tokens


class BM25:
    """经典 Okapi BM25，语料量几百篇时内存占用可忽略。"""

    def __init__(self, docs_tokens: list[list[str]]):
        self.n_docs = len(docs_tokens)
        self.tfs = [Counter(toks) for toks in docs_tokens]
        self.lengths = [len(toks) for toks in docs_tokens]
        total = sum(self.lengths)
        self.avg_len = (total / self.n_docs) if self.n_docs else 1.0
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        self.idf = {
            term: math.log(1 + (self.n_docs - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def scores(self, query_tokens: list[str]) -> list[float]:
        q = [t for t in query_tokens if t in self.idf]
        out = [0.0] * self.n_docs
        if not q:
            return out
        for idx, tf in enumerate(self.tfs):
            length = self.lengths[idx] or 1
            score = 0.0
            for term in q:
                freq = tf.get(term)
                if not freq:
                    continue
                denom = freq + K1 * (1 - B + B * length / self.avg_len)
                score += self.idf[term] * freq * (K1 + 1) / denom
            out[idx] = score
        return out

    def top_k(self, query_tokens: list[str], k: int = 5) -> list[tuple[int, float]]:
        scored = list(enumerate(self.scores(query_tokens)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(i, s) for i, s in scored[:k] if s > 0]


def snippet(text: str, query_tokens: list[str], width: int = 180) -> str:
    """取最能对上查询的一段做摘要（按标点切句后取命中 token 最多的窗口）。"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= width:
        return text
    parts = re.split(r"(?<=[。；！？\n])", text)
    best, best_hit = "", -1
    window = ""
    for part in parts:
        window = (window + part)[-width:] if len(window) + len(part) > width else window + part
        hit = sum(1 for t in set(query_tokens) if t and t in window)
        if hit > best_hit:
            best, best_hit = window, hit
    return (best or text[:width]).strip()
