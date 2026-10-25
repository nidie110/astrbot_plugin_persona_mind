"""亲昵语境的回归测试。

2026-10-24 实测：对方全程「宝宝 / 乖 / 可爱鱼」，COY_WORDS 一个都没命中，
coy 永远进不了候选，她只能从 answer/tease 里选，表现成逐句纠正用词的"语文老师"。
这六条是那时真实对话的摘录，用来锁住"别再变回直女"。
"""

import unittest

from engine.act import decide

PERSONA = {"O": 75, "C": 35, "E": 55, "A": 40, "N": 60}
AFFECT = {"P": 55, "Ar": 50, "D": 50}
NEAR = {"T": 60, "I": 60, "L": 50, "Ag": 45}
STRANGER = {"T": 50, "I": 10, "L": 50, "Ag": 45}


def _decide(msg, relation=None):
    return decide(
        persona=PERSONA, affect=AFFECT, relation=relation or NEAR,
        scene="private", msg=msg,
    )


class IntimateTest(unittest.TestCase):
    def test_praise_about_appearance(self):
        d = _decide("再肥也是可爱鱼")
        self.assertEqual(d["lead"][0][0], "coy")

    def test_baby_talk(self):
        d = _decide("好的乖宝宝鱼")
        self.assertEqual(d["lead"][0][0], "coy")

    def test_greeting_with_endearment(self):
        d = _decide("宝宝今天怎么样")
        self.assertEqual(d["lead"][0][0], "coy")

    def test_coy_hint_carries_do_not_criticize(self):
        # 提示词不能是「点评他的用词」——那正是直女的病灶
        d = _decide("好的乖宝宝鱼")
        self.assertIn("撩回去", d["act_hint"])
        self.assertNotIn("点评", d["act_hint"].replace("别点评他的用词", ""))

    def test_stranger_not_coy(self):
        # 关系不够近时，亲昵称呼不能换来撒娇资格
        d = _decide("好的乖宝宝鱼", relation=STRANGER)
        self.assertNotEqual(d["lead"][0][0], "coy")

    def test_plain_message_stays_plain(self):
        # 没有亲昵词就不该被误判
        for msg in ("好的我知道了", "买一吨吃的给你", "长高了就得吃多点"):
            with self.subTest(msg=msg):
                d = _decide(msg)
                self.assertNotEqual(d["lead"][0][0], "coy")


if __name__ == "__main__":
    unittest.main()
