"""自我模型（1.2.0）的测试。

重点守四类坑：
1. 阈值：relevance 不够就不该产生候选（否则等于全收）
2. 合并：同义 claim 必须并成一条（否则 evidence_count 永远到不了 3）
3. 衰减：相反证据要能把强度打下来
4. 开关：off 档必须什么都不写（否则关不掉）
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.self_model import SelfModel, _strength, jaccard, keywords  # noqa: E402


class SelfModelTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sm = SelfModel(self.dir, {"evolution_mode": "rule",
                                        "evolution_max_candidates_per_day": 10,
                                        "evolution_max_new_beliefs_per_day": 10})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _cand(self, want="comfort", rel=0.8, text=None, uid="u1", scene="private"):
        # observe() 有 120 秒内同文本去重（防 QQ 重试造成假证据），
        # 测试要连续产生候选，所以每次换一句等价的话。
        self._n = getattr(self, "_n", 0) + 1
        text = text or f"好累啊（第{self._n}次）"
        return self.sm.observe(
            thought={"want_from_me": want, "doing": "他说他今天很累"},
            text=text, uid=uid, scene=scene, relevance=rel,
        )

    def test_low_relevance_no_candidate(self):
        self.assertIsNone(self._cand(rel=0.1))

    def test_comfort_candidate_shape(self):
        c = self._cand()
        self.assertIsNotNone(c)
        self.assertEqual(c["kind"], "trust")
        self.assertEqual(c["scope"], "user:u1")
        self.assertIn("难受", c["claim"])

    def test_same_claim_merges(self):
        for _ in range(3):
            self.sm.apply(self._cand(), "accept")
        path = os.path.join(self.dir, "beliefs", "u1.jsonl")
        rows = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").strip().splitlines()]
        self.assertEqual(len(rows), 1, "三条同义候选应合并成一条")
        self.assertEqual(rows[0]["evidence_count"], 3)
        self.assertGreater(rows[0]["strength"], 0.6)

    def test_reject_lowers_strength(self):
        # auto=False = 管理员批准，直接 accept（自动路径先 hold 攒确认次数）
        self.sm.apply(self._cand(), "accept", auto=False)
        self.sm.apply(self._cand(), "accept", auto=False)
        before = self.sm.brief("u1")
        self.sm.apply(self._cand(), "reject")
        path = os.path.join(self.dir, "beliefs", "u1.jsonl")
        row = json.loads(Path(path).read_text(encoding="utf-8").strip().splitlines()[0])
        self.assertLess(row["evidence_count"], 2, "相反证据应打折证据数")
        self.assertTrue(before, "拒绝前后都应有信念可读")

    def test_reject_without_match_writes_nothing(self):
        self.sm.apply(self._cand(), "reject")
        path = os.path.join(self.dir, "beliefs", "u1.jsonl")
        self.assertFalse(os.path.exists(path), "没有可匹配的信念时，拒绝不应新建")

    def test_brief_orders_by_strength(self):
        self.sm.apply(self._cand(want="comfort"), "accept", auto=False)
        for _ in range(3):
            self.sm.apply(self._cand(want="praise"), "accept", auto=False)
        out = self.sm.brief("u1", limit=2)
        self.assertTrue(out)
        self.assertIn("夸", out[0]["claim"], "证据更多的信念应排在前面")

    def test_off_mode_writes_nothing(self):
        sm = SelfModel(tempfile.mkdtemp(), {"evolution_mode": "off"})
        self.assertIsNone(
            sm.observe(thought={"want_from_me": "comfort"}, text="x", uid="u",
                       scene="private", relevance=1.0)
        )

    def test_pause_blocks_observe(self):
        self.sm.pause()
        self.assertIsNone(self._cand())
        self.sm.resume()
        self.assertIsNotNone(self._cand())

    def test_default_state_created(self):
        p = os.path.join(self.dir, "self_model.json")
        self.assertTrue(os.path.exists(p), "首次运行应自动生成 self_model.json")
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        self.assertEqual(data["evolution_mode"], "rule")
        self.assertTrue(data["charter"])

    def test_self_scope_goes_to_self_beliefs(self):
        c = self.sm.observe(
            thought={"mood": "很难受"}, text="你就是个废物", uid="u1",
            scene="private", relevance=0.9,
        )
        self.assertIsNotNone(c)
        self.assertEqual(c["scope"], "self")
        self.sm.apply(c, "accept")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "self_beliefs.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "beliefs", "u1.jsonl")))

    def test_snapshot_incremental(self):
        self.sm.apply(self._cand(), "accept")
        self.sm.apply(self._cand(want="praise"), "accept")
        # 增量快照每天一份（full-*.json 是全量备份，不在此列）
        snaps = [f for f in os.listdir(os.path.join(self.dir, "evolution_snapshots"))
                 if not f.startswith("full-")]
        self.assertEqual(len(snaps), 1, "一天内多次变更只应产生一份快照")

    def test_events_audit_grows(self):
        for _ in range(3):
            self.sm.apply(self._cand(), "accept")
        p = os.path.join(self.dir, "evolution_events.jsonl")
        self.assertEqual(len(Path(p).read_text(encoding="utf-8").strip().splitlines()), 3)

    def test_strength_curve(self):
        self.assertAlmostEqual(_strength(1), 0.455, places=2)
        self.assertAlmostEqual(_strength(3), 0.669, places=2)
        self.assertGreater(_strength(10), 0.9)
        self.assertLessEqual(_strength(999), 1.0, "强度封顶为 1")

    def test_keywords_and_jaccard(self):
        a = keywords("他难过的时候会来找我")
        b = keywords("他难过的时候会来找我")
        self.assertEqual(jaccard(a, b), 1.0)
        self.assertGreater(jaccard(a, keywords("他难过的时候会来找我呀")), 0.3)


class ActBeliefsTest(unittest.TestCase):
    def test_decide_accepts_beliefs_without_crash(self):
        from engine.act import decide
        out = decide(
            persona={"O": 75, "C": 35, "E": 55, "A": 40, "N": 60},
            affect={"P": 60, "Ar": 45, "D": 50},
            relation={"T": 80, "I": 75},
            scene="private", msg="我今天好累",
            beliefs=["他难过的时候会来找我", "他夸我的时候是真心的"],
        )
        self.assertIn("acts", out)
        self.assertIn("belief_care", out.get("reason", []) or [])

    def test_decide_dispatches_belief_by_topic(self):
        """按 topic 分派，不靠中文子串（专家建议 A）。"""
        from engine.act import decide
        out = decide(
            persona={"O": 75, "C": 35, "E": 55, "A": 40, "N": 60},
            affect={"P": 60, "Ar": 45, "D": 50},
            relation={"T": 60, "I": 60},
            scene="private", msg="今天上班好烦",
            beliefs=[{"kind": "trust", "topic": "comfort",
                      "claim": "他说过会来找我", "score": 0.7}],
        )
        self.assertIn("belief_care", out.get("reason", []) or [])

    def test_decide_handles_empty_beliefs(self):
        from engine.act import decide
        out = decide(
            persona={}, affect={}, relation={}, scene="private", msg="嗯", beliefs=None,
        )
        self.assertIn("lead", out)


class BeliefLifecycleTest(unittest.TestCase):
    """专家审核后新增的语义测试：hold → accept、单次作废、brief 名额。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sm = SelfModel(self.dir, {"evolution_mode": "rule",
                                       "evolution_confirm_count": 3})
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _cand(self, want="comfort"):
        # 同 observe() 有同文本去重，这里每次换一句等价表达
        self._n = getattr(self, "_n", 0) + 1
        return self.sm.observe(
            thought={"want_from_me": want, "doing": "跟他说话"},
            text=f"我今天好累（第{self._n}次）", uid="u1", scene="private", relevance=0.9,
        )

    def _row(self):
        path = os.path.join(self.dir, "beliefs", "u1.jsonl")
        return json.loads(Path(path).read_text(encoding="utf-8").strip().splitlines()[0])

    def test_new_belief_starts_as_hold(self):
        self.sm.apply(self._cand(), "accept")
        self.assertEqual(self._row()["status"], "hold", "自动路径先记 hold，不立刻注入")
        self.assertEqual(self.sm.brief("u1"), [], "hold 的信念不进注入")

    def test_hold_promotes_after_confirm_count(self):
        for _ in range(3):
            self.sm.apply(self._cand(), "accept")
        self.assertEqual(self._row()["status"], "accept", "累计 3 次确认后转正")
        self.assertTrue(self.sm.brief("u1"), "转正后应能注入")

    def test_reject_single_evidence_drops_belief(self):
        self.sm.apply(self._cand(), "accept", auto=False)
        self.sm.apply(self._cand(), "reject")
        row = self._row()
        self.assertEqual(row["status"], "reject", "只被证实过一次的信念被否决后应作废")
        self.assertEqual(self.sm.brief("u1"), [])

    def test_brief_gives_self_belief_at_most_one_slot(self):
        self.sm.apply(self._cand(), "accept", auto=False)
        self.sm.apply(self._cand(want="praise"), "accept", auto=False)
        # 人工塞一条 self 信念
        self.sm._append({
            "id": "b-self001", "scope": "self", "kind": "boundary",
            "claim": "我不喜欢被这样说", "keywords": ["不喜欢", "这样"],
            "evidence": ["x"], "evidence_count": 5, "strength": 0.95,
            "confidence": 0.8, "status": "accept",
            "created_at": "2026-10-01", "last_confirmed": "2026-10-24",
            "source_scene": "group",
        })
        out = self.sm.brief("u1", limit=2)
        self.assertEqual(len(out), 2)
        claims = [x["claim"] for x in out]
        self.assertIn("我不喜欢被这样说", claims, "self 信念只占一个名额")
        self.assertEqual(sum(1 for x in claims if "不喜欢" in x), 1)


if __name__ == "__main__":
    unittest.main()
