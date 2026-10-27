"""不启动 AstrBot，直接跑 _ability_block，看它到底拼出什么、打什么日志。

跑法（要用 AstrBot 那个 python）：
    cd D:/qqaibot/astrbot
    set PYTHONIOENCODING=utf-8
    "C:/Users/33887/AppData/Roaming/uv/tools/astrbot/Scripts/python.exe" \
        data/plugins/astrbot_plugin_persona_mind/tools/test_ability_block.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]  # <AstrBot>
sys.path.insert(0, str(ROOT / "data" / "plugins"))
sys.dont_write_bytecode = True

import astrbot_plugin_persona_mind.main as M  # noqa: E402

# 别的插件要注册动作才有 ACTIONS；这里手动喂一份，模拟「qzone 关着」的真实状态
M.kit.ACTIONS.clear()
for verb in ("搜", "看视频", "读网页", "截图", "说", "画", "认人", "发评论", "问妈妈"):
    M.kit.ACTIONS[verb] = {"handler": lambda self, **kw: "", "desc": ""}


class Probe:
    cfg = {"tool_guide_enable": True, "api_spec_enable": True}
    _SPEC_STATE_MAX = M.MindEngine._SPEC_STATE_MAX
    _URL_RE = M.MindEngine._URL_RE
    _PROMISE_RE = M.MindEngine._PROMISE_RE
    _data_root = staticmethod(M.MindEngine._data_root)
    _spec_state = M.MindEngine._spec_state
    _has_target = M.MindEngine._has_target
    _caps_lines = M.MindEngine._caps_lines
    _ability_block = M.MindEngine._ability_block

    def _mom_note(self):
        return ""


def ev(admin: bool, comps=(), umo="test:probe"):
    return SimpleNamespace(
        is_admin=lambda: admin,
        unified_msg_origin=umo,
        message_obj=SimpleNamespace(message=list(comps)),
    )


CASES = [
    ("看看这个 BV1jhn16GEcv 讲了啥", True, ()),
    ("你搜搜看，一首关于鲸鱼的歌", True, ()),
    ("帮我画一只猫", True, ()),
    ("你能在这条底下评论一句吗", True, ("BV1tjnh6sEzW",)),
    ("那你说说为什么", True, ()),
]


def main() -> int:
    p = Probe()
    for i, (text, admin, comps) in enumerate(CASES):
        # 每条用独立会话，否则上一条的「会话粘性」会让后面全部命中同一批动词
        blk = p._ability_block(ev(admin, comps, umo=f"test:case{i}"), text)
        print(f"\n>>> {text!r}  admin={admin}  →  {len(blk)} 字")
        if not blk:
            print("    （空）")
    print("\n--- 一段完整的输出（『那你说说为什么』，应当没有格式行）---")
    print(p._ability_block(ev(True, (), umo="test:show"), "那你说说为什么"))
    print("\n--- 一段完整的输出（看视频，应当只有一行格式）---")
    print(p._ability_block(ev(True, (), umo="test:show2"), "看看这个 BV1jhn16GEcv 讲了啥"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
