"""F 组回归：陈旧事件拒绝、暂停不写、人工采纳不被降级、清队列只清前 20。"""
import io
import json
import os
import shutil
import tempfile
import unittest

from engine.self_model import SelfModel


def _cfg(**kw):
    base = {
        "evolution_mode": "rule",
        "evolution_confirm_count": 2,
        "evolution_max_new_beliefs_per_day": 3,
        "evolution_daily_interval_hours": 6,
    }
    base.update(kw)
    return base


class TestFBatch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _cand(self, claim="他难过的时候会来找我"):
        return {"claim": claim, "kind": "trust", "scope": "user:3388708615",
                "evidence": ["我难受"], "confidence": 0.8, "relevance": 0.8,
                "keywords": ["难过", "来找", "我"]}

    # F04：事件之后又变过 → 回滚默认拒绝
    def test_rollback_refuses_when_state_moved_on(self):
        sm = SelfModel(self.d, _cfg())
        c = self._cand()
        sm.apply(c, "accept", auto=True)
        sm.apply(c, "accept", auto=True)          # 第二次事件
        sm.apply(c, "accept", auto=True)          # 第三次事件
        evs = sm.review_events(20)
        # 找第二条 accept 事件回滚（它的 after 是 evidence=2，当前是 3）
        target = [e for e in evs if e.get("decision") == "accept"][-2]
        out = sm.rollback_event(str(target.get("id")))
        self.assertFalse(out.get("ok"), "陈旧事件必须被拒绝")
        self.assertTrue(out.get("conflict"))
        # 加 force 才允许
        out2 = sm.rollback_event(str(target.get("id")), force=True)
        self.assertTrue(out2.get("ok"))

    # F07：暂停时 apply_daily 一条都不写
    def test_apply_daily_noop_when_paused(self):
        sm = SelfModel(self.d, _cfg(evolution_mode="daily"))
        sm.pause()
        n = sm.apply_daily([{"claim": "他在意我", "kind": "trust",
                             "scope": "user:3388708615", "confidence": 0.9}])
        self.assertEqual(n, 0)
        self.assertEqual(sm.review(10), [])

    # F08：人工采纳后，普通支持证据不得把它降回 hold
    def test_manual_accept_not_downgraded(self):
        sm = SelfModel(self.d, _cfg(evolution_confirm_count=5))
        c = self._cand()
        sm.apply(c, "hold", auto=True)
        rid = sm.review(5)[0]["id"]
        sm.approve(rid, "accept")
        sm.apply(c, "accept", auto=True)          # 证据仍不够 5 次
        row = sm.review(5)[0]
        self.assertEqual(row["status"], "accept", "人工裁决被自动证据推翻了")

    # F02：只清这一批前 20 条，队列里多的留着
    def test_clear_pending_only_first_batch(self):
        sm = SelfModel(self.d, _cfg())
        for i in range(25):
            sm.enqueue({"claim": f"事{i}", "kind": "general",
                        "scope": "user:3388708615", "evidence": ["x"], "relevance": 0.9,
                        "keywords": ["事"]})
        rows = sm.pending(max_age_days=2)
        self.assertEqual(len(rows), 25)
        sm.clear_pending([str(r.get("queued_at")) + str(r.get("claim")) for r in rows[:20]])
        left = sm.pending(max_age_days=2)
        self.assertEqual(len(left), 5, "队列里应剩 5 条未提交的")


if __name__ == "__main__":
    unittest.main()
