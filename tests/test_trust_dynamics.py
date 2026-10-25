"""1.2.2 三条改动的回归测试：信任 T 动力学、信念覆盖、侧写噪声过滤。

纯离线单测：不连 AstrBot、不调模型。
"""
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_v2_regressions import load_plugin

_, engine, _ = load_plugin()
ProfileStore = engine.profiles.ProfileStore
SelfModel = engine.self_model.SelfModel

UID = "3388708615"


def _cfg():
    """SelfModel 需要的最小配置：演化打开，阈值放到最容易触发的位置。"""
    return {
        "evolution_mode": "rule",
        "evolution_relevance_percentile": 50,
        "evolution_max_candidates_per_day": 20,
        "evolution_max_new_beliefs_per_day": 20,
    }


class TrustDynamicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProfileStore(Path(self.tmp.name) / "profiles", 55, 40)

    def _backdate(self, prof, days=1):
        """把记账日改到过去，模拟「隔了几天又来了」。"""
        prof["T_day"] = time.strftime(
            "%Y-%m-%d", time.localtime(time.time() - days * 86400)
        )
        self.store.save(prof)
        return prof

    def test_first_ever_visit_does_not_raise_trust(self):
        # 第一次来不算「隔天还来」，但愿意多讲几句仍算一次自我披露（+0.1）
        prof = self.store.touch(UID, "今天上班路上看见一只很胖的猫，蹲在便利店门口")
        self.assertAlmostEqual(prof["T"], 55.1, places=1)
        self.assertEqual(prof["T_days"], 1)

    def test_returning_next_day_raises_trust(self):
        prof = self.store.touch(UID, "晚饭吃的泡面，加了个蛋")
        self._backdate(prof, 1)
        prof = self.store.touch(UID, "今天又在加班，好累")
        # 隔天还来 +0.4，这条也算自我披露 +0.1
        self.assertAlmostEqual(prof["T"], 55.6, places=1)
        self.assertEqual(prof["T_days"], 2)

    def test_same_day_messages_only_credit_once(self):
        self.store.touch(UID, "第一句，先说今天都发生了什么吧")
        prof = self.store.touch(UID, "第二句，接着聊今天遇到的事情")
        prof = self.store.touch(UID, "第三句，还在说今天遇到的麻烦")
        # 同一天只有自我披露那几笔，没有「隔天还来」的分
        self.assertAlmostEqual(prof["T"], 55.3, places=1)
        self.assertEqual(prof["T_days"], 1)

    def test_short_message_does_not_count_as_self_disclosure(self):
        prof = self.store.touch(UID, "嗯嗯")
        self.assertEqual(prof["T"], 55.0)

    def test_insult_drops_trust(self):
        self.store.touch(UID, "先聊一句正常的")
        prof = self.store.touch(UID, "你这破插件真烦人", negative=True)
        self.assertAlmostEqual(prof["T"], 54.3, places=1)

    def test_insult_offsets_same_day_gain(self):
        prof = self.store.touch(UID, "今天过得还算可以吧")
        self._backdate(prof, 1)
        prof = self.store.touch(UID, "烦死了滚", negative=True)
        # 55.1 -> 次日 +0.4 -> 54.7
        self.assertAlmostEqual(prof["T"], 54.7, places=1)

    def test_group_chat_does_not_move_trust_on_new_day(self):
        prof = self.store.touch(UID, "私聊先说一句，今天挺累的")
        self._backdate(prof, 1)
        prof = self.store.touch(UID, "群里大家今天都在聊什么呢我看看", scene="group")
        # 群聊只滚动日期，不给返场分、也不推进 T_days
        self.assertAlmostEqual(prof["T"], 55.1, places=1)
        self.assertEqual(prof["T_days"], 1)

    def test_daily_cap_limits_gain(self):
        prof = self.store.touch(UID, "第一天先随便说两句")
        self._backdate(prof, 1)
        for i in range(30):
            prof = self.store.touch(UID, f"第{i}条：今天发生了不少事情想跟你慢慢讲")
        self.assertLessEqual(prof["T"], 56.6 + 0.001)
        self.assertLessEqual(prof["T_today"], 1.5 + 0.001)

    def test_cap_smaller_than_day_gain(self):
        store = ProfileStore(
            Path(self.tmp.name) / "small_cap", 55, 40, trust_day_gain=0.4, trust_cap_day=0.2
        )
        prof = store.touch(UID, "第一次来，随便聊两句吧")
        prof["T_day"] = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        store.save(prof)
        base = prof["T"]
        prof = store.touch(UID, "第二天又来了，今天想多说几句")
        self.assertLessEqual(prof["T"] - base, 0.2 + 0.001, "日额度小于单次加分时，仍要守额度")

    def test_insult_is_bounded_by_daily_cap(self):
        self.store.touch(UID, "先聊一句正常的")
        # 必须用不同文本：同一句会被 md5 指纹去重，测不到额度逻辑
        for i in range(10):
            prof = self.store.touch(UID, f"第{i}句：你这破玩意儿真垃圾", negative=True)
        self.assertGreaterEqual(prof["T"], 55.0 - 1.5 - 0.001, "连骂也要受当日额度约束")
        self.assertAlmostEqual(prof["T_today"], -1.5, places=2, msg="连骂应正好停在当日地板")

    def test_same_message_twice_settles_once(self):
        prof = self.store.touch(UID, "同一句话被钩子触发了两次的情况")
        prof = self.store.touch(UID, "同一句话被钩子触发了两次的情况")
        self.assertAlmostEqual(prof["T"], 55.1, places=1)

    def test_trust_stays_in_range(self):
        prof = self.store.load(UID)
        prof["T"] = 99.9
        self.store.save(prof)
        self._backdate(prof, 1)
        prof = self.store.touch(UID, "又来了，今天想跟你多说几句心里话")
        self.assertLessEqual(prof["T"], 100.0)

    def test_bad_config_falls_back(self):
        store = ProfileStore(
            Path(self.tmp.name) / "bad_cfg", 55, 40,
            trust_penalty="abc", trust_day_gain=-3, trust_cap_day=float("inf"),
        )
        self.assertAlmostEqual(store.trust_penalty, 0.8)
        self.assertEqual(store.trust_day_gain, 0.0)
        self.assertLess(store.trust_cap_day, 1e9)
        prof = store.touch(UID, "配置写坏了也不能炸")
        self.assertIsInstance(prof["T"], float)

    def test_corrupt_profile_is_kept_aside(self):
        path = self.store._path(UID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"uid": "3388708615", "称呼": "宝', encoding="utf-8")
        prof = self.store.load(UID)
        self.assertEqual(prof["称呼"], "")
        self.assertTrue(path.with_suffix(".json.corrupt").exists(), "损坏档案要留档，不能直接抹掉")


class ProfileNoiseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProfileStore(Path(self.tmp.name) / "profiles", 55, 40)

    def test_interjection_not_stored(self):
        prof = self.store.touch(UID, "哈哈哈哈哈哈")
        self.assertEqual(prof["recent"], [])

    def test_short_message_not_stored(self):
        prof = self.store.touch(UID, "嗯")
        self.assertEqual(prof["recent"], [])

    def test_small_facts_are_kept(self):
        # 「在成都」「叫我老王」「夜班」正是称呼/身份/作息的原料，不能被长度门槛误杀
        for msg in ("我在成都", "叫我老王", "夜班", "家里有只猫", "程序员"):
            prof = self.store.touch(UID, msg)
            self.assertIn(msg, prof["recent"], f"{msg} 应被保留")

    def test_meaningful_message_stored(self):
        msg = "今天去了普陀山，海边的风特别大，回来路上买了桂花糕"
        prof = self.store.touch(UID, msg)
        self.assertEqual(prof["recent"], [msg[:60]])

    def test_duplicate_not_stored_twice(self):
        msg = "明天要早起赶车，今晚得早点睡了"
        self.store.touch(UID, msg)
        prof = self.store.touch(UID, msg)
        self.assertEqual(len(prof["recent"]), 1)

    def test_emoji_only_not_stored(self):
        prof = self.store.touch(UID, "🙂🙂🙂🙂🙂🙂🙂")
        self.assertEqual(prof["recent"], [])


class BeliefCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.model = SelfModel(Path(self.tmp.name), dict(_cfg()))
        # 默认把「现在」钉在下午三点，避免夜间分支把结果吃掉
        self.clock = patch.object(
            engine.self_model.time, "localtime",
            lambda *a: time.struct_time((2026, 10, 25, 15, 0, 0, 6, 298, -1)),
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def _observe(self, text, thought, scene="private"):
        return self.model.observe(
            thought=thought, text=text, uid=UID, scene=scene,
            relevance=99, relevance_cut=0,
        )

    def test_listen_now_produces_candidate(self):
        cand = self._observe("我心里有点事，就想跟你说说", {"want_from_me": "listen"})
        self.assertIsNotNone(cand)
        self.assertEqual(cand["topic"], "listen")
        self.assertEqual(cand["claim"], "他心里有事愿意讲给我听")

    def test_play_now_produces_candidate(self):
        cand = self._observe("来来来陪我闹一会儿", {"want_from_me": "play"})
        self.assertIsNotNone(cand)
        self.assertEqual(cand["kind"], "routine")
        self.assertEqual(cand["topic"], "play")

    def test_taste_produces_preference_candidate(self):
        cand = self._observe("我最近特别喜欢听坂本龙一", {})
        self.assertIsNotNone(cand)
        self.assertEqual(cand["kind"], "preference")
        self.assertEqual(cand["topic"], "taste")

    def test_negative_taste_uses_dislike_template(self):
        cand = self._observe("我最讨厌加班到十点", {})
        self.assertIsNotNone(cand)
        self.assertEqual(cand["topic"], "dislike")
        self.assertEqual(cand["claim"], "他知道自己不喜欢什么")

    def test_taste_about_her_is_not_preference(self):
        self.assertIsNone(self._observe("我讨厌你", {}))
        self.assertIsNone(self._observe("我不喜欢你这样", {}))

    def test_plain_message_without_signal_produces_nothing(self):
        self.assertIsNone(self._observe("今天下午三点开会", {}))

    def test_advice_still_maps_to_capability(self):
        cand = self._observe("你说我这个情况该怎么办", {"want_from_me": "advice"})
        self.assertIsNotNone(cand)
        self.assertEqual(cand["kind"], "capability")

    def _at_night(self):
        self.clock.stop()
        night = patch.object(
            engine.self_model.time, "localtime",
            lambda *a: time.struct_time((2026, 10, 25, 2, 30, 0, 6, 298, -1)),
        )
        night.start()
        self.addCleanup(night.stop)

    def test_night_branch(self):
        self._at_night()
        cand = self._observe("又睡不着了，脑子一直在转", {})
        self.assertIsNotNone(cand)
        self.assertEqual(cand["topic"], "night")

    def test_night_ignores_empty_at(self):
        self._at_night()
        self.assertIsNone(self._observe("嗯", {}), "深夜一个「嗯」不该种一条人格信念")

    def test_night_ignores_group(self):
        self._at_night()
        self.assertIsNone(self._observe("群里还有人没睡吗", {}, scene="group"))

    def test_night_candidate_once_per_day(self):
        self._at_night()
        first = self._observe("又睡不着了，脑子一直在转", {})
        second = self._observe("还是睡不着，刚又看了一下手机", {})
        self.assertIsNotNone(first)
        self.assertIsNone(second, "同一晚只种一条，别让 2 点聊天吃光当天配额")

    def test_taste_wins_over_night(self):
        self._at_night()
        cand = self._observe("我超爱吃火锅，半夜也想来一碗", {})
        self.assertIsNotNone(cand)
        self.assertEqual(cand["topic"], "taste", "凌晨讲喜好应该记成偏好，不是作息")


if __name__ == "__main__":
    unittest.main()
