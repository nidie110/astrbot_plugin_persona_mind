"""内心引擎 · AstrBot 插件入口。

它做的事只有一件：**在她开口之前，先替她想一遍。**
想的结果不发送，只拼进 system prompt，影响语气、态度和分寸。

分层见 engine/ 目录；本文件只负责接线（配置、钩子、指令、持久化）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

from .engine import (
    Affect,
    EMOTION_LABELS,
    INTENT_NAMES,
    KnowledgeBase,
    ProfileStore,
    THINK_PROMPT,
    VoiceCorpus,
    apply_overrides,
    line_budget,
    parse_json_block,
    render,
    scores,
    summary_line,
    thought_to_effects,
    top_two,
)

PLUGIN_NAME = "astrbot_plugin_persona_mind"

DEFAULTS = {
    "enable": True,
    "think_provider": "",
    "think_timeout": 15,
    "group_light": True,
    "max_inject_chars": 1200,
    "log_enable": True,
    "profile_enable": True,
    "summarize_every": 20,
    "kb_enable": True,
    "voice_enable": True,
    "decay_minutes": 5.0,
    "decay_ratio": 0.2,
    "persona_o": 75,
    "persona_c": 35,
    "persona_e": 55,
    "persona_a": 40,
    "persona_n": 60,
    "affect_p": 60,
    "affect_ar": 45,
    "affect_d": 50,
    "rel_t": 55,
    "rel_i": 40,
}

VOICE_TEMPLATE = """# 语料库模板
# 用法：每个 ## 是一个分组，下面每行一句「她真的会这么说」的话。
# 引擎会按当前意图（安慰/损人/自述/追问/敷衍）自动抽对应分组。

## 关心
你没事吧？
累了就歇着，别硬撑
别熬夜，真的

## 怼回去
哈？就这？
你很嚣张嘛
我可不吃这套

## 说自己的日子
今天有点忙，刚歇下来
我这边看不到外面，只能听你说
诶 我跟你说个事

## 追问
然后呢
说清楚点
为什么呀

## 敷衍
嗯
就这样？
哦

## 亲近
你刚才那句，很好听
过来点，我有话说
"""

KB_TEMPLATE = """# 知识库示例

> 把「她应该知道的东西」写在这里：术语、梗、圈内黑话、角色设定。
> 引擎只在话题命中时注入，最多 3 段。

## 术语
- 例子：把你们圈子常用的说法列在这里，她会用行话，而不是外行话。

## 人物与关系
- 例子：谁是老板、谁不能提、谁的外号是什么。

