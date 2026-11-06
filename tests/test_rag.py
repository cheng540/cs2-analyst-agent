"""经验库（RAG）测试：分词、排序、语料边界、检索入口。

重点不是"能跑"，而是锁住两条设计红线：
1. 检索结果里**不能出现当前持仓/价格数字**（那必须走取数工具）；
2. 「上次类似形态」这类提问要能返回带日期与出处的具体案例。
"""
import json
import tempfile
import unittest
from pathlib import Path

from cs2analyst.rag.bm25 import BM25, snippet, tokenize
from cs2analyst.rag.corpus import build_documents, knowledge_docs, case_docs
from cs2analyst.rag.store import (
    KnowledgeIndex,
    build_index,
    load_index,
    search_knowledge,
)

PROJECT = Path(__file__).resolve().parent.parent


class TestTokenizer(unittest.TestCase):
    def test_chinese_bigram(self):
        toks = tokenize("跌破支撑")
        self.assertIn("跌破", toks)
        self.assertIn("支撑", toks)
        self.assertIn("破支", toks)

    def test_single_char_run(self):
        self.assertEqual(tokenize("刀"), ["刀"])

    def test_english_and_numbers(self):
        toks = tokenize("T+7 fee 1%")
        self.assertIn("t+7", toks)
        self.assertIn("fee", toks)

    def test_empty(self):
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize(None), [])


class TestBM25(unittest.TestCase):
    def test_ranks_relevant_document_first(self):
        docs = [tokenize(t) for t in [
            "纪念品来自 Major 赛事纪念包，自带金色贴纸",
            "平台卖出收百分之一手续费，提现再收百分之一",
            "均线多头排列说明上升趋势",
        ]]
        bm25 = BM25(docs)
        hits = bm25.top_k(tokenize("纪念品 贴纸"), k=3)
        self.assertEqual(hits[0][0], 0)

    def test_no_overlap_returns_nothing(self):
        bm25 = BM25([tokenize("纪念品贴纸"), tokenize("手续费")])
        self.assertEqual(bm25.top_k(tokenize("完全不相关的词"), k=2), [])

    def test_empty_corpus(self):
        bm25 = BM25([])
        self.assertEqual(bm25.top_k(tokenize("纪念品"), k=3), [])

    def test_snippet_short_text_unchanged(self):
        self.assertEqual(snippet("短文本", tokenize("短")), "短文本")


class TestCorpus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel: str, content) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    def test_knowledge_split_by_section(self):
        self._write("knowledge/a.md", "# 标题甲\n\n总览文字\n\n## 小节一\n\n内容一\n\n## 小节二\n\n内容二\n")
        docs = knowledge_docs(self.root)
        titles = [d["title"] for d in docs]
        self.assertIn("标题甲 · 小节一", titles)
        self.assertIn("标题甲 · 小节二", titles)
        self.assertIn("标题甲（总览）", titles)

    def test_case_card_contains_followup_move(self):
        """案例卡片必须带「之后实际走势」，否则经验库没有意义。"""
        self._write("data/analysis/2026-01-02.json", {
            "date": "2026-01-02",
            "market": {"趋势": "下降趋势", "风险等级": "高风险", "大盘指数": 700.0},
            "items": [{
                "饰品": "Test | Item (Factory New)", "中文名": "测试饰品",
                "现价": 100.0, "趋势": "下降趋势", "位置": "低位（5%）",
                "量价": "缩量", "流动性": "高", "系统建议": "🟡 持有观望",
                "信心度": 2, "最近支撑": 95.0, "最近压力": 110.0, "理由": ["低位"],
            }],
        })
        # K 线：买入基准日之后 7 天跌到 90
        rows = [
            [1767312000, 100, 100, 101, 99],     # 2026-01-02
            [1767484800, 100, 96, 100, 95],
            [1767916800, 96, 90, 97, 89],        # 约 7 天后
        ]
        self._write("data/items/Test___Item__Factory_New_/kline_2.json", rows)
        docs = case_docs(self.root)
        self.assertEqual(len(docs), 1)
        text = docs[0]["text"]
        self.assertIn("之后实际走势", text)
        self.assertIn("7 日后", text)
        self.assertIn("复核结论", text)

    def test_build_documents_offline_ok(self):
        """缺数据来源时不能崩，只跳过（规则文本是常量，永远有）。"""
        kinds = {d["kind"] for d in build_documents(self.root)}
        self.assertEqual(kinds, {"rules"})


