"""离线回归：python -B -m unittest discover -s tests -p 'test_v2*.py' -v

载入真实入口和 engine；只替换 AstrBot 框架接口与存储/模型 I/O。
"""
import asyncio
import importlib.util
import logging
import math
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load_plugin():
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("mind-regression")
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        on_llm_request=lambda *a, **k: lambda f: f,
        command=lambda *a, **k: lambda f: f,
    )
    provider = types.ModuleType("astrbot.api.provider")
    provider.ProviderRequest = object
    star = types.ModuleType("astrbot.api.star")
    star.Context = star.Star = object
    star.register = lambda *a, **k: lambda cls: cls
    modules = {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
               "astrbot.api.event": event, "astrbot.api.provider": provider, "astrbot.api.star": star}
    spec = importlib.util.spec_from_file_location("mind_v2_test_plugin", ROOT / "main.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
        engine_module = sys.modules[spec.name + ".engine"]
        act_module = sys.modules[spec.name + ".engine.act"]
    return module, engine_module, act_module


plugin, engine, act = load_plugin()
PERSONA = {"O": 75, "C": 35, "E": 55, "A": 40, "N": 60}


def decide(message, mood=50, **kwargs):
    return act.decide(persona=PERSONA, affect={"P": 60, "Ar": 45, "D": 50},
                      relation={"T": 55, "I": 40}, scene="private", msg=message,
                      user_mood=mood, **kwargs)


def render(decision, **kwargs):
    args = dict(affect={"P": 60, "Ar": 45, "D": 50}, emotion="None", thought={},
                profile_line="", intents={}, lead=decision["lead"], budget=decision["budget"],
                recent=[], kb_hits=[], voice_lines=[], scene="private", decision=decision)
    args.update(kwargs)
    return engine.render(**args)


class DecisionTests(unittest.TestCase):
    def test_distress_excludes_teasing_even_with_direct_insult(self):
        for msg in ('他说“你个笨蛋”，我有点难受', '你个笨蛋，我今天真的撑不住了'):
            with self.subTest(msg=msg):
                d = decide(msg, 15, aggression_other=50)
                self.assertEqual(d["lead"][0][0], "care")
                self.assertEqual(d["acts"]["tease"], 0)
                self.assertEqual(d["tone"], "warm")

    def test_quoted_reported_and_negated_insults_not_direct(self):
        for msg in ('他说“你个笨蛋”', '他说你个笨蛋', '不要叫她臭鱼',
                    '我不是在骂你个笨蛋', '请解释 `你个笨蛋` 这句话', "他说 '你个笨蛋'"):
            with self.subTest(msg=msg):
                self.assertEqual(act.evidence(msg)["angry"], 0)
                self.assertEqual(decide(msg)["acts"]["tease"], 0)

    def test_genuine_play_still_allowed(self):
        d = decide("哈哈")
        self.assertEqual(d["lead"][0][0], "tease")
        self.assertEqual(d["tone"], "playful")

    def test_sharp_requires_explicit_other_aggression(self):
        self.assertEqual(decide("你个笨蛋", aggression_self=50)["tone"], "playful")
        self.assertEqual(decide("你个笨蛋", aggression_other=45)["tone"], "sharp")
        self.assertEqual(decide("你个笨蛋", aggression_other=float("nan"))["tone"], "playful")

    def test_good_news_is_warm_without_unsolicited_teasing(self):
        for msg, mood in (("我终于考过了", 95), ("这次中了奖，真开心", 100)):
            with self.subTest(msg=msg):
                d = decide(msg, mood)
                self.assertEqual(d["lead"][0][0], "answer")
                self.assertEqual(d["tone"], "warm")
                self.assertEqual(d["acts"]["tease"], 0)

    def test_mixed_request_keeps_task_and_care(self):
        d = decide("我真笨，程序报错搞得我想哭，帮我修一下", 15)
        self.assertEqual(d["lead"][0][0], "answer")
        self.assertIn("care", d["secondary_acts"])
        self.assertEqual(d["tone"], "warm")
        self.assertEqual(d["length_mode"], "task")
        text = render(d)
        self.assertIn("先回答/先解决", text)
        self.assertIn("不要用安慰替代办事", text)

    def test_task_length_exemption_survives_long_material(self):
        d = decide("请帮我写一个读取CSV的Python脚本")
        text = render(d, kb_hits=["资料" * 300] * 3, voice_lines=["语料" * 200] * 3)
        self.assertIn("字数与条数限制不适用", text)
        self.assertNotIn("每条不超过 15 字", text)

    def test_chat_keeps_short_style(self):
        d = decide("你好")
        self.assertEqual(d["lead"][0][0], "answer")
        self.assertEqual(d["length_mode"], "chat")
        self.assertIn("每条不超过 15 字", render(d))

    def test_self_negation_and_negated_self_judgment(self):
        self.assertEqual(decide("我真没用", 15)["lead"][0][0], "care")
        self.assertEqual(act.evidence("我不是废物")["self_neg"], 0)
        self.assertEqual(act.evidence("我的代码不行")["self_neg"], 0)

    def test_scores_are_finite_normalized_and_masked(self):
        for msg, mood in (("你好", 50), ("你个笨蛋", 50), ("帮我写代码，我好累", 0)):
            d = decide(msg, mood)
            self.assertAlmostEqual(sum(d["acts"].values()), 1.0)
            self.assertTrue(all(math.isfinite(v) and 0 <= v <= 1 for v in d["acts"].values()))

    def test_mood_validation(self):
        for value, expected in ((0, 0), ("0", 0), (None, 50), ("很差", 50),
                                ([], 50), (False, 50), (float("nan"), 50),
                                (float("inf"), 50), (-20, 0), (200, 100)):
            with self.subTest(value=value):
                self.assertEqual(engine.normalize_user_mood(value), expected)


class Profiles:
    def __init__(self): self.data = {"T": 55, "I": 40, "count": 0, "recent": []}
    def load(self, uid): return self.data
    def touch(self, uid, message): self.data["count"] += 1; return self.data


class HookTests(unittest.IsolatedAsyncioTestCase):
    def make_mind(self, mood, appraisal=None):
        mind = object.__new__(plugin.MindEngine)
        mind.cfg = dict(plugin.DEFAULTS, field_enable=False, kb_enable=False, voice_enable=False, profile_enable=False, summarize_every=0)
        mind.affect = engine.Affect()
        mind.profiles = Profiles()
        mind._inflight = set()
        mind._dodge_streaks = {}
        mind.bridge = types.SimpleNamespace(get=lambda *a, **k: {}, available=lambda: False)
        mind.last_decision = {}
        async def think(*args): return {"user_mood": mood, "mood": "低落", "emotion": "None", **(appraisal or {})}
        mind._think = think
        mind._save_state = lambda: None
        logs = []
        mind._log = logs.append
        return mind, logs

    async def invoke(self, mood, message="今天又出了点事", appraisal=None):
        mind, logs = self.make_mind(mood, appraisal)
        event = types.SimpleNamespace(message_str=message, unified_msg_origin="test:FriendMessage:1", get_sender_id=lambda: "test")
        req = types.SimpleNamespace(system_prompt="BASE")
        await mind.before_llm(event, req)
        self.assertNotEqual(req.system_prompt, "BASE")
        self.assertEqual(len(logs), 1)
        self.assertFalse(mind._inflight)
        return logs[0], req.system_prompt

    async def test_zero_is_preserved_through_real_hook(self):
        log, _ = await self.invoke(0)
        self.assertEqual(log["act"], "care")

    async def test_invalid_mood_keeps_injection(self):
        for value in ("很差", [], None, float("nan")):
            with self.subTest(value=value):
                await self.invoke(value)

    async def test_mixed_task_flows_through_real_hook(self):
        log, text = await self.invoke(15, "我真笨，程序报错搞得我想哭，帮我修一下")
        self.assertEqual(log["act"], "answer")
        self.assertIn("care", log["secondary_acts"])
        self.assertIn("字数与条数限制不适用", text)

    async def test_occ_zero_and_invalid_fields_do_not_break_injection(self):
        log, _ = await self.invoke(50, appraisal={"emotion": "Anger", "relevance": 0, "desirability": -1, "control": 0})
        # relevance=0 表示「这事跟我无关」，按 OCC 语义不产生冲量。
        # 关键断言：P 完全不变 —— 证明 0 被当成 0，而不是被 normalize 的默认值 0.5 顶掉。
        self.assertEqual(log["affect"]["P"], 60.0)
        self.assertEqual(log["affect"]["Ar"], 45.0)
        await self.invoke(50, appraisal={"relevance": "不确定", "desirability": [], "control": float("nan")})

    async def test_zero_impulse_does_not_discount_later_real_emotion(self):
        """零冲量若被记成一次重复事件，会让随后的真实 Anger 被折扣吃掉。"""
        from engine.affect import Mood

        mood = Mood()
        mood.apply({"P": 0.0, "Ar": 0.0, "D": 0.0}, umo="scene-a")
        self.assertEqual(len(mood._core_hist), 0, "零冲量不该进重复事件历史")
        self.assertEqual(mood.core.P, 60.0)
        self.assertEqual(mood.offset("scene-a"), {"P": 0.0, "Ar": 0.0, "D": 0.0})

        mood.apply({"P": -10.0, "Ar": 15.0, "D": 5.0}, umo="scene-a")
        # Anger 的维度和是 -10+15+5 = +10，记 sign=+1。期望值按**首次事件**的份额推：
        # 此刻历史里已有一条，再查 _core_share_for 会按重复事件打折，所以直接用 CORE_SHARE。
        event = {"P": -10.0, "Ar": 15.0, "D": 5.0}
        self.assertAlmostEqual(mood.core.P, 60.0 + event["P"] * mood.CORE_SHARE, places=6)
        self.assertAlmostEqual(mood.core.Ar, 45.0 + event["Ar"] * mood.CORE_SHARE, places=6)

    def test_mixed_unknown_keys_do_not_pollute_history_or_crash(self):
        """有效键与未知键混在一起时，未知键不该进额度历史，也不该抛异常。"""
        from engine.affect import Mood

        mood = Mood()
        mood.apply({"P": 6.0, "X": 5.0}, umo="scene-a")
        self.assertEqual(len(mood._core_hist), 1)
        self.assertEqual(set(mood._core_hist[0].granted), {"P"}, "未知键不该写进额度历史")
        mood.apply({"P": 1.0, "X": "bad"}, umo="scene-a")  # 不该抛 TypeError
        self.assertEqual(len(mood._core_hist), 2)

    def test_num_or_keeps_real_zero(self):
        """势场里 `x or 50.0` 会把真实 0 抹成 50。"""
        from engine.affect import num_or

        self.assertEqual(num_or(0, 50.0), 0.0)
        self.assertEqual(num_or(0.0, 50.0), 0.0)
        self.assertEqual(num_or(None, 50.0), 50.0)
        self.assertEqual(num_or("", 50.0), 50.0)
        self.assertEqual(num_or("bad", 50.0), 50.0)
        self.assertEqual(num_or(float("nan"), 50.0), 50.0)
        self.assertEqual(num_or(35, 50.0), 35.0)

    def test_normalize_preserves_exact_zero(self):
        from engine.appraisal import normalize_appraisal_number as norm

        self.assertEqual(norm(0, 0.0, 1.0, 0.5), 0.0)
        self.assertEqual(norm(0.0, 0.0, 1.0, 0.5), 0.0)
        self.assertEqual(norm(None, 0.0, 1.0, 0.5), 0.5)

    async def test_dodge_break_is_independent_between_group_sessions(self):
        mind, logs = self.make_mind(50)
        mind.cfg["persona_e"] = 0
        mind.affect.Ar = mind.affect.base["Ar"] = 0
        mind.affect.P = mind.affect.base["P"] = 50
        for group in ("A", "B", "A", "B", "A", "B"):
            event = types.SimpleNamespace(
                message_str="在吗", unified_msg_origin=f"test:GroupMessage:{group}",
                get_sender_id=lambda: "test",
            )
            req = types.SimpleNamespace(system_prompt="BASE")
            await mind.before_llm(event, req)
            self.assertNotEqual(req.system_prompt, "BASE")
        self.assertEqual([row["act"] for row in logs], ["dodge"] * 6)
        self.assertEqual([row["budget"] for row in logs], [0, 0, 1, 1, 0, 0])
        self.assertFalse(mind._inflight)

    async def test_voice_intimacy_filter_is_wired_through_real_hook(self):
        mind, _ = self.make_mind(0)
        mind.cfg["voice_enable"] = True
        mind.voice = engine.VoiceCorpus(None)
        mind.voice.sections = {"关心": ["普通关心样本"], "亲近": ["只给熟人的亲近样本"]}
        event = types.SimpleNamespace(
            message_str="今天很难受", unified_msg_origin="test:FriendMessage:1",
            get_sender_id=lambda: "test",
        )
        # care 的禁用集合里有「亲近」，这道闸优先于关系档位：
        # 关心的时候撒娇或损他，前面所有铺垫都白做。
        # 所以 I=80 也不该出现亲近样本；关系档位真正放开的是 share 之类的行为。
        for intimacy, expected in ((20, False), (80, False)):
            with self.subTest(intimacy=intimacy, act="care"):
                mind.profiles.data["I"] = intimacy
                req = types.SimpleNamespace(system_prompt="BASE")
                await mind.before_llm(event, req)
                self.assertIn("普通关心样本", req.system_prompt)
                self.assertEqual("只给熟人的亲近样本" in req.system_prompt, expected)

        # 换成 share，关系档位才有话语权：亲近档才允许「亲近」组。
        mind.voice.sections = {"说自己的日子": ["自述样本"], "亲近": ["只给熟人的亲近样本"]}
        for intimacy, expected in ((20, False), (80, True)):
            with self.subTest(intimacy=intimacy, act="share"):
                mind.profiles.data["I"] = intimacy
                got = mind.voice.pick("share", n=3, relation={"I": intimacy, "T": 55})
                self.assertEqual(any("只给熟人的亲近样本" in g for g in got), expected)


if __name__ == "__main__":
    unittest.main()
