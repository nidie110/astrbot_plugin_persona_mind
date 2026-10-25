# -*- coding: utf-8 -*-
"""端到端：core 跨场景耦合，scene 按会话隔离。"""
import types
import unittest

from test_v2_regressions import load_plugin

plugin, engine, act = load_plugin()
Mood = engine.Mood

POS = {"user_mood": 70, "mood": "开心", "emotion": "Joy",
       "relevance": 1.0, "desirability": 1.0, "control": 1.0}
NEG = {"user_mood": 10, "mood": "低落", "emotion": "Anger",
       "relevance": 1.0, "desirability": -1.0, "control": 0.0}


class MoodE2E(unittest.IsolatedAsyncioTestCase):
    def make(self, appraisal):
        mind = object.__new__(plugin.MindEngine)
        mind.cfg = dict(plugin.DEFAULTS, field_enable=False, kb_enable=False, voice_enable=False,
                        profile_enable=False, summarize_every=0)
        mind.mood = Mood()
        mind.profiles = types.SimpleNamespace(
            load=lambda uid: {"T": 55, "I": 40, "count": 1, "recent": []},
            touch=lambda *a, **k: None)
        mind._inflight = set()
        mind._dodge_streaks = {}
        mind.last_decision = {}
        mind.bridge = types.SimpleNamespace(get=lambda *a, **k: {}, available=lambda: False)
        mind._save_state = lambda: None
        mind._log = lambda x: None

        async def think(*a, **k):
            return dict(appraisal)
        mind._think = think
        return mind

    async def say(self, mind, umo, text):
        ev = types.SimpleNamespace(message_str=text, unified_msg_origin=umo,
                                   get_sender_id=lambda: "u1")
        req = types.SimpleNamespace(system_prompt="BASE")
        await mind.before_llm(ev, req)
        return mind.mood.effective(umo)

    async def test_core_couples_but_scene_stays_local(self):
        A = "aiocqhttp:GroupMessage:111"
        B = "aiocqhttp:GroupMessage:999"

        good = self.make(POS)
        await self.say(good, A, "你今天真棒")
        await self.say(good, B, "你今天真棒")
        # 被夸过之后，两处都在基线之上（core 抬起来了）
        self.assertGreater(good.mood.effective(A)["P"], 60.0)
        self.assertGreater(good.mood.effective(B)["P"], 60.0)

        # 换一个只对 A 群发脾气的场景
        bad = self.make(NEG)
        bad.mood.load_state(good.mood.dump_state())
        b_before = bad.mood.effective(B)["P"]
        for _ in range(3):
            a_after = await self.say(bad, A, "你个废物")
        b_after_val = bad.mood.effective(B)["P"]

        # A 群被连续冲量打下去，B 群几乎不动（只随 core 缓慢下沉）
        self.assertLess(a_after["P"], b_after_val - 6.0)
        # B 群只受 core 传导，掉幅应远小于 A 群的场景直击
        self.assertLess(abs(b_after_val - b_before), 7.0)
        # 两个场景确实各自存了状态
        self.assertEqual(len(bad.mood.scenes), 2)


if __name__ == "__main__":
    unittest.main()
