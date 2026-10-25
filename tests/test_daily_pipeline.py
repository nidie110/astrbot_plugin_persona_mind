"""daily 档端到端隔离测试：素材进队 → 归纳 → 写信念 → 清队列。"""
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_plugin():
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("mind-daily")
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
    for name, mod in modules.items():
        sys.modules.setdefault(name, mod)
    import importlib.util
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "mind_daily_main", os.path.join(here, "main.py"),
        submodule_search_locations=[here])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mind_daily_main"] = mod
    spec.loader.exec_module(mod)
    return mod


MAIN = load_plugin()
SelfModel = MAIN.SelfModel


class _FakeCtx:
    """假的 AstrBot context：只实现 daily 用到的两个方法。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    async def llm_generate(self, **kw):
        self.calls += 1
        return types.SimpleNamespace(completion_text=self.reply)

    def get_all_providers(self):
        return []


def _mk_plugin(tmp, reply, mode="daily", **over):
    obj = MAIN.MindEngine.__new__(MAIN.MindEngine)
    cfg = {
        "evolution_mode": mode,
        "evolution_confirm_count": 2,
        "evolution_max_new_beliefs_per_day": 3,
        "evolution_daily_timeout": 10,
        "evolution_daily_interval_hours": 6,
        "think_provider": "fake/provider",
        "profile_enable": True,
    }
    cfg.update(over)
    obj.cfg = cfg
    obj.self_model = SelfModel(tmp, cfg)
    obj.context = _FakeCtx(reply)
    obj._int_cfg = types.MethodType(MAIN.MindEngine._int_cfg, obj)
    obj.__dict__["_daily_at"] = 0.0
    return obj


def _seed(sm, n=2):
    for i in range(n):
        sm.enqueue({"claim": f"素材{i}", "kind": "trust",
                    "scope": "user:3388708615", "evidence": ["他说难受"],
                    "relevance": 0.9, "keywords": ["难受"]})


PAYLOAD = "```json\n" + json.dumps({
    "beliefs": [{"claim": "他难过的时候会来找我", "kind": "trust",
                 "scope": "user:3388708615", "confidence": 0.9}]
}, ensure_ascii=False) + "\n```"


class TestDailyPipeline(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_full_pipeline_writes_belief_and_clears_queue(self):
        obj = _mk_plugin(self.d, PAYLOAD)
        _seed(obj.self_model, 3)
        asyncio.run(obj._run_daily_beliefs())

        self.assertEqual(obj.context.calls, 1, "应当只调一次模型")
        rows = obj.self_model.review(10)
        self.assertEqual(len(rows), 1, "应当写入 1 条信念")
        self.assertEqual(rows[0]["status"], "hold", "daily 产出一律 hold")
        self.assertEqual(obj.self_model.pending(max_age_days=2), [], "队列应当清空")
        self.assertGreater(obj.__dict__.get("_daily_at", 0), 0, "应当记下本次时间")

    def test_interval_blocks_second_run(self):
        obj = _mk_plugin(self.d, PAYLOAD)
        _seed(obj.self_model, 1)
        asyncio.run(obj._run_daily_beliefs())
        self.assertEqual(obj.context.calls, 1)
        _seed(obj.self_model, 1)
        asyncio.run(obj._run_daily_beliefs())      # 6 小时内不再跑
        self.assertEqual(obj.context.calls, 1, "间隔内不许重复调用模型")
        self.assertEqual(len(obj.self_model.pending(max_age_days=2)), 1, "新素材还留着")

    def test_broken_reply_keeps_queue(self):
        obj = _mk_plugin(self.d, "我瞎说一句没有 JSON")
        _seed(obj.self_model, 2)
        asyncio.run(obj._run_daily_beliefs())
        self.assertEqual(len(obj.self_model.pending(max_age_days=2)), 2,
                         "解析失败必须保留队列")
        self.assertGreater(obj.__dict__.get("_daily_at", 0.0), 0.0,
                           "解析失败也要记尝试时间（防每条消息重试）")
        self.assertEqual(obj.__dict__.get("_daily_fails"), 1, "失败计数应当 +1")

    def test_off_mode_does_nothing(self):
        obj = _mk_plugin(self.d, PAYLOAD, mode="off")
        _seed(obj.self_model, 2)
        asyncio.run(obj._run_daily_beliefs())
        self.assertEqual(obj.context.calls, 0, "off 档不该调模型")
        self.assertEqual(obj.self_model.review(10), [])

    def test_paused_does_not_write(self):
        obj = _mk_plugin(self.d, PAYLOAD)
        _seed(obj.self_model, 2)
        obj.self_model.pause()
        asyncio.run(obj._run_daily_beliefs())
        self.assertEqual(obj.self_model.review(10), [], "暂停后不许写入")


if __name__ == "__main__":
    unittest.main()
