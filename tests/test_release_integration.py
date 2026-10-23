"""Release contracts using real hook/state methods, fake providers and temporary files."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import test_v2_regressions as fixture
from test_v2_regressions import plugin, engine


def event(message, uid="alice", group=False):
    return types.SimpleNamespace(
        message_str=message,
        unified_msg_origin="fake:GroupMessage:G" if group else "fake:FriendMessage:alice",
        get_sender_id=lambda: uid, get_self_id=lambda: "synthetic_bot",
        is_admin=lambda: True, plain_result=lambda value: value)


class StateIntegrationTests(unittest.TestCase):
    def make(self, directory, **config):
        m = object.__new__(plugin.MindEngine)
        m.cfg = dict(plugin.DEFAULTS, **config)
        m.dir = Path(directory)
        m.state_path = m.dir / "state.json"
        m.mood = engine.Mood()
        return m

    def test_own_mood_ignores_last_relation_and_all_scenes(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(time, "time", return_value=10000):
            m = self.make(temp)
            m.mood.rebase({"P": 17.5})
            self.assertEqual(m._own_pad()["P"], 60)
            m.mood.apply({"P": 10}, umo="A")
            own = m._own_pad()
            self.assertEqual(own["P"], 61.4)
            m.mood.scenes[""] = {"P": 60, "Ar": -60, "D": 60, "ts": 10000}
            m.mood.rebase({"P": 82.5})
            self.assertEqual(m._own_pad(), own)
            self.assertEqual(m.mood.core.base["P"], 82.5)
            m._save_state()
            saved = json.loads((Path(temp) / "pad.json").read_text(encoding="utf-8"))
            for key in ("P", "Ar", "D"):
                self.assertEqual(saved[key], own[key])

    def test_saved_reference_survives_real_plugin_restart(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(time, "time", return_value=10000):
            m = self.make(temp)
            m.mood.rebase({"P": 69, "Ar": 42, "D": 48.5})
            m.mood.apply({"P": 10}, umo="A")
            before = m.mood.effective("A")
            plugin.MindEngine._save_state(m)
            loaded = self.make(temp)
            loaded._load_state()
            loaded.mood.rebase({"P": 69, "Ar": 42, "D": 48.5})
            self.assertEqual(loaded.mood.effective("A"), before)
            self.assertEqual(loaded.mood.core.base["P"], 69)
            self.assertEqual(loaded.mood.core.P - 69, 4)
            self.assertEqual(len(loaded.mood._core_hist), 1)

    def test_disabled_field_changes_reference_not_saved_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            m = self.make(temp)
            m.mood.rebase({"P": 69})
            m.mood.core.P += 12
            m._save_state()
            loaded = self.make(temp, field_enable=False)
            loaded._load_state()
            self.assertEqual(loaded.mood.core.base["P"], 60)
            self.assertEqual(loaded.mood.core.P, 72)

    def test_new_and_legacy_state_use_configured_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            m = self.make(temp, affect_p=42)
            m._load_state()
            self.assertEqual(m.mood.core.P, 42)
            m.state_path.write_text(json.dumps({"affect": {"P": 47, "Ar": 45, "D": 50}}))
            loaded = self.make(temp, affect_p=42)
            loaded._load_state()
            self.assertEqual(loaded.mood.core.P - loaded.mood.core.base["P"], 5)

    def test_traits_first_start_and_restart_agree(self):
        with tempfile.TemporaryDirectory() as temp:
            config = dict(persona_o=10, chars_per_line=36, play_bias=0.7,
                          max_lines_private=2, max_lines_group=1)
            first, second = self.make(temp, **config), self.make(temp, **config)
            first._load_traits()
            second._load_traits()
            for key, value in config.items():
                self.assertEqual(first.cfg[key], value)
                self.assertEqual(second.cfg[key], value)


class HookIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_assistant_question_is_used_not_predicted_act(self):
        m, logs = fixture.HookTests().make_mind(50)
        e = event("嗯")
        m.last_decision[e.unified_msg_origin] = {"uid": "alice", "act": "answer"}
        req = types.SimpleNamespace(system_prompt="BASE", contexts=[
            {"role": "user", "content": "今天一般"},
            {"role": "assistant", "content": "需要我陪你聊聊吗？"}])
        await m.before_llm(e, req)
        self.assertEqual(logs[-1]["act"], "answer")
        self.assertTrue(m.last_decision[e.unified_msg_origin]["previous_asked"])

    async def test_group_question_does_not_transfer_to_another_user(self):
        m, _ = fixture.HookTests().make_mind(50)
        e = event("嗯", uid="bob", group=True)
        m.last_decision[e.unified_msg_origin] = {"uid": "alice", "act": "care"}
        req = types.SimpleNamespace(system_prompt="BASE", contexts=[
            {"role": "assistant", "content": "需要我陪你聊聊吗？"}])
        await m.before_llm(e, req)
        self.assertFalse(m.last_decision[e.unified_msg_origin]["previous_asked"])

    async def test_care_act_without_a_question_does_not_create_one(self):
        m, _ = fixture.HookTests().make_mind(50)
        e = event("嗯")
        m.last_decision[e.unified_msg_origin] = {"uid": "alice", "act": "care", "budget": 4}
        req = types.SimpleNamespace(system_prompt="BASE", contexts=[
            {"role": "assistant", "content": [{"type": "text", "text": "累了就歇会。"}]}])
        await m.before_llm(e, req)
        self.assertFalse(m.last_decision[e.unified_msg_origin]["previous_asked"])

    async def test_real_bridge_fields_drive_the_baseline(self):
        rows = []
        for value in (0, 50):
            m, logs = fixture.HookTests().make_mind(50)
            m.cfg["field_enable"] = True
            m.bridge = engine.AffectionBridge(None)
            m.bridge._ts = time.time()
            m.bridge._cache = {"synthetic_bot": {"alice": {
                "affection": 50, "current_libido_other": value, "current_aggression_other": value}}}
            await m.before_llm(event("你个笨蛋"), types.SimpleNamespace(system_prompt="BASE"))
            rows.append((dict(m.mood.core.base), logs[-1]["tone"]))
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertEqual([r[1] for r in rows], ["playful", "sharp"])

    async def test_chat_cap_applies_but_task_still_has_full_answer(self):
        for text, mode, budget in [("今天很难受", "chat", 1), ("帮我写一个脚本", "task", 3)]:
            m, logs = fixture.HookTests().make_mind(50)
            m.cfg["max_lines_private"] = 1
            req = types.SimpleNamespace(system_prompt="BASE")
            await m.before_llm(event(text), req)
            self.assertEqual(logs[-1]["length_mode"], mode)
            self.assertEqual(logs[-1]["budget"], budget)
            if mode == "task":
                self.assertIn("字数与条数限制不适用", req.system_prompt)
            else:
                self.assertIn("这一轮说：1 条左右", req.system_prompt)

    async def test_failure_has_traceback_rate_limit_and_recovers(self):
        m, _ = fixture.HookTests().make_mind(50)
        async def fail(*args):
            raise NameError("synthetic_failure")
        m._inject = fail
        with patch.object(plugin.logger, "error") as errors, patch.object(plugin.time, "time", return_value=10000):
            for _ in range(3):
                req = types.SimpleNamespace(system_prompt="BASE")
                await m.before_llm(event("你好"), req)
                self.assertEqual(req.system_prompt, "BASE")
            self.assertEqual(errors.call_count, 1)
            self.assertIsNotNone(errors.call_args.kwargs["exc_info"][2])
            self.assertEqual(m._health["inject"]["consecutive"], 3)
            self.assertFalse(m._inflight)
            self.assertIn("连续失败3次", m._health_line())
            async def succeed(*args): pass
            m._inject = succeed
            await m.before_llm(event("你好"), types.SimpleNamespace(system_prompt="BASE"))
            self.assertEqual(m._health["inject"]["consecutive"], 0)
            self.assertEqual(m._health["inject"]["last_success"], 10000)

    async def test_log_health_recovers_after_write_failure(self):
        m, _ = fixture.HookTests().make_mind(50)
        m.cfg["log_enable"] = True
        with tempfile.TemporaryDirectory() as temp:
            m._log_path = Path(temp) / "mind.log"
            with patch("builtins.open", side_effect=OSError("synthetic disk failure")), patch.object(plugin.logger, "error"):
                plugin.MindEngine._log(m, {"synthetic": True})
            self.assertEqual(m._health["log"]["consecutive"], 1)
            plugin.MindEngine._log(m, {"synthetic": True})
            self.assertEqual(m._health["log"]["consecutive"], 0)
            self.assertEqual(json.loads(m._log_path.read_text(encoding="utf-8")), {"synthetic": True})

    async def test_think_does_not_inherit_previous_speakers_baseline(self):
        class Context:
            async def llm_generate(self, **kwargs):
                return types.SimpleNamespace(completion_text='{"emotion":"None"}', usage={})
        m, _ = fixture.HookTests().make_mind(50)
        m.cfg["think_provider"] = "synthetic/provider"
        m.context = Context()
        with patch.object(plugin, "feel_line", return_value="synthetic mood") as feel:
            for baseline in (17.5, 82.5):
                m.mood.rebase({"P": baseline})
                await plugin.MindEngine._think(m, "你好", "", "")
            self.assertEqual(feel.call_args_list[0].args, feel.call_args_list[1].args)
            self.assertEqual(feel.call_args.args[0]["P"], 60)

    async def test_failed_provider_cannot_reuse_success_tokens(self):
        class Context:
            fail = False
            async def llm_generate(self, *, chat_provider_id, prompt):
                if self.fail: raise RuntimeError("synthetic failure")
                return types.SimpleNamespace(completion_text='{"emotion":"None"}', usage={"total_tokens": 123})
        m, logs = fixture.HookTests().make_mind(50)
        m.cfg["think_provider"] = "synthetic/provider"
        m.context = Context()
        m._think = types.MethodType(plugin.MindEngine._think, m)
        with patch.object(plugin.logger, "error"):
            for fail in (False, True):
                m.context.fail = fail
                await m.before_llm(event("你好"), types.SimpleNamespace(system_prompt="BASE"))
        self.assertEqual(logs[0]["cost"]["total_tokens"], 123)
        self.assertNotIn("total_tokens", logs[1]["cost"])
        self.assertFalse(logs[1]["cost"]["ok"])
        self.assertEqual(logs[1]["cost"]["error"], "RuntimeError")

    async def test_why_uses_only_the_recorded_turn(self):
        m, _ = fixture.HookTests().make_mind(50)
        m.voice = engine.VoiceCorpus(None)
        def forbidden(*args, **kwargs): raise AssertionError("must not read another user's state")
        m.profiles = types.SimpleNamespace(load=forbidden)
        m.bridge = types.SimpleNamespace(get=forbidden)
        e = event("mind why", uid="admin", group=True)
        m.last_decision[e.unified_msg_origin] = {"uid": "alice", "msg": "synthetic message",
            "pad": {"P": 79, "Ar": 61, "D": 59}, "relation": {"T": 80, "I": 80},
            "aff": {"affection": 90}, "tier": 2, "act": "share", "tone": "warm", "acts": {"share": 1}}
        reply = "\n".join([r async for r in m.cmd_mind(e)])
        self.assertIn("信任80 亲密80", reply)
        self.assertIn("愉悦79.0", reply)
        self.assertIn("好感 90", reply)
        self.assertIn("alice", reply)

    async def test_provider_registry_can_recover_after_initial_empty(self):
        m, _ = fixture.HookTests().make_mind(50)
        registry = []
        m.context = types.SimpleNamespace(get_all_providers=lambda: registry)
        m._auto_provider_id = None
        self.assertEqual(m._resolve_provider(), "")
        registry.append(types.SimpleNamespace(provider_config={"id": "synthetic/deepseek"}))
        self.assertEqual(m._resolve_provider(), "synthetic/deepseek")

    async def test_kb_command_passes_query_text(self):
        m, _ = fixture.HookTests().make_mind(50)
        seen = []
        m.kb = types.SimpleNamespace(chunks=[], query=lambda text, k: seen.append(text) or [])
        m.voice = types.SimpleNamespace(count=lambda: 0)
        replies = [r async for r in m.cmd_mind(event("mind kb 合成查询"))]
        self.assertTrue(replies)
        self.assertEqual(seen, ["合成查询"])