class TestRealCorpus(unittest.TestCase):
    """在真实项目目录上跑：语料存在、且不含持仓数字。"""

    @classmethod
    def setUpClass(cls):
        cls.docs = build_documents(PROJECT)

    def test_has_documents(self):
        self.assertGreater(len(self.docs), 20)

    def test_has_rules_and_knowledge(self):
        kinds = {d["kind"] for d in self.docs}
        self.assertIn("rules", kinds)
        self.assertIn("knowledge", kinds)

    def test_no_holdings_numbers_in_corpus(self):
        """红线：持仓与盈亏数字不进检索库（文字里提到「成本」概念不算）。"""
        for doc in self.docs:
            self.assertNotIn("config/inventory.json", doc["source"])
            self.assertNotIn("real_trades.json", doc["source"])
            # 真实组合的合计数字不能出现在任何片段里
            self.assertNotIn("1090.0", doc["text"])
            self.assertNotIn("¥1,090", doc["text"])

    def test_sources_are_known(self):
        allowed = ("knowledge/", "cs2analyst/rules.py", "data/intel/", "data/analysis/",
                   "data/llm/", "data/journal/")
        odd = [d["source"] for d in self.docs
               if not str(d["source"]).startswith(allowed)]
        self.assertEqual(odd, [])


class TestSearchEntry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._write("knowledge/k.md", "# 饰品常识\n\n## 纪念品\n\n纪念品带金色贴纸，和普通版不是同一物品。\n\n"
                                      "## 费率\n\n卖出收 1%，提现再收 1%。\n")
        self._write("data/analysis/2026-02-01.json", {
            "date": "2026-02-01", "market": {},
            "items": [{"饰品": "X", "中文名": "测试", "现价": 50.0,
                       "系统建议": "🔴 清仓", "理由": ["破位"]}],
        })
        self._write("data/llm/2026-02-01.json", {"date": "2026-02-01",
                                                 "market_view": "大盘继续下跌"})
        build_index(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str)
                        else json.dumps(content, ensure_ascii=False), encoding="utf-8")

    def test_search_hits_knowledge(self):
        result = search_knowledge(self.root, "纪念品和普通版区别")
        titles = [r["title"] for r in result["results"]]
        self.assertTrue(any("纪念品" in t for t in titles))
        self.assertIn("note", result)

    def test_empty_query(self):
        self.assertIn("error", search_knowledge(self.root, "   "))

    def test_price_question_is_not_answered_from_corpus(self):
        """问价格：可以没有结果，但必须带「数字走工具」的说明。"""
        result = search_knowledge(self.root, "海豹蛙人现在多少钱")
        self.assertIn("其他工具", result["note"])
        for hit in result["results"]:
            self.assertNotIn("现价", hit["snippet"])

    def test_recent_fallback_for_vague_question(self):
        result = search_knowledge(self.root, "上次类似形态我们怎么判断的")
        kinds = [r["kind"] for r in result["results"]]
        self.assertIn("case", kinds)

    def test_index_roundtrip(self):
        index = load_index(self.root, auto_build=False)
        self.assertGreater(len(index), 0)
        again = load_index(self.root)
        self.assertEqual(len(again), len(index))

    def test_kind_filter(self):
        index = KnowledgeIndex(build_documents(self.root))
        self.assertEqual(index.search("规则", k=5, kind="intel"), [])
        self.assertTrue(index.search("纪念品", k=5, kind="knowledge"))


if __name__ == "__main__":
    unittest.main()