## 边界
- 不懂的东西就说不懂，别硬编。
"""


@register(
    PLUGIN_NAME,
    "内心引擎",
    "先想再说：大五人格 + OCC 情绪评价 + PAD 衰减 + ToM 情景推断 + 五意图 + 人物侧写 + 本地知识库/语料库",
    "1.0.0",
)
class MindEngine(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        if isinstance(config, dict):
            for k, v in config.items():
                if v is not None and v != "":
                    self.cfg[k] = v

        self.dir = Path("data/plugin_data") / PLUGIN_NAME
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "kb").mkdir(exist_ok=True)
        (self.dir / "profiles").mkdir(exist_ok=True)
        self._seed_templates()

        # 状态与组件
        self.state_path = self.dir / "state.json"
        self.affect = Affect()
        self._load_state()

        self.profiles = ProfileStore(self.dir / "profiles", self.cfg["rel_t"], self.cfg["rel_i"])
        self.kb = KnowledgeBase(self.dir / "kb", self._extra_kb_files())
        if self.cfg.get("kb_enable", True):
            try:
                self.kb.load()
            except Exception as e:
                logger.warning(f"[mind] 知识库加载失败: {e}")

        voice_path = self.dir / "kb" / "voice.md"
        self.voice = VoiceCorpus(voice_path)

        self._inflight: set[str] = set()
        self._log_path = self.dir / "mind.log"

        logger.info(
            "[mind] 已启动 · 知识 %d 段 · 语料 %d 句 · 人格 O%d/C%d/E%d/A%d/N%d"
            % (
                len(self.kb.chunks),
                self.voice.count(),
                self.cfg["persona_o"],
                self.cfg["persona_c"],
                self.cfg["persona_e"],
                self.cfg["persona_a"],
                self.cfg["persona_n"],
            )
        )

    # ---------- 初始化辅助 ----------

    def _seed_templates(self) -> None:
        voice = self.dir / "kb" / "voice.md"
        if not voice.exists():
            voice.write_text(VOICE_TEMPLATE, encoding="utf-8")
        kb = self.dir / "kb" / "knowledge.md"
        if not kb.exists():
            kb.write_text(KB_TEMPLATE, encoding="utf-8")

    def _extra_kb_files(self) -> list[str]:
        raw = self.cfg.get("extra_kb_files") or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        return [str(x) for x in raw]

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.affect.load(data.get("affect") or {})
        except Exception:
            pass
        self.affect.base = {
            "P": float(self.cfg["affect_p"]),
            "Ar": float(self.cfg["affect_ar"]),
            "D": float(self.cfg["affect_d"]),
        }

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps({"affect": self.affect.snapshot()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[mind] 状态保存失败: {e}")

    def _log(self, row: dict) -> None:
        if not self.cfg.get("log_enable", True):
            return
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---------- 主钩子 ----------

    @filter.on_llm_request()
    async def before_llm(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.cfg.get("enable", True):
            return
        msg = (event.message_str or "").strip()
        if not msg or msg.startswith("/"):
            return
        uid = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not uid or uid in self._inflight:
            return
        self._inflight.add(uid)
        try:
            await self._inject(event, req, uid, msg)
        except Exception as e:
            logger.warning(f"[mind] 注入失败: {e}")
        finally:
            self._inflight.discard(uid)

    async def _think(self, msg: str, recent: str, profile_line: str) -> dict:
        prompt = THINK_PROMPT.format(
            now=datetime.now().strftime("%m-%d %H:%M"),
            msg=msg[:200],
            recent=recent[:200] or "（无）",
            profile=profile_line or "（还不了解）",
        )
        kwargs = {"prompt": prompt}
        if self.cfg.get("think_provider"):
            kwargs["chat_provider_id"] = self.cfg["think_provider"]
        resp = await asyncio.wait_for(
            self.context.llm_generate(**kwargs), timeout=float(self.cfg["think_timeout"])
        )
        return parse_json_block(getattr(resp, "completion_text", "") or "")

    async def _inject(self, event: AstrMessageEvent, req: ProviderRequest, uid: str, msg: str):
        self.affect.decay(float(self.cfg["decay_minutes"]), float(self.cfg["decay_ratio"]))

        umo = str(getattr(event, "unified_msg_origin", ""))
        scene = "group" if "group" in umo.lower() else "private"
        profile = self.profiles.load(uid)
        pline = summary_line(profile) if self.cfg.get("profile_enable", True) else ""
        recent = "／".join(profile.get("recent", [])[-2:])

        thought: dict = {}
        try:
            thought = await self._think(msg, recent, pline)
        except Exception as e:
            logger.warning(f"[mind] 思考失败（降级为直接回复）: {e}")

        emotion, effects = thought_to_effects(thought)
        self.affect.apply(effects)

        persona = {
            "O": self.cfg["persona_o"],
            "C": self.cfg["persona_c"],
            "E": self.cfg["persona_e"],
            "A": self.cfg["persona_a"],
            "N": self.cfg["persona_n"],
        }
        relation = {"T": profile.get("T", self.cfg["rel_t"]), "I": profile.get("I", self.cfg["rel_i"])}
        intent_scores = scores(
            persona=persona,
            affect=self.affect.as_dict(),
            relation=relation,
            scene=scene,
            msg=msg,
        )
        intent_scores = apply_overrides(intent_scores, msg)
        lead = top_two(intent_scores)
        mentioned = any(w in msg for w in ("@", "你", "在吗"))
        budget = line_budget(dict(lead), mentioned, scene)

        kb_hits: list[str] = []
        if self.cfg.get("kb_enable", True):
            try:
                self.kb.reload_if_changed()
                kb_hits = self.kb.query(msg, k=3)
            except Exception as e:
                logger.warning(f"[mind] 知识检索失败: {e}")

        voice_lines: list[str] = []
        if self.cfg.get("voice_enable", True) and lead:
            voice_lines = self.voice.pick(lead[0][0], 3)

        if self.cfg.get("group_light", True) and scene == "group":
            pline = ""  # 群聊轻量版：不注入侧写，避免过度解读
            voice_lines = voice_lines[:1]

        block = render(
            affect=self.affect.as_dict(),
            emotion=emotion,
            thought=thought,
            profile_line=pline,
            intents=intent_scores,
            lead=lead,
            budget=budget,
            recent=profile.get("recent", []),
            kb_hits=kb_hits,
            voice_lines=voice_lines,
            scene=scene,
            max_chars=int(self.cfg["max_inject_chars"]),
        )
        req.system_prompt = (req.system_prompt or "") + block

        # 关系推进 + 侧写样本
        profile = self.profiles.touch(uid, msg)
        self._save_state()
        self._log(
            {
                "t": datetime.now().strftime("%m-%d %H:%M"),
                "uid": uid,
                "scene": scene,
                "emotion": emotion,
                "affect": self.affect.as_dict(),
                "thought": thought,
                "intent": intent_scores,
                "budget": budget,
                "kb": len(kb_hits),
            }
        )

        every = int(self.cfg.get("summarize_every") or 0)
        if self.cfg.get("profile_enable", True) and every > 0 and profile["count"] % every == 0:
            asyncio.create_task(self._summarize(uid))

    async def _summarize(self, uid: str) -> None:
        try:
            prompt = self.profiles.summarize_prompt(uid)
            kwargs = {"prompt": prompt}
            if self.cfg.get("think_provider"):
                kwargs["chat_provider_id"] = self.cfg["think_provider"]
            resp = await asyncio.wait_for(self.context.llm_generate(**kwargs), timeout=25)
            patch = parse_json_block(getattr(resp, "completion_text", "") or "")
            if patch:
                self.profiles.merge_patch(uid, patch)
                logger.info(f"[mind] 已更新 {uid} 的侧写")
        except Exception as e:
            logger.warning(f"[mind] 侧写归纳失败: {e}")

    # ---------- 指令 ----------

    @filter.command("mind")
    async def cmd_mind(self, event: AstrMessageEvent):
        """查看/操作内心引擎：/mind [status|profile|kb|toggle|selftest]"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能用")
            return
        raw = (event.message_str or "").split(maxsplit=1)
        sub = (raw[1].strip().lower() if len(raw) > 1 else "status") or "status"
        uid = str(event.get_sender_id() or "")

        if sub in ("toggle", "开关"):
            self.cfg["enable"] = not self.cfg.get("enable", True)
            yield event.plain_result(f"内心引擎：{'开' if self.cfg['enable'] else '关'}")
            return

        if sub in ("profile", "侧写"):
            p = self.profiles.load(uid)
            lines = [
                f"称呼 {p.get('称呼') or '—'}｜信任 {p.get('T')}｜亲密 {p.get('I')}｜聊过 {p.get('count')} 轮"
            ]
            for key in ("身份", "作息", "在做什么"):
                if p.get(key):
                    lines.append(f"{key}：{p[key]}")
            for key in ("兴趣", "雷点", "note"):
                if p.get(key):
                    lines.append(f"{key}：{'、'.join(map(str, p[key][-6:]))}")
            if p.get("近况"):
                lines.append(
                    "近况：" + "；".join(f"{x.get('时间','')} {x.get('事','')}" for x in p["近况"][-3:])
                )
            yield event.plain_result("\n".join(lines))
            return

        if sub in ("kb", "知识"):
            hits = self.kb.query((raw[1].strip() if len(raw) > 1 else "测试"), 3)
            yield event.plain_result(
                f"知识库 {len(self.kb.chunks)} 段，语料 {self.voice.count()} 句"
                + ("\n\n" + "\n\n".join(h[:180] for h in hits) if hits else "")
            )
            return

        if sub in ("selftest", "自检"):
            checks = [
                ("配置", True, ""),
                ("知识库", len(self.kb.chunks) > 0, f"{len(self.kb.chunks)} 段"),
                ("语料库", self.voice.count() > 0, f"{self.voice.count()} 句"),
                ("侧写目录", (self.dir / "profiles").is_dir(), str(self.dir / "profiles")),
                ("状态文件", True, str(self.state_path)),
            ]
            lines = [("✓" if ok else "✗") + f" {name} — {detail}" for name, ok, detail in checks]
            yield event.plain_result("\n".join(lines))
            return

        a = self.affect.as_dict()
        p = self.profiles.load(uid)
        yield event.plain_result(
            f"人格 O{self.cfg['persona_o']} C{self.cfg['persona_c']} E{self.cfg['persona_e']} "
            f"A{self.cfg['persona_a']} N{self.cfg['persona_n']}\n"
            f"情绪 愉悦{a['P']:.0f} 唤醒{a['Ar']:.0f} 底气{a['D']:.0f}\n"
            f"和你的关系 信任{p.get('T')} 亲密{p.get('I')} 聊过{p.get('count')}轮\n"
            f"知识 {len(self.kb.chunks)} 段 · 语料 {self.voice.count()} 句"
        )
