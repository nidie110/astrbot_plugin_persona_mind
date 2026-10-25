"""判读层（want_from_me）接进 act 的回归测试。

守三件事：
1. 加成只作用于仍有资格的 act —— 被关系门槛丢弃的（coy/tease）不能被分数顶回来；
2. 模型写脏的枚举（抄括号、中文、列表、非 dict）不能炸，也不能静默顶掉硬证据；
3. 他骂她时（angry）判读层不接入，猜错了不能反过来哄骂她的人。
"""

import unittest

from engine.act import decide

PERSONA = {"O": 75, "C": 35, "E": 55, "A": 40, "N": 60}
AFFECT = {"P": 55, "Ar": 50, "D": 50}
RELATION = {"T": 50, "I": 55, "L": 50, "Ag": 45}
STRANGER = {"T": 50, "I": 10, "L": 50, "Ag": 45}


def _decide(msg, thought=None, scene="private", relation=None):
    return decide(
        persona=PERSONA,
        affect=AFFECT,
        relation=relation or RELATION,
        scene=scene,
        msg=msg,
        thought=thought,
    )


class TomTest(unittest.TestCase):
    def test_comfort_soft_leads_without_killing_answer(self):
        # 0.30 的增量应该让 care 领跑，但 answer 仍要有竞争力（不是一刀切）
        d = _decide("今天开会开到八点，饭都没吃上", {"want_from_me": "comfort"})
        self.assertEqual(d["lead"][0][0], "care")
        self.assertLess(d["acts"]["care"], 0.6)          # 不是硬改写
        self.assertGreater(d["acts"]["answer"], 0.2)     # 答案仍有戏

    def test_angry_blocks_tom(self):
        # 被骂时判读层不接入：tease 仍然是 lead，且不出现 tom_comfort
        d = _decide("你这胖鱼真烦", {"want_from_me": "comfort"})
        self.assertEqual(d["lead"][0][0], "tease")
        self.assertNotIn("tom_comfort", d["reason"])

    def test_stranger_coy_not_revived_by_praise(self):
        # 生疏关系下 coy 本就被丢弃，判读层不能靠加分把它拉回来
        d = _decide("你好厉害啊", {"want_from_me": "praise"}, relation=STRANGER)
        self.assertEqual(d["acts"]["coy"], 0.0)

    def test_discarded_act_stays_zero(self):
        # command 场景 allowed 收窄为 {answer}，任何加成都不该污染其余 act
        d = _decide("帮我写个脚本", {"want_from_me": "comfort"})
        self.assertEqual(d["lead"][0][0], "answer")
        for k in ("care", "tease", "coy", "dodge", "share"):
            self.assertEqual(d["acts"][k], 0.0, msg=f"{k} 应为 0")

    def test_dirty_values_never_crash(self):
        bad = [
            {"want_from_me": "  COMFORT  "},
            {"want_from_me": "comfort（想被安慰）"},
            {"want_from_me": "想被安慰"},
            {"want_from_me": None},
            {"want_from_me": ["comfort"]},
            {"want_from_me": 123},
            "comfort",          # 整个 thought 不是 dict
            ["comfort"],
            {},
        ]
        for thought in bad:
            with self.subTest(thought=thought):
                d = _decide("今天真累", thought)
                self.assertIn("lead", d)

    def test_listen_changes_hint_not_score(self):
        base = _decide("今天真累")
        d = _decide("今天真累", {"want_from_me": "listen"})
        # 分数不变
        self.assertAlmostEqual(d["acts"]["answer"], base["acts"]["answer"], places=6)
        # 但提示词换成「被听见」而不是「先解决」
        self.assertIn("被听见", d["act_hint"])

    def test_group_none_needs_low_relevance(self):
        # 模板默认值就是 none：高相关时不压 budget，低相关时才装死
        high = _decide("嗯嗯", {"want_from_me": "none", "relevance": 0.8}, scene="group")
        low = _decide("嗯嗯", {"want_from_me": "none", "relevance": 0.1}, scene="group")
        self.assertNotIn("tom_none", high["reason"])
        self.assertIn("tom_none", low["reason"])

    def test_play_opens_tease_eligibility(self):
        # 没命中 play 词时，判读层说「他在闹」应该放开 tease 资格
        d = _decide("今天好无聊", {"want_from_me": "play"})
        self.assertEqual(d["lead"][0][0], "tease")


if __name__ == "__main__":
    unittest.main()
