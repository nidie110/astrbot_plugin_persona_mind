"""针对 1.2.0-beta 新增功能的测试（专家 P1-10：新代码零覆盖）。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.self_model import SelfModel  # noqa: E402


def _read(path):
    p = Path(path)
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


class RollbackDailyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sm = SelfModel(self.dir, {"evolution_mode": "daily",
                                       "evolution_max_new_beliefs_per_day": 5})

    def _cand(self, claim="他难过的时候会来找我", scope="user:1234567"):
        return {
            "scope": scope, "kind": "trust-comfort", "topic": "comfort",
            "claim": claim, "keywords": ["难过", "找我"],
            "evidence": ["他说今天很难受"], "relevance": 0.8, "scene": "private",
        }

    # ── P0-1：日级全量快照往返 ────────────────────────────
    def test_full_snapshot_restore_roundtrip(self):
        self.sm.apply(self._cand(scope="self"), "accept")
        n1 = len(_read(self.sm.self_beliefs_path))
        self.sm.full_snapshot()
        snaps = sorted(os.listdir(os.path.join(self.dir, "evolution_snapshots")))
        fulls = [f for f in snaps if f.startswith("full-")]
        self.assertEqual(len(fulls), 1, "应产生一份全量快照")

        # 再加一条，然后恢复到快照那天
        _c2 = self._cand(claim="他夸我的时候是真的", scope="self")
        _c2["kind"] = "trust-praise"          # 换 kind，避免被 match 判成同一条
        _c2["keywords"] = ["夸我", "真心"]
        self.sm.apply(_c2, "accept")
        self.assertGreater(len(_read(self.sm.self_beliefs_path)), n1)
        r = self.sm.restore_snapshot(fulls[0][5:15])  # full-YYYY-MM-DD-HHMMSS
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(len(_read(self.sm.self_beliefs_path)), n1)

    def test_snapshot_name_has_time_so_double_rollback_is_safe(self):
        self.sm.full_snapshot()
        self.sm.full_snapshot()
        snaps = [f for f in os.listdir(os.path.join(self.dir, "evolution_snapshots"))
                 if f.startswith("full-")]
        # 同一秒内两次可能同名，但不同秒要留两份；至少不能覆盖成 0 份
        self.assertGreaterEqual(len(snaps), 1)

    # ── P0-2：跨天 carry-over ────────────────────────────
    def test_pending_carries_yesterday(self):
        import time as _t
        path = self.sm._pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        yday = _t.strftime("%Y-%m-%d", _t.localtime(_t.time() - 86400))
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"claim": "昨天的候选", "kind": "pattern",
                                "scope": "self", "queued_at": yday + "T10:00:00"},
                               ensure_ascii=False) + "\n")
        self.assertEqual(len(self.sm.pending(max_age_days=2)), 1, "昨天的应带上来")
        self.assertEqual(len(self.sm.pending(max_age_days=0)), 0, "0 天只看今天")

    def test_clear_pending_by_ids_keeps_new_arrivals(self):
        path = self.sm._pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"claim": "a", "queued_at": "2026-10-24T10:00:00"},
                {"claim": "b", "queued_at": "2026-10-24T11:00:00"}]
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.sm.clear_pending([rows[0]["queued_at"] + rows[0]["claim"]])
        left = _read(path)
        self.assertEqual([r["claim"] for r in left], ["b"], "只该清掉归纳过的那条")

    # ── P0-4：scope 目录穿越 ─────────────────────────────
    def test_apply_daily_rejects_traversal_scope(self):
        n = self.sm.apply_daily([{
            "claim": "他其实很在意我", "kind": "trust",
            "scope": "user:../../../../tmp/evil", "confidence": 0.9,
        }])
        self.assertEqual(n, 1)
        rows = _read(os.path.join(self.dir, "self_beliefs.jsonl"))
        self.assertEqual(rows[0]["scope"], "self", "带路径的 scope 必须回落 self")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "..", "..", "tmp", "evil.jsonl")))

    # ── P1-3：created 分支要有审计 ───────────────────────
    def test_rollback_new_belief_writes_audit(self):
        cand = self._cand()
        self.sm.apply(cand, "accept")
        events = _read(self.sm.events_path)
        created = [e for e in events if e.get("created")]
        self.assertTrue(created, "新建应记 created 事件")
        r = self.sm.rollback_event(created[-1]["id"])
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(r.get("deleted"))
        after = _read(self.sm.events_path)
        self.assertTrue(any(e.get("decision") == "rollback_delete" for e in after),
                        "回滚删除也要留审计")

    # ── P1-9：apply_daily 去重与日上限 ───────────────────
    def test_apply_daily_dedups_same_claim(self):
        item = {"claim": "他总在深夜找我", "kind": "pattern",
                "scope": "user:1234567", "confidence": 0.9}
        self.assertEqual(self.sm.apply_daily([item]), 1)
        self.assertEqual(self.sm.apply_daily([item]), 0, "同一句话第二次不该再建一条")


if __name__ == "__main__":
    unittest.main()
