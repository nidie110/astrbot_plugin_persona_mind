import json
import tempfile
import unittest
from pathlib import Path

from engine.claims import ClaimStore
from engine.diary import Diary
from tools.import_whale_diary import parse_rows


class ClaimsDiaryTests(unittest.TestCase):
    def test_diary_parser_keeps_only_valid_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            diary = Diary(Path(temp), Path(temp))
            raw = json.dumps({
                "mood": 55,
                "tags": ["猫咪"],
                "title": "测试",
                "claims": [
                    {"title": "猫咪地铁站", "stance": "确实搭了地铁站，值得再看"},
                    {"title": "", "stance": "丢弃"},
                    {"title": "第三条", "stance": "不应超过两条"},
                ],
                "text": "这是一段足够长的日记正文，用来验证 JSON 里的判断会被保留下来。",
            }, ensure_ascii=False)
            parsed = diary.parse_reply(raw)
            self.assertEqual(len(parsed["claims"]), 2)
            self.assertEqual(parsed["claims"][0]["title"], "猫咪地铁站")

    def test_claim_store_deduplicates_and_handles_bad_confidence(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ClaimStore(Path(temp) / "claims.jsonl")
            kwargs = dict(surface="diary", source_scene="diary", source="小鲸鱼",
                          date="2026-10-23", confidence="bad")
            self.assertTrue(store.add("猫咪地铁站", "值得再看", **kwargs))
            self.assertFalse(store.add("猫咪地铁站", "值得再看", **kwargs))
            self.assertEqual(store.stats()["claims"], 1)

    def test_material_update_replaces_old_judgment(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ClaimStore(Path(temp) / "claims.jsonl")
            self.assertTrue(store.add("猫咪地铁站", "先看完就算了", surface="diary",
                                      source="小鲸鱼", source_scene="diary",
                                      material_id="whale:BVabc123"))
            self.assertTrue(store.add("猫咪地铁站", "确实搭了地铁站，值得再看", surface="diary",
                                      source="小鲸鱼", source_scene="diary",
                                      material_id="whale:BVabc123"))
            self.assertEqual(store.stats()["claims"], 1)
            store.load()
            self.assertEqual(store.recent()[0]["stance"], "确实搭了地铁站，值得再看")

    def test_private_claim_never_enters_query_or_recent(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ClaimStore(Path(temp) / "claims.jsonl")
            self.assertTrue(store.add("私密话题", "不能带进 QQ", surface="diary", visibility="private"))
            self.assertTrue(store.add("公开话题", "可以共享", surface="diary", visibility="shared"))
            store.load()
            self.assertEqual(store.query("私密话题"), [])
            self.assertEqual([x["title"] for x in store.recent()], ["公开话题"])

    def test_visibility_change_can_publish_same_material_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ClaimStore(Path(temp) / "claims.jsonl")
            kwargs = dict(surface="diary", source_scene="diary", source="小鲸鱼",
                          material_id="whale:private-to-shared")
            self.assertTrue(store.add("可公开话题", "先私下记着", visibility="private", **kwargs))
            self.assertTrue(store.add("可公开话题", "现在可以共享", visibility="shared", **kwargs))
            self.assertEqual([x["stance"] for x in store.recent()], ["现在可以共享"])

    def test_cross_surface_line_keeps_origin(self):
        rec = {
            "title": "猫咪地铁站", "stance": "值得再看", "experience": "watched",
            "source_scene": "diary", "same_surface": False,
        }
        self.assertIn("日记那边看过", ClaimStore.to_line(rec))

    def test_whale_claim_line_says_heard(self):
        rec = {
            "title": "猫咪地铁站", "stance": "值得再看", "experience": "heard",
            "source_scene": "diary", "source": "小鲸鱼", "same_surface": False,
        }
        self.assertIn("听小鲸鱼说过", ClaimStore.to_line(rec))

    def test_import_parser_keeps_bv_and_latest_stance(self):
        text = """## 2026-10-23
| 时间 | BV | 标题 | 想法 |
| --- | --- | --- | --- |
| 晚上 | BVabc123 | 猫咪地铁站 | 之前低估了，值得再看 |
"""
        rows = parse_rows(text)
        self.assertEqual(rows[0]["bv"], "BVabc123")
        self.assertEqual(rows[0]["title"], "猫咪地铁站")


if __name__ == "__main__":
    unittest.main()
