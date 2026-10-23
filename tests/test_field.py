# -*- coding: utf-8 -*-
"""势场基线与换人迁移的回归。"""
import types
import unittest

from test_v2_regressions import load_plugin

plugin, engine, act = load_plugin()
Mood = engine.Mood
base_from_field = engine.base_from_field


class FieldTests(unittest.TestCase):
    def test_base_follows_relation(self):
        stranger = base_from_field(50, 50, 50, 50)
        liked = base_from_field(85, 70, 40, 80)
        hostile = base_from_field(35, 30, 80, 25)
        self.assertGreater(liked["P"], stranger["P"])
        self.assertLess(hostile["P"], stranger["P"])
        # 攻击性高 → 底气足
        self.assertGreater(hostile["D"], base_from_field(35, 30, 20, 25)["D"])

    def test_strength_zero_is_neutral(self):
        a = base_from_field(90, 90, 90, 90, strength=0.0)
        b = base_from_field(10, 10, 10, 10, strength=0.0)
        self.assertEqual(a, b)

    def test_rebase_keeps_offset(self):
        m = Mood()
        m.rebase(base_from_field(85, 70, 40, 80))
        m.apply({"P": 8}, umo="p2p:u1")
        off_before = m.core.P - m.core.base["P"]
        m.rebase(base_from_field(35, 30, 80, 25))
        off_after = m.core.P - m.core.base["P"]
        self.assertAlmostEqual(off_before, off_after, places=1)
        # 基线本身确实换了
        self.assertLess(m.core.base["P"], 45.0)


class FieldHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_values_move_the_baseline(self):
        mind = object.__new__(plugin.MindEngine)
        mind.cfg = dict(plugin.DEFAULTS, kb_enable=False, voice_enable=False,
                        profile_enable=False, summarize_every=0, field_enable=True)
        mind.mood = Mood()
        mind.profiles = types.SimpleNamespace(
            load=lambda uid: {"T": 80, "I": 60, "count": 3, "recent": []},
            touch=lambda *a, **k: None)
        mind._inflight = set()
        mind._dodge_streaks = {}
        mind.last_decision = {}
        # 高好感 / 高信任
        mind.bridge = types.SimpleNamespace(
            get=lambda *a, **k: {"affection": 90, "libido": 70, "aggression": 30},
            available=lambda: True)
        mind._save_state = lambda: None
        mind._log = lambda x: None

        async def think(*a, **k):
            return {"user_mood": 50, "mood": "平静", "emotion": "None"}
        mind._think = think

        ev = types.SimpleNamespace(message_str="在吗", unified_msg_origin="t:FriendMessage:u1",
                                   get_sender_id=lambda: "u1")
        await mind.before_llm(ev, types.SimpleNamespace(system_prompt="BASE"))

        # 好感 90 / 信任 80 → 基线应该明显高于中性
        self.assertGreater(mind.mood.core.base["P"], 55.0)


if __name__ == "__main__":
    unittest.main()
