"""骂 vs 开玩笑：规则分级、模型判断优先、信任只对真骂记账。

单位测试，不连模型也不连 AstrBot。
"""
import unittest

from test_v2_regressions import load_plugin

_, engine, _ = load_plugin()
act = engine.act
ProfileStore = engine.profiles.ProfileStore

PERSONA = {"O": 75, "C": 35, "E": 55, "A": 40, "N": 60}
AFFECT = {"P": 60, "Ar": 45, "D": 55}
RELATION = {"T": 70, "I": 60}


def decide(msg, thought=None, scene="private"):
    return act.decide(
        persona=PERSONA, affect=AFFECT, relation=RELATION, scene=scene,
        msg=msg, thought=thought,
    )


class BotAffectRuleTests(unittest.TestCase):
    def assertAffect(self, msg, want):
        got = decide(msg)["bot_affect"]
        self.assertEqual(got, want, f"{msg} 判成 {got}，应为 {want}")

    def test_plain_insult(self):
        for msg in ("你这破插件真烦人", "闭嘴，别烦我", "你滚", "傻逼东西", "我讨厌你"):
            self.assertAffect(msg, "insult")

    def test_teasing_reads_as_tease(self):
        for msg in (
            "哈哈哈你个笨蛋",
            "你是不是傻啊哈哈哈",
            "笑死，你这鱼真憨",
            "你个小胖子快点",
            "逗你的啦，别当真",
        ):
            self.assertAffect(msg, "tease")

    def test_not_about_her_is_none(self):
        for msg in ("这电影真垃圾", "今天好累啊", "滚来滚去好像挺好玩"):
            self.assertAffect(msg, "none")

    def test_old_hard_words_still_count(self):
        # 老词表里的昵称式骂法，不能因为加了玩笑分支就漏判
        self.assertAffect("你个死肥鱼", "insult")

    def test_tease_does_not_get_an_angry_decision(self):
        d = decide("哈哈哈你个笨蛋")
        self.assertEqual(d["bot_affect"], "tease")
        self.assertIn("tease_bot", d["reason"])
        self.assertNotIn("angry_at_bot", d["reason"])

    def test_insult_keeps_angry_path(self):
        d = decide("你这破插件真烦人")
        self.assertEqual(d["bot_affect"], "insult")
        self.assertIn("angry_at_bot", d["reason"])


class LlmOverrideTests(unittest.TestCase):
    def test_model_can_call_a_tease(self):
        # 规则判成骂，但她自己想明白这是闹着玩
        d = decide("你这破插件真烦人", thought={"bot_affect": "tease"})
        self.assertEqual(d["bot_affect"], "tease")
        self.assertNotIn("angry_at_bot", d["reason"])

    def test_model_can_call_a_real_insult(self):
        d = decide("哈哈哈你个笨蛋", thought={"bot_affect": "insult"})
        self.assertEqual(d["bot_affect"], "insult")
        self.assertIn("angry_at_bot", d["reason"])

    def test_unreadable_model_value_keeps_rule_result(self):
        # 真正认不出的值不能当成 none，把真实的不满吃掉
        for value in ("不知道", "nah", "", "???"):
            d = decide("你这破插件真烦人", thought={"bot_affect": value})
            self.assertEqual(d["bot_affect"], "insult", f"模型给 {value!r} 时不该覆盖规则")


class TrustLedgerTests(unittest.TestCase):
    def test_only_insult_costs_trust(self):
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ProfileStore(Path(tmp.name) / "p", 55, 40)
        base = store.touch("u1", "今天过得还行吧")["T"]

        store.touch("u2", "哈哈哈你个笨蛋")  # 玩笑：不扣，最多算一次自我披露
        self.assertGreaterEqual(store.load("u2")["T"], 55.0)

        store.touch("u3", "你这破插件真烦人", negative=True)  # 真骂：扣
        self.assertLess(store.load("u3")["T"], 55.0)
        self.assertEqual(base, 55.1)


if __name__ == "__main__":
    unittest.main()
