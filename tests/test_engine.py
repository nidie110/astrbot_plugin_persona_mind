"""引擎单元测试（不依赖 AstrBot，可单独跑）：

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import (  # noqa: E402
    Affect,
    KnowledgeBase,
    ProfileStore,
    VoiceCorpus,
    apply_overrides,
    line_budget,
    parse_json_block,
    scores,
    summary_line,
    thought_to_effects,
    top_two,
)

PERSONA = {"O": 75, "C": 35, "E": 55, "A": 40, "N": 60}


class TestAffect(unittest.TestCase):
    def test_apply_clamped(self):
        a = Affect()
        a.apply({"P": -100, "Ar": 5})
        self.assertGreaterEqual(a.P, 0.0)
        self.assertLessEqual(a.Ar, 100.0)

    def test_decay_towards_base(self):
        a = Affect(P=0.0)
        a.ts -= 3600  # 假装过了一小时
        a.decay(5, 0.2)
        self.assertGreater(a.P, 0.0)
        self.assertLessEqual(a.P, 60.0)


class TestIntent(unittest.TestCase):
    def _score(self, msg, scene="private"):
        return scores(
            persona=PERSONA,
            affect={"P": 60, "Ar": 45, "D": 50},
            relation={"T": 55, "I": 40},
            scene=scene,
            msg=msg,
        )

    def test_distribution(self):
        s = self._score("不想上课")
        self.assertAlmostEqual(sum(s.values()), 1.0, places=2)

    def test_comfort_dominates_on_distress(self):
        s = apply_overrides(self._score("我真的撑不住了"), "我真的撑不住了")
        self.assertEqual(top_two(s)[0][0], "care")

    def test_anger_override(self):
        s = apply_overrides(self._score("大肥鱼！"), "大肥鱼！")
        self.assertGreaterEqual(s["tease"], 0.7)

    def test_question_not_dodged(self):
        s = self._score("这个插件怎么安装啊，有没有文档")
        self.assertLess(s["dodge"], s["probe"])

    def test_line_budget_dodge(self):
        budget = line_budget({"dodge": 0.9, "care": 0.1}, mentioned=False, scene="group")
        self.assertEqual(budget, 0)


class TestAppraisal(unittest.TestCase):
    def test_parse_json_block(self):
        text = '好的，结果如下：{"doing":"上课","emotion":"Worry"} 完毕'
        self.assertEqual(parse_json_block(text)["emotion"], "Worry")

    def test_parse_garbage(self):
        self.assertEqual(parse_json_block("完全不是 JSON"), {})

    def test_effects_scale(self):
        emotion, eff = thought_to_effects({"emotion": "Anger"}, intensity=0.5)
        self.assertEqual(emotion, "Anger")
        self.assertEqual(eff["Ar"], 7.5)


class TestKnowledge(unittest.TestCase):
    def test_query_hits_term(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "a.md").write_text(
                "# 足球\n\n## 术语\n- 越位：进攻球员在传球瞬间比倒数第二名防守球员更靠近球门线。\n",
                encoding="utf-8",
            )
            kb = KnowledgeBase(d)
            self.assertGreater(kb.load(), 0)
            hits = kb.query("越位是什么意思")
            self.assertTrue(hits)

    def test_empty_query(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            kb = KnowledgeBase(Path(td))
            kb.load()
            self.assertEqual(kb.query(""), [])


class TestProfiles(unittest.TestCase):
    def test_merge_and_privacy(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            store = ProfileStore(Path(td))
            prof = store.merge_patch(
                "u1",
                {
                    "身份": "学生",
                    "兴趣": ["足球"],
                    "近况": [{"时间": "10-22", "事": "逃课"}],
                    "note": ["手机号 13800138000"],  # 应被过滤
                },
            )
            self.assertEqual(prof["身份"], "学生")
            self.assertIn("足球", prof["兴趣"])
            self.assertEqual(prof.get("note"), [])

    def test_summary_line(self):
        line = summary_line({"称呼": "球迷", "身份": "学生", "兴趣": ["足球"]})
        self.assertIn("球迷", line)
        self.assertLessEqual(len(line), 150)


class TestVoice(unittest.TestCase):
    def test_pick_by_intent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "voice.md"
            p.write_text("## 关心\n你没事吧？\n累了就歇着\n## 亲近\n过来点\n", encoding="utf-8")
            v = VoiceCorpus(p)
            self.assertEqual(v.count(), 3)
            lines = v.pick("care", 2)
            self.assertTrue(lines)
            self.assertIn(lines[0], ("你没事吧？", "累了就歇着"))


if __name__ == "__main__":
    unittest.main()
