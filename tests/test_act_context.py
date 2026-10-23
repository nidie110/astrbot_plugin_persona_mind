"""上下文、关系边界和话题退出的离线回归，不调用模型或用户存储。"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.act import decide, evidence
from engine.prompts import render


def decision(message, **kwargs):
    args = dict(persona={"O":75, "C":35, "E":55, "A":40, "N":60},
                affect={"P":60, "Ar":45, "D":50}, relation={"T":55, "I":40},
                scene="private", msg=message)
    args.update(kwargs)
    return decide(**args)


def prompt(result, tier=1):
    return render(affect={"P":60, "Ar":45, "D":50}, emotion="None", thought={},
                  profile_line="", intents={}, lead=result["lead"], budget=result["budget"],
                  recent=[], kb_hits=[], voice_lines=[], scene="private", tier=tier,
                  decision=result)


class ActContextTests(unittest.TestCase):
    def test_explicit_question_flag_is_authoritative(self):
        # main 已判定没问当前用户时，旧用户问句不能把 False 翻回 True。
        self.assertEqual(evidence("嗯", prev="今天几号？", prev_asked=False)["empty"], 1)
        self.assertEqual(evidence("嗯", prev="今天一般", prev_asked=True)["empty"], 0)
        self.assertEqual(decision("嗯", prev="今天一般", prev_asked=True)["lead"][0][0], "answer")
        self.assertEqual(decision("嗯", prev="今天几号？", prev_asked=False)["budget"], 0)
        # 未提供新接口的旧调用仍能传入已确认的上一条问句。
        self.assertEqual(evidence("嗯", prev="还想聊聊吗？")["empty"], 0)

    def test_quotes_are_content_not_empty_messages(self):
        for message in ('“今天我很开心”', '「一段新消息」', '`a = 42`'):
            with self.subTest(message=message):
                self.assertEqual(evidence(message)["empty"], 0)
                self.assertGreater(decision(message)["budget"], 0)
        # 引用内容不能反过来获得直接攻击资格。
        self.assertEqual(evidence('“你个笨蛋”')["angry"], 0)

    def test_intimacy_gates_coy_and_seek_at_same_boundary(self):
        for intimacy in (20, 39.9, 40, 70):
            with self.subTest(intimacy=intimacy):
                relation = {"T":55, "I":intimacy}
                coy = decision("抱抱", relation=relation)
                seek = decision("你好", relation=relation, affect={"P":0, "Ar":45, "D":50})
                self.assertEqual(coy["acts"]["coy"] > 0, intimacy >= 40)
                self.assertEqual(seek["acts"]["seek"] > 0, intimacy >= 40)
        stranger = decision("你好", relation={"T":55, "I":20}, affect={"P":0, "Ar":45, "D":50})
        text = prompt(stranger, tier=0)
        self.assertIn("别主动讲自己的事", text)
        self.assertNotIn("说一句自己的低落", text)

    def test_explicit_play_is_preserved_for_new_and_familiar_users(self):
        for intimacy in (20, 70):
            with self.subTest(intimacy=intimacy):
                result = decision("哈哈", relation={"T":55, "I":intimacy})
                self.assertEqual(result["lead"][0][0], "tease")
                self.assertEqual(result["tone"], "playful")

    def test_topic_exit_has_one_line_and_specific_instruction(self):
        for message in ("不说了", "算了不说了哈哈", "不说了，你个笨蛋"):
            with self.subTest(message=message):
                result = decision(message)
                self.assertTrue(result["close_topic"])
                self.assertEqual(result["lead"][0][0], "answer")
                self.assertEqual(result["budget"], 1)
                text = prompt(result)
                self.assertIn("不追问、不续聊", text)
                self.assertNotIn("先回答/先解决", text)

    def test_quotes_negation_and_recovery_do_not_request_exit(self):
        for message in ('他说“算了”', '他说算了', '不要说“算了”', '别算了',
                        '我已经没事了，想和你聊聊'):
            with self.subTest(message=message):
                self.assertEqual(evidence(message)["giveup"], 0)
                self.assertFalse(decision(message)["close_topic"])

    def test_task_and_distress_keep_priority_over_exit(self):
        task = decision("算了不说了，帮我写一个脚本")
        self.assertFalse(task["close_topic"])
        self.assertEqual(task["length_mode"], "task")
        self.assertIn("字数与条数限制不适用", prompt(task))
        distress = decision("算了不说了，我撑不住了", user_mood=10)
        self.assertFalse(distress["close_topic"])
        self.assertEqual(distress["lead"][0][0], "care")
        mixed = decision("算了我真笨，帮我修这个报错", user_mood=10)
        self.assertEqual(mixed["lead"][0][0], "answer")
        self.assertIn("care", mixed["secondary_acts"])


if __name__ == "__main__":
    unittest.main()
