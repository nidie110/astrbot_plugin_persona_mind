"""管理面命令与语义的离线测试。

复审 P2-8 指出：`/mind config`、`approve/reject` 从来没有测试覆盖，
而 P1-2 / P1-3 恰好都出在这些路径上。
"""
import importlib.util
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load_plugin():
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("mind-admin-test")
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
    msg_mod = types.ModuleType("astrbot.core.agent.message")

    class _TextPart:
        def __init__(self, text: str = ""):
            self.text = text

    msg_mod.TextPart = _TextPart
    msg_mod.ContentPart = _TextPart
    agent_mod = types.ModuleType("astrbot.core.agent")
    agent_mod.message = msg_mod
    core_mod = types.ModuleType("astrbot.core")
    core_mod.agent = agent_mod
    modules = {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
               "astrbot.api.event": event, "astrbot.api.provider": provider,
               "astrbot.api.star": star, "astrbot.core": core_mod,
               "astrbot.core.agent": agent_mod,
               "astrbot.core.agent.message": msg_mod}
    spec = importlib.util.spec_from_file_location(
        "mind_admin_test_plugin", ROOT / "main.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


M = load_plugin()
cls = getattr(M, "PersonaMind", None) or getattr(M, "MindEngine")


class FakeEngine:
    _cfg_file = cls._cfg_file
    _cfg_save = cls._cfg_save
    _cfg_check = cls._cfg_check
    _cmd_config = cls._cmd_config
    _CFG_LIMITS = cls._CFG_LIMITS
    _CFG_ENUMS = cls._CFG_ENUMS

    def __init__(self, workdir: str):
        self.cfg = dict(M.DEFAULTS)
        self.dir = Path(workdir)
        self.self_model = None


class AdminConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.getcwd()
        os.chdir(self.tmp)
        self.eng = FakeEngine(self.tmp)

    def tearDown(self):
        os.chdir(self._old)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, *args):
        return self.eng._cmd_config(["/mind", "config"] + list(args))

    def test_get_returns_default(self):
        out = self.call("get", "field_strength")
        self.assertIn("1.0", out)

    def test_set_persists_to_file(self):
        self.call("set", "field_strength", "0.5")
        p = Path(self.tmp) / "data" / "config" / "astrbot_plugin_persona_mind_config.json"
        self.assertTrue(p.is_file(), "set 应真的写盘")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["field_strength"], 0.5)

    def test_set_out_of_range_rejected(self):
        out = self.call("set", "field_strength", "100")
        self.assertIn("不合法", out)
        self.assertEqual(self.eng.cfg["field_strength"], 1.0, "越界值不得写入")

    def test_set_negative_confirm_count_rejected(self):
        out = self.call("set", "evolution_confirm_count", "-3")
        self.assertIn("不合法", out)

    def test_enum_mode_validated(self):
        out = self.call("set", "evolution_mode", "whatever")
        self.assertIn("只能", out)

    def test_unknown_key_hints(self):
        out = self.call("set", "ding", "1")
        self.assertIn("没有配置项", out)

    def test_reset_restores_default(self):
        self.call("set", "field_strength", "0.3")
        self.call("reset", "field_strength")
        self.assertEqual(self.eng.cfg["field_strength"], 1.0)

    def test_bool_text_off_becomes_false(self):
        self.call("set", "diary_enable", "off")
        self.assertIs(self.eng.cfg["diary_enable"], False)


class AdminRejectLockTest(unittest.TestCase):
    """P1-3：管理员否决后，普通消息不得复活这条信念。"""

    def setUp(self):
        from engine.self_model import SelfModel

        self.dir = tempfile.mkdtemp()
        self.sm = SelfModel(self.dir, {"evolution_mode": "rule",
                                       "evolution_confirm_count": 2,
                                       "evolution_max_new_beliefs_per_day": 10})
        self.n = 0

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _cand(self):
        self.n += 1
        return self.sm.observe(
            thought={"want_from_me": "comfort", "doing": "他今天很累"},
            text=f"我好累（第{self.n}次）", uid="u1", scene="private", relevance=0.9,
        )

    def test_admin_reject_blocks_revive(self):
        self.sm.apply(self._cand(), "accept", auto=False)
        rows = self.sm.review(5)
        self.assertTrue(rows)
        self.sm.approve(rows[0]["id"], "reject")
        for _ in range(4):
            self.sm.apply(self._cand(), "accept")
        after = self.sm.review(5)[0]
        self.assertEqual(after["status"], "reject", "管理员否决后不应被普通消息复活")
        self.assertTrue(after.get("admin_reject"))

    def test_admin_accept_unlocks(self):
        self.sm.apply(self._cand(), "accept", auto=False)
        bid = self.sm.review(5)[0]["id"]
        self.sm.approve(bid, "reject")
        self.sm.approve(bid, "accept")
        after = self.sm.review(5)[0]
        self.assertEqual(after["status"], "accept")
        self.assertFalse(after.get("admin_reject"), "人工采纳应解除否决锁")


if __name__ == "__main__":
    unittest.main()
