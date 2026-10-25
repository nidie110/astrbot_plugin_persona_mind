"""内心引擎 · AstrBot 插件入口。

它做的事只有一件：**在她开口之前，先替她想一遍。**
想的结果不发送，只拼进 system prompt，影响语气、态度和分寸。

分层见 engine/ 目录；本文件只负责接线（配置、钩子、指令、持久化）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

try:  # AstrBot >= 4.11 才有；老版本退回 system_prompt 注入
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore[assignment]

from .engine import (
    Affect,
    Mood,
    base_from_field,
    EMOTION_LABELS,
    INTENT_NAMES,
    Diary,
    ClaimStore,
    SelfModel,
    KnowledgeBase,
    parse_front_matter,
    ProfileStore,
    THINK_PROMPT,
    VoiceCorpus,
    tier_of,
    AffectionBridge,
    act_brief,
    act_decide,
    apply_overrides,
    line_budget,
    normalize_appraisal_number,
    normalize_user_mood,
    num_or,
    clamp100,
    parse_json_block,
    render,
    feel_line,
    scores,
    summary_line,
    thought_to_effects,
    top_two,
)

# act 与语料分组的映射：新动作名 → 旧语料库的组名
VOICE_GROUP = {
    "answer": None,
    "care": "care",
    "share": "share",
    "tease": "tease",
    "probe": "probe",
    "dodge": "dodge",
    "coy": "tease",
    "seek": "care",
}

PLUGIN_NAME = "astrbot_plugin_persona_mind"

DEFAULTS = {
    "enable": True,
    "field_enable": True,
    "field_strength": 1.0,
    "think_provider": "",
    "think_timeout": 15,
    "group_light": True,
    "max_inject_chars": 1200,
    "log_enable": True,
    "profile_enable": True,
    "summarize_every": 20,
    "kb_enable": True,
    "voice_enable": True,
    "affection_bridge": True,
    "extra_kb_files": [],
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
    # 1.2.2 信任 T 的动力学：此前 T 恒等于初始值，关系只有一个轴在动
    "rel_trust_day_gain": 0.4,      # 隔天还来一次 +0.4
    "rel_trust_share_gain": 0.1,    # 私聊里愿意多讲（≥20 字）+0.1
    "rel_trust_penalty": 0.8,       # 被攻击 -0.8
    "rel_trust_cap_day": 1.5,       # 每日总上限，防刷屏
    "drive_source": "internal",
    "chars_per_line": 15,
    "diary_enable": True,
    "diary_lookback": 3,
    "diary_keep_days": 30,
    "diary_timeout": 60,
    "diary_mood_recall": True,
    "diary_material_path": "",
    # 1.2.0 定时日记：到点就写当天，没素材就跳过（不写空日记）
    "diary_at": "22:40",
    "diary_record_chat": True,
    "diary_chat_gap": 900,
    # 与 schema 对齐（此前只在 schema 里有，代码靠兜底默认值）
    "evolution_daily_interval_hours": 6,
    "evolution_daily_timeout": 30,
    "claims_enable": True,
    "claims_recent": False,
    # 自我模型（1.2.0）：默认 rule 档——复用已有判读，不新增 LLM 调用
    "evolution_mode": "rule",
    "evolution_max_candidates_per_day": 3,
    "evolution_max_new_beliefs_per_day": 1,
    "evolution_relevance_percentile": 75,
    "evolution_confirm_window_days": 7,
    "evolution_confirm_count": 3,
    "evolution_max_history_days": 90,
    "evolution_snapshot_keep_days": 30,
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
    "先想再说：双层 PAD + 八动作七口吻 + 人物侧写 + 本地知识库/语料库",
    "1.1.2",
)
class MindEngine(Star):
    @property
    def mood(self) -> Mood:
        """双层情绪。若实例被绕过 __init__ 构造（测试/热重载），就地补一个。"""
        m = self.__dict__.get("_mood")
        if m is None:
            m = Mood()
            old = self.__dict__.get("affect")
            if isinstance(old, Affect):
                m.core = old
            self.__dict__["_mood"] = m
            self.affect = m.core
        return m

    @mood.setter
    def mood(self, value: Mood) -> None:
        self.__dict__["_mood"] = value
        self.affect = value.core

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
        self._load_traits()

        # 状态与组件
        self.state_path = self.dir / "state.json"
        self.mood = Mood()
        self.affect = self.mood.core  # 兼容旧读法
        self._load_state()

        self.profiles = ProfileStore(
            self.dir / "profiles",
            self.cfg["rel_t"],
            self.cfg["rel_i"],
            trust_day_gain=float(num_or(self.cfg.get("rel_trust_day_gain"), 0.4)),
            trust_share_gain=float(num_or(self.cfg.get("rel_trust_share_gain"), 0.1)),
            trust_penalty=float(num_or(self.cfg.get("rel_trust_penalty"), 0.8)),
            trust_cap_day=float(num_or(self.cfg.get("rel_trust_cap_day"), 1.5)),
        )
        self._auto_provider_id: str | None = None  # 仅用于记录选中项，不缓存不可用结果
        self.last_decision: dict = {}  # {umo: 快照}，供 /mind why 诊断（按会话隔离）
        # 按会话分别记「连续装死」次数：umo -> [次数, 最后使用时间]
        self._dodge_streaks: dict[str, list] = {}
        self._last_cost: dict = {}  # 上一轮思考的开销（耗时/token），写进 mind.log
        self._rel_hist: dict[str, list[float]] = self._rel_hist_load()  # 按场景分桶（含历史）
        self.kb = KnowledgeBase(self.dir / "kb", self._extra_kb_files())
        # 隔壁情绪管理插件的数值（读文件，读不到就静默降级）
        self.bridge = AffectionBridge(
            Path("data/plugin_data") / "astrbot_plugin_affection",
            enabled=bool(self.cfg.get("affection_bridge", True)),
        )
        if self.cfg.get("kb_enable", True):
            try:
                self.kb.load()
            except Exception as e:
                logger.warning(f"[mind] 知识库加载失败: {e}")

        voice_path = self.dir / "kb" / "voice.md"
        self.voice = VoiceCorpus(voice_path)

        # 日记：素材由 video 插件写、这里补写；写完落进 kb/diary/ 参与检索
        self.diary = Diary(self.dir, self.dir / "kb", keep_days=int(self.cfg.get("diary_keep_days") or 30))

        # 自我模型：把经历沉淀成「我怎么看他」「我是什么样的人」。
        # 与 PAD（分钟级）、profiles（事实级）分工，见 engine/self_model.py 文件头。
        self.self_model = SelfModel(self.dir, self.cfg)
        self.claims = ClaimStore(self.dir / "shared" / "claims.jsonl", current_surface="qq")
        self._diary_task: asyncio.Task | None = None
        self._diary_last_check = ""  # 每天只检查一次补写
        self._diary_loop_task: asyncio.Task | None = None  # 固定时间写日记的后台任务
        self._diary_writing = False  # 同一时刻只写一篇，避免定时器与手动命令撞车

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

    TRAITS_SEED = r'''{
  "_note": "人格正本：引擎读这里，改这个文件比重装插件安全。插件配置里的同名字段会被这里覆盖。",
  "_version": 1,
  "big5": { "O": 75, "C": 35, "E": 55, "A": 40, "N": 60 },
  "temper": {
    "play_bias": 0.2,
    "tease_base": 0.3,
    "care_base": 0.55,
    "share_base": 0.25
  },
  "limits": {
    "max_lines_private": 4,
    "max_lines_group": 3,
    "chars_per_line": 15
  },
  "taboo": ["大肥鱼", "胖", "废物", "没用"],
  "voice": { "max_tts_chars": 40 }
}
'''

    def _load_traits(self) -> None:
        """读人格正本 persona.traits.json，覆盖引擎参数。

        为什么要有这个文件：她的性格数值原来散在三处（人格稿正文、插件配置、
        state.json 运行时值），改一处另外两处不动就会打架。这里定一个唯一正本，
        配置项退化成「临时覆盖」。
        """
        path = self.dir / "persona.traits.json"
        if not path.is_file():
            # 首次运行：把「当前生效的配置」落成正本，而不是照抄固定种子。
            # 否则用户改过的面板值会在下一次启动时被种子的默认值悄悄覆盖（复核复现：10/36 -> 75/15）。
            try:
                seed = json.loads(self.TRAITS_SEED)
                seed["big5"] = {
                    "O": int(num_or(self.cfg.get("persona_o"), 75)),
                    "C": int(num_or(self.cfg.get("persona_c"), 35)),
                    "E": int(num_or(self.cfg.get("persona_e"), 55)),
                    "A": int(num_or(self.cfg.get("persona_a"), 40)),
                    "N": int(num_or(self.cfg.get("persona_n"), 60)),
                }
                seed["limits"]["chars_per_line"] = int(num_or(self.cfg.get("chars_per_line"), 15))
                seed["limits"]["max_lines_private"] = int(num_or(self.cfg.get("max_lines_private"), 4))
                seed["limits"]["max_lines_group"] = int(num_or(self.cfg.get("max_lines_group"), 3))
                if self.cfg.get("play_bias") is not None:
                    seed["temper"]["play_bias"] = round(float(num_or(self.cfg.get("play_bias"), 0.2)), 3)
                path.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning(f"[mind] 人格正本初始化失败: {e}")
            # 写成功后立即走同一套校验，首次启动与重启的有效值保持一致。
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[mind] 人格正本读取失败，沿用配置: {e}")
            return
        if not isinstance(data, dict):
            return

        big5 = data.get("big5") or {}
        for short, key in (("O", "persona_o"), ("C", "persona_c"), ("E", "persona_e"),
                           ("A", "persona_a"), ("N", "persona_n")):
            v = num_or(big5.get(short), None) if isinstance(big5, dict) else None
            if v is not None:
                self.cfg[key] = clamp100(v)

        temper = data.get("temper") or {}
        if isinstance(temper, dict):
            v = num_or(temper.get("play_bias"), None)
            if v is not None:
                self.cfg["play_bias"] = max(0.0, min(1.0, v))

        limits = data.get("limits") or {}
        if isinstance(limits, dict):
            v = num_or(limits.get("chars_per_line"), None)
            if v is not None:
                self.cfg["chars_per_line"] = max(5, min(200, int(v)))
            v = num_or(limits.get("max_lines_private"), None)
            if v is not None:
                self.cfg["max_lines_private"] = max(1, min(10, int(v)))
            v = num_or(limits.get("max_lines_group"), None)
            if v is not None:
                self.cfg["max_lines_group"] = max(1, min(10, int(v)))

        taboo = data.get("taboo")
        if isinstance(taboo, list) and taboo:
            self.cfg["taboo_words"] = [str(x) for x in taboo if str(x).strip()]

        logger.info(
            f"[mind] 人格正本已加载：O{self.cfg['persona_o']:.0f}/C{self.cfg['persona_c']:.0f}"
            f"/E{self.cfg['persona_e']:.0f}/A{self.cfg['persona_a']:.0f}/N{self.cfg['persona_n']:.0f}"
            f" · 每条≤{int(self.cfg.get('chars_per_line', 15))}字"
            f" · 条数 {int(self.cfg.get('max_lines_group', 3))}群/{int(self.cfg.get('max_lines_private', 4))}私"
        )

        voice = data.get("voice") or {}
        if isinstance(voice, dict):
            v = num_or(voice.get("max_tts_chars"), None)
            if v is not None:
                self.cfg["max_tts_chars"] = max(10, min(300, int(v)))

    def _load_state(self) -> None:
        # 旧格式没有参照基线，只能以配置基线解释；新格式的保存基线必须保留。
        base = {
            "P": clamp100(num_or(self.cfg.get("affect_p"), 60.0)),
            "Ar": clamp100(num_or(self.cfg.get("affect_ar"), 45.0)),
            "D": clamp100(num_or(self.cfg.get("affect_d"), 50.0)),
        }
        self.mood.rebase(base)
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state.json must contain an object")
            if data.get("mood"):
                self.mood.load_state(data["mood"])
            else:
                self.mood.load_state({"core": data.get("affect") or {}})
            self._record_success("load_state")
        except FileNotFoundError:
            pass
        except Exception as e:
            self._record_failure("load_state", e)
        if not self.cfg.get("field_enable", True) or num_or(self.cfg.get("field_strength"), 1.0) <= 0:
            self.mood.rebase(base)

    def _own_pad(self) -> dict:
        """只读自身心情：固定配置参照，不借用上一位用户的关系基线或场景残留。"""
        defaults = {"P": ("affect_p", 60.0), "Ar": ("affect_ar", 45.0), "D": ("affect_d", 50.0)}
        return {
            name: round(clamp100(
                clamp100(num_or(self.cfg.get(key), default))
                + self.mood.CORE_W * (getattr(self.mood.core, name) - self.mood.core.base[name])
            ), 1)
            for name, (key, default) in defaults.items()
        }

    def _save_state(self) -> None:
        try:
            pending = self.state_path.with_suffix(".json.tmp")
            pending.write_text(
                json.dumps({"mood": self.mood.dump_state()}, ensure_ascii=False),
                encoding="utf-8",
            )
            pending.replace(self.state_path)
            # 对外快照：别的插件/工具（比如主动消息）想读她此刻的心情时读这个文件，
            # 不用去解析 state.json 的内部结构。
            pad = self._own_pad()
            (self.dir / "pad.json").write_text(
                json.dumps(
                    {
                        "P": round(pad.get("P", 0), 1),
                        "Ar": round(pad.get("Ar", 0), 1),
                        "D": round(pad.get("D", 0), 1),
                        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self._record_success("save_state")
        except Exception as e:
            self._record_failure("save_state", e)

    def _log(self, row: dict) -> None:
        if not self.cfg.get("log_enable", True):
            return
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._record_success("log")
        except Exception as e:
            self._record_failure("log", e)

    def _record_failure(self, stage: str, error: Exception) -> None:
        """完整堆栈限频写入 AstrBot 日志，健康状态每次更新；不自动禁用。"""
        health = self.__dict__.setdefault("_health", {})
        row = health.setdefault(stage, {})
        now = time.time()
        row["failures"] = row.get("failures", 0) + 1
        row["consecutive"] = row.get("consecutive", 0) + 1
        row["last_failure"] = now
        row["error"] = type(error).__name__
        if "last_logged" not in row or now - row["last_logged"] >= 60:
            logger.error(f"[mind] {stage} 失败（连续 {row['consecutive']} 次）",
                         exc_info=(type(error), error, error.__traceback__))
            row["last_logged"] = now

    def _record_success(self, stage: str) -> None:
        row = self.__dict__.setdefault("_health", {}).setdefault(stage, {})
        if row.get("consecutive", 0):
            logger.info(f"[mind] {stage} 已恢复")
        row["consecutive"] = 0
        row["last_success"] = time.time()

    def _health_line(self) -> str:
        health = self.__dict__.get("_health") or {}
        if not health:
            return "健康：尚无运行记录"
        failed = [f"{stage}连续失败{row['consecutive']}次（{row.get('error', '未知')}）"
                  for stage, row in health.items() if row.get("consecutive", 0)]
        last = health.get("inject", {}).get("last_success")
        stamp = datetime.fromtimestamp(last).strftime("%m-%d %H:%M:%S") if last else "尚无"
        return "健康：" + ("；".join(failed) if failed else "已执行阶段正常") + f"；最近注入成功 {stamp}"

    @staticmethod
    def _content_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(part.get("text", "") for part in content
                             if isinstance(part, dict) and isinstance(part.get("text"), str))
        return ""

    def _previous_question(self, req, umo: str, uid: str, scene: str, msg: str) -> tuple[str, bool]:
        """只读取真实 assistant 回复；没有历史时不从上一轮 act 猜测。"""
        rows = getattr(req, "contexts", None)
        if not isinstance(rows, list) or not rows:
            rows = getattr(req, "messages", None)
        if not isinstance(rows, list):
            return "", False
        skipped_current = False
        for row in reversed(rows):
            if not isinstance(row, dict):
                continue
            role = row.get("role")
            text = self._content_text(row.get("content")).strip()
            if role == "user":
                # 部分适配器把当前消息也放在 messages 中；只跳过这一个。
                if not skipped_current and text == msg.strip():
                    skipped_current = True
                    continue
                return "", False
            if role == "assistant":
                last = self.last_decision.get(umo) or {}
                addressed = scene == "private" or str(last.get("uid", "")) == uid
                asked = bool(text and re.search(r"[？?]|(?:吗|么|呢)[。！!\s]*$", text))
                return text, bool(addressed and asked)
        return "", False

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
        self.__dict__["_last_cost"] = {}
        self._record_chat_material(msg, str(getattr(event, "unified_msg_origin", "") or ""))
        try:
            await self._inject(event, req, uid, msg)
            self._record_success("inject")
        except Exception as e:
            self._record_failure("inject", e)
        finally:
            self._inflight.discard(uid)

    def _int_cfg(self, key: str, default: int = 0) -> int:
        """读整数配置；缺失/脏值回落默认值（不用 or，避免 0 被当成空）。"""
        raw = self.cfg.get(key)
        if raw is None or raw == "":
            return int(default)
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return int(default)

    def _resolve_provider(self) -> str:
        """决定「先想一下」用哪个模型。

        配置里写了就用配置；没写就自己找一个——优先 deepseek（便宜、够用），
        再退到任意可用 provider。找不到返回空，不缓存失败，下一轮可自动恢复。
        """
        name = str(self.cfg.get("think_provider") or "").strip()
        if name:
            return name
        try:
            provs = self.context.get_all_providers() or []
            picked = ""
            for p in provs:
                cfgp = getattr(p, "provider_config", None) or {}
                pid = str(cfgp.get("id") or "") if isinstance(cfgp, dict) else ""
                if not pid:
                    continue
                if not picked:
                    picked = pid
                if "deepseek" in pid.lower():
                    picked = pid
                    break
            if picked and picked != self.__dict__.get("_auto_provider_id"):
                logger.info(f"[mind] 思考模型自动选中：{picked}")
            self._auto_provider_id = picked
            return picked
        except Exception as e:
            logger.debug(f"[mind] 自动选择 provider 失败: {e}")
        return ""

    # ---------- 日记 ----------

    def _rel_hist_path(self):
        return self.dir / "rel_hist.jsonl"

    def _rel_hist_cutoff(self) -> str:
        days = int(num_or(self.cfg.get("evolution_max_history_days"), 7))
        return (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%d")

    def _rel_hist_load(self) -> dict:
        """从 rel_hist.jsonl 读回 relevance 历史，按 evolution_max_history_days 裁剪。"""
        out: dict[str, list[float]] = {}
        path = self.dir / "rel_hist.jsonl"
        cutoff = self._rel_hist_cutoff()
        try:
            if not path.is_file():
                return out
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                day = str(row.get("d") or "")
                bucket = str(row.get("b") or "")
                val = row.get("v")
                if day < cutoff or not bucket or not isinstance(val, (int, float)) or isinstance(val, bool):
                    continue
                out.setdefault(bucket, []).append(float(val))
            for k in list(out):
                out[k] = out[k][-400:]
        except OSError:
            return {}
        return out

    def _rel_hist_append(self, scene: str, value: float) -> None:
        """追加今天的 relevance。

        每条都落盘：8 条一攒会在进程退出时丢掉最后几条（复审 P2-7），
        而这是 append 小行，开销可忽略。每 200 条顺手裁一次旧数据（P2-4）。
        """
        buf = int(num_or(self.__dict__.get("_rel_buf"), 0)) + 1
        self.__dict__["_rel_buf"] = buf
        try:
            path = self._rel_hist_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {"d": datetime.now().strftime("%Y-%m-%d"), "b": scene, "v": round(float(value), 3)}
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if buf % 200 == 0:
                self._rel_hist_prune()
        except OSError:
            return

    def _rel_hist_prune(self) -> None:
        """文件太大时按天裁剪一次（保留配置的天数）。"""
        try:
            path = self._rel_hist_path()
            if not path.is_file() or path.stat().st_size < 120_000:
                return
            cutoff = self._rel_hist_cutoff()
            keep = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and str(row.get("d") or "") >= cutoff:
                    keep.append(line)
            path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        except OSError:
            return

    def _relevance_cut(self, scene: str = "private") -> float:
        """当天 relevance 的分位数，作为自我模型筛选候选的相对阈值。

        绝对阈值不靠谱：她几乎每条消息的 relevance 都在 0.6~0.9（群里被点名、
        私聊对话），按 0.65 卡等于全收。用相对分位能自适应「今天聊得热不热闹」。

        按场景分桶：群聊 relevance 系统性低于私聊（act.py 自己就用 0.35 分群聊），
        混在一起算会让分位数偏移（专家建议 D）。
        """
        pct = num_or(self.cfg.get("evolution_relevance_percentile"), 75)
        hist = self.__dict__.get("_rel_hist") or {}
        vals = sorted((hist.get(scene) or []))
        if len(vals) < 20:
            return 0.60  # 样本太少时用保守默认，宁严勿松
        idx = min(len(vals) - 1, int(len(vals) * float(pct) / 100.0))
        return float(vals[idx])

    # ---------- 定时写日记 ----------

    _DIARY_TICK_SECONDS = 45  # 45 秒醒一次：够准，又不至于空转

    async def initialize(self) -> None:
        """AstrBot 插件加载钩子：起后台任务，每天 diary_at 写当天日记。"""
        if not self.cfg.get("diary_enable", True):
            return
        if self.__dict__.get("_diary_loop_task") is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._diary_loop_task = loop.create_task(self._diary_loop())
        logger.info("[mind] 日记定时器已启动 · 每天 %s", self.cfg.get("diary_at") or "—")
        # 开机先看一遍：有漏写的日子立刻补，不等下一次开口
        loop.create_task(self._diary_tick())

    def _diary_at_minutes(self) -> int | None:
        """把 diary_at 解析成「当天第几分钟」；空值或脏值返回 None（= 不定时）。"""
        raw = str(self.cfg.get("diary_at") or "").strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", raw):
            return None
        try:
            hh, mm = (int(x) for x in raw.split(":"))
        except ValueError:
            return None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return hh * 60 + mm

    async def _diary_loop(self) -> None:
        """常驻循环：到点写今天的日记，顺手补上漏掉的几天。"""
        while True:
            try:
                await asyncio.sleep(self._DIARY_TICK_SECONDS)
                await self._diary_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 定时器绝不能把自己搞死
                logger.debug(f"[mind] 日记定时器异常: {type(e).__name__}: {e}")

    async def _diary_tick(self) -> None:
        if not self.cfg.get("diary_enable", True):
            return
        if self.__dict__.get("_diary_writing"):
            return
        days = self._diary_due_days()
        if not days:
            return
        self.__dict__["_diary_writing"] = True
        try:
            await self._write_diaries(days)
        finally:
            self.__dict__["_diary_writing"] = False

    def _diary_due_days(self) -> list[str]:
        """今天到点且还没写 → 写今天；往前几天有素材没写 → 补写。"""
        try:
            now = datetime.now()
            today = now.date().isoformat()
            older: list[str] = []
            lookback = self._int_cfg("diary_lookback", 3)
            for day in self.diary.pending_days(lookback=max(1, lookback)):
                if day != today:
                    older.append(day)
            at = self._diary_at_minutes()
            today_due = bool(
                at is not None
                and (now.hour * 60 + now.minute) >= at
                and not self.diary.has_diary(today)
                and self.diary.materials(today)
            )
            # 从旧到新：先把漏掉的补上，最后写今天
            return older + ([today] if today_due else [])
        except Exception:
            return []

    def _record_chat_material(self, msg: str, umo: str) -> None:
        """把当天的对话随手记一条当日记素材——没看视频的日子也有东西可写。"""
        if not self.cfg.get("diary_enable", True):
            return
        if not self.cfg.get("diary_record_chat", True):
            return
        text = (msg or "").strip()
        if len(text) < 6:
            return
        gap = max(60, self._int_cfg("diary_chat_gap", 900))
        now = time.time()
        seen = self.__dict__.setdefault("_chat_mat_at", {})
        try:
            if now - float(seen.get(umo) or 0) < gap:
                return
        except (TypeError, ValueError):
            pass
        seen[umo] = now
        try:
            self.diary.add("chat", {"text": text[:200]}, umo=umo)
        except Exception as e:
            logger.debug(f"[mind] 聊天素材写入失败: {e}")

    def _maybe_write_diary(self) -> None:
        """每天第一次开口时检查一次：有没有前几天的素材等着补写。"""
        if not self.cfg.get("diary_enable", True):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self.__dict__.get("_diary_last_check") == today:
            return
        self.__dict__["_diary_last_check"] = today
        try:
            days = self.diary.pending_days(lookback=int(self.cfg.get("diary_lookback", 3) or 3))
        except Exception:
            return
        if not days:
            return
        task = self.__dict__.get("_diary_task")
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.__dict__["_diary_task"] = loop.create_task(self._write_diaries(days))

    async def _write_diaries(self, days: list[str]) -> None:
        """补写日记：一次最多两篇，失败跳过，绝不把异常抛回聊天链路。"""
        try:
            pid = self._resolve_provider()
        except Exception:
            pid = ""
        if not pid:
            logger.warning("[mind] 日记：没有可用 provider，跳过")
            return

        for day in days[:2]:
            try:
                prompt = self.diary.build_prompt(day)
                resp = await asyncio.wait_for(
                    self.context.llm_generate(chat_provider_id=pid, prompt=prompt),
                    timeout=float(self.cfg.get("diary_timeout", 60) or 60),
                )
                entry = self.diary.parse_reply(getattr(resp, "completion_text", "") or "")
                if not entry:
                    logger.warning(f"[mind] 日记 {day} 生成失败：模型没给出合规 JSON")
                    continue
                mats = self.diary.materials(day)
                sources = [str(m.get("title") or "") for m in mats if m.get("kind") == "video"]
                self.diary.save(day, entry, sources)
                # 日记里形成的判断，进共享判断库（经历留在日记，判断可以跨界面用）
                for c in (entry.get("claims") or [])[:2]:
                    if not isinstance(c, dict):
                        continue
                    self.claims.add(
                        str(c.get("title") or ""),
                        str(c.get("stance") or ""),
                        surface="diary",
                        experience="diary_judgment",
                        kind="topic",
                        confidence=0.75,
                        source="persona_mind_diary",
                        date=day,
                        source_scene="diary",
                        canonical_id="deepseek_xiaojingyu",
                        visibility="shared",
                        material_id=f"diary:{day}:{str(c.get('title') or '').strip()[:60]}",
                    )
                if self.cfg.get("kb_enable", True):
                    try:
                        self.kb.load()
                    except Exception:
                        pass
                logger.info(
                    "[mind] 日记已写入 %s · %s · %d 字 · tags=%s"
                    % (
                        day,
                        entry.get("title") or "",
                        len(entry.get("text") or ""),
                        ",".join(entry.get("tags") or []),
                    )
                )
            except Exception as e:
                logger.warning(f"[mind] 日记 {day} 写入失败: {type(e).__name__}: {e}")

        try:
            self.diary.prune()
        except Exception:
            pass

    async def _think(self, msg: str, recent: str, profile_line: str) -> dict:
        # 内在驱动：让评价从「她自己的状态」出发，而不是纯被动回应。
        # drive_source=external 时退回纯响应式（只读对方的话）。
        if str(self.cfg.get("drive_source") or "internal") == "internal":
            pad = self._own_pad()
            self_state = feel_line(pad, "None") + "，正待在机房这边，没什么特别的事"
        else:
            self_state = "（不特别考虑自己，按对方的话回应）"
        prompt = THINK_PROMPT.format(
            now=datetime.now().strftime("%m-%d %H:%M"),
            msg=msg[:200],
            recent=recent[:200] or "（无）",
            profile=profile_line or "（还不了解）",
            self_state=self_state,
        )
        kwargs = {"prompt": prompt}
        pid = self._resolve_provider()
        if pid:
            kwargs["chat_provider_id"] = pid
        t0 = time.perf_counter()
        # 每轮都重置：上一轮成功、这一轮失败时不能沿用旧 token 数（复核指出）
        self.__dict__["_last_cost"] = {}
        cost: dict = {"think_provider": pid or "default", "ok": False}
        try:
            if not pid:
                raise RuntimeError("没有可用的思考 provider")
            resp = await asyncio.wait_for(
                self.context.llm_generate(**kwargs), timeout=float(self.cfg["think_timeout"])
            )
        except Exception as e:
            cost["think_ms"] = int((time.perf_counter() - t0) * 1000)
            cost["error"] = type(e).__name__
            self.__dict__["_last_cost"] = cost
            raise
        cost["think_ms"] = int((time.perf_counter() - t0) * 1000)
        cost["ok"] = True
        # token 用量：不同 provider 返回字段不一，能拿到就记，拿不到留空
        usage = getattr(resp, "usage", None)
        if isinstance(usage, dict):
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage.get(k), (int, float)):
                    cost[k] = int(usage[k])
        self.__dict__["_last_cost"] = cost
        return parse_json_block(getattr(resp, "completion_text", "") or "")

    async def _inject(self, event: AstrMessageEvent, req: ProviderRequest, uid: str, msg: str):
        umo = str(getattr(event, "unified_msg_origin", ""))
        self.mood.decay(
            num_or(self.cfg.get("decay_minutes"), 5.0),
            num_or(self.cfg.get("decay_ratio"), 0.2),
        )
        scene = "group" if "group" in umo.lower() else "private"
        profile = self.profiles.load(uid)
        pline = summary_line(profile) if self.cfg.get("profile_enable", True) else ""
        # 群聊别人说的话不该当众复述进她的上下文：只保留私聊的近况
        recent = "／".join(profile.get("recent", [])[-2:]) if scene == "private" else ""

        thought: dict = {}
        try:
            thought = await self._think(msg, recent, pline)
            self._record_success("think")
        except Exception as e:
            self._record_failure("think", e)

        emotion, effects = thought_to_effects(thought)
        # 相关性、有利/不利强度与可控性调节冲量；合法的 0 不应被默认值覆盖。
        rel = normalize_appraisal_number(thought.get("relevance"), 0.0, 1.0, 0.5)
        des = normalize_appraisal_number(thought.get("desirability"), -1.0, 1.0, 0.0)
        ctl = normalize_appraisal_number(thought.get("control"), 0.0, 1.0, 0.5)
        # OCC 冲量：relevance 是门槛（不相关 = 不产生情绪），|desirability| 放大强弱。
        # 旧式 0.55 + 0.45·(…) 在 relevance=0 时仍有 0.55 背景冲量，等于「任何消息都能撩到她」。
        # control 暂不参与：它对 Anger/Worry/Guilt 的语义方向不一致（能控制的过错反而更自责），
        # 硬接会在 des=0 处造成约 43% 的强度跳变。等按情绪类别分别校准后再引入。
        intensity = rel * (0.35 + 0.65 * abs(des))
        impulse = {k: v * intensity for k, v in effects.items()}
        if any(v != 0.0 for v in impulse.values()):
            self.mood.apply(impulse, umo=umo)

        persona = {
            "O": self.cfg["persona_o"],
            "C": self.cfg["persona_c"],
            "E": self.cfg["persona_e"],
            "A": self.cfg["persona_a"],
            "N": self.cfg["persona_n"],
        }
        relation = {"T": profile.get("T", self.cfg["rel_t"]), "I": profile.get("I", self.cfg["rel_i"])}
        user_mood = normalize_user_mood(thought.get("user_mood"))
        # mentioned 只对群聊有意义：私聊每句话本来就该接，硬抬 answer 会让
        # 关心/撒娇/逗他全部被"应答"吃掉（标定集实测：私聊 answer 霸榜到 0.79）。
        mentioned = scene == "group" and (("@" in msg) or ("大肥鱼" in msg))
        prev = self._dodge_streaks.get(umo)
        prev_streak = prev[0] if prev else 0
        try:
            self_id = str(event.get_self_id())
        except Exception:
            self_id = ""
        aff_state = self.bridge.get(uid, bot_id=self_id) if uid else {}

        # 势场 → 基线：她对"这个人"的基本态度决定情绪回落到哪
        _fs = num_or(self.cfg.get("field_strength"), 1.0)
        if self.cfg.get("field_enable", True) and _fs > 0:
            # 注意不能用 `x or 50.0`：真实的 0（好感耗尽、攻击性为零）会被当成缺失值。
            _A = num_or(aff_state.get("affection"), 50.0)
            # 桥接给的是她「对他」的两个量（0~50），势场按 0~100 居中 50 计算，所以 ×2。
            # 键名必须是 libido_other / aggression_other：写成 libido / aggression
            # 会一直取不到值，四变量势场实际只有好感与信任在起作用（复核复现）。
            _L = num_or(aff_state.get("libido_other"), 25.0) * 2.0
            _Ag = num_or(aff_state.get("aggression_other"), 25.0) * 2.0
            _T = num_or(relation.get("T"), 50.0)
            self.mood.rebase(base_from_field(_A, _L, _Ag, _T, _fs))
        # 关闭势场时不 rebase：基线保持旧行为（配置在 _load_state 里设好，
        # 或由调用方显式改），避免每轮把别人的改动覆盖掉。

        # v2：act / tone 双通道决策（取代旧的五意图）
        previous_text, previous_asked = self._previous_question(req, umo, uid, scene, msg)
        # 自我模型：从这一轮已有的判读里提取候选（零额外 LLM 调用）
        beliefs: list[str] = []
        try:
            _th = thought if isinstance(thought, dict) else {}
            _rel = num_or(_th.get("relevance"), 0.0)
            _bucket = self.__dict__.setdefault("_rel_hist", {}).setdefault(scene, [])
            _bucket.append(_rel)
            if len(_bucket) > 200:
                del _bucket[:100]
            self._rel_hist_append(scene, _rel)
            _sm = getattr(self, "self_model", None)
            if _sm is None:
                raise RuntimeError("self_model 未初始化")
            cand = _sm.observe(
                thought=_th,
                text=msg,
                uid=uid,
                scene=scene,
                relevance=_rel,
                relevance_cut=self._relevance_cut(scene),
            )
            if cand:
                # 档位分派（daily/deep 先排队，当天统一归纳；rule 当场结算）
                if _sm.mode in ("daily", "deep"):
                    _sm.enqueue(cand)
                else:
                    _sm.apply(cand, "accept")
            beliefs = _sm.brief(uid)
        except Exception as e:
            self._record_failure("self_model", e)
        decision = act_decide(
            persona=persona,
            affect=self.mood.effective(umo),
            relation=relation,
            scene=scene,
            msg=msg,
            prev=previous_text,
            prev_asked=previous_asked,
            user_mood=user_mood,
            mentioned=mentioned,
            play_bias=max(0.0, min(1.0, num_or(self.cfg.get("play_bias"), 0.2))),
            dodge_streak=prev_streak,
            aggression_other=aff_state.get("aggression_other"),
            affection=aff_state.get("affection"),
            libido_other=aff_state.get("libido_other"),
            thought=thought,
            beliefs=beliefs,
        )
        lead = decision["lead"]
        budget = decision["budget"]
        # 条数上限来自人格正本：之前只读进 cfg 却没有消费者（复核指出）
        _cap = num_or(self.cfg.get("max_lines_private" if scene == "private" else "max_lines_group"),
                      4 if scene == "private" else 3)
        if budget > 0 and decision.get("length_mode") != "task":
            budget = max(1, min(int(_cap), budget))
        decision["budget"] = budget
        self.last_decision[umo] = {
            "msg": msg,
            "uid": uid,
            "user_mood": user_mood,
            # 当轮快照：诊断要用这一轮的关系/PAD，而不是命令时点的全局值
            "relation": dict(relation),
            # 存当轮实际生效的 PAD（含 core/scene 混合），不是空会话的偏移表
            "pad": self.mood.effective(umo),
            "aff": dict(aff_state),
            "tier": tier_of(relation),
            "reason": decision.get("reason", ""),
            "acts": dict(decision.get("acts") or {}),
            "act": lead[0][0] if lead else "answer",
            "tone": decision.get("tone", "plain"),
            "budget": budget,
            "length_mode": decision.get("length_mode", "chat"),
            "previous_asked": previous_asked,
            # 这一轮实际带进提示词的信念（诊断看的是「信念有没有推动决策」）
            "beliefs": [
                (b.get("claim") if isinstance(b, dict) else str(b or ""))
                for b in (beliefs or [])
            ],
        }
        if len(self.last_decision) > 300:
            for _k in list(self.last_decision)[: len(self.last_decision) - 250]:
                self.last_decision.pop(_k, None)
        # 连续装死计数按会话隔离：A 群躲两次不该影响 B 群
        self._dodge_streaks[umo] = [
            prev_streak + 1 if budget == 0 else 0,
            time.time(),
        ]
        # 真 LRU：先清计数已归零的会话，再按最久未用淘汰到 300
        if len(self._dodge_streaks) > 400:
            for k in [k for k, v in self._dodge_streaks.items() if v[0] == 0]:
                del self._dodge_streaks[k]
            while len(self._dodge_streaks) > 300:
                oldest = min(self._dodge_streaks, key=lambda k: self._dodge_streaks[k][1])
                del self._dodge_streaks[oldest]
        # 旧的五意图分数仍然算一份，用于日志对照与语料分组
        intent_scores = scores(
            persona=persona,
            affect=self.mood.effective(umo),
            relation=relation,
            scene=scene,
            msg=msg,
            user_mood=user_mood,
        )
        intent_scores = apply_overrides(intent_scores, msg)

        kb_hits: list[str] = []
        diary_hits: list[dict] = []
        if self.cfg.get("kb_enable", True):
            try:
                self.kb.reload_if_changed()
                kb_hits = self.kb.query(msg, k=3)
            except Exception as e:
                logger.warning(f"[mind] 知识检索失败: {e}")

        # 她自己的日记：聊到相关话题时会想起来；心情很低时会想起以前开心的那天
        if self.cfg.get("diary_enable", True):
            try:
                diary_hits = self.kb.query_diary(msg, k=1)
                if not diary_hits and self.cfg.get("diary_mood_recall", True):
                    pad_now = self.mood.effective(umo)
                    if pad_now.get("P", 60) <= 42:
                        diary_hits = self.kb.mood_diary(want_happy=True, k=1)
                    elif pad_now.get("P", 60) >= 72:
                        diary_hits = self.kb.mood_diary(want_happy=False, k=1)
            except Exception as e:
                logger.warning(f"[mind] 日记检索失败: {e}")

        # 跨界面共享的「判断」：只共用看法，不共用经历
        claim_lines: list[str] = []
        if self.cfg.get("claims_enable", True):
            try:
                self.claims.reload_if_changed()
                hits = self.claims.query(msg, k=2)
                if not hits and self.cfg.get("claims_recent", False):
                    hits = self.claims.recent(days=3, k=2)
                claim_lines = [ClaimStore.to_line(c) for c in hits]
            except Exception as e:
                logger.warning(f"[mind] 判断检索失败: {e}")

        # 每天第一次开口：把昨天的日记补上（不阻塞回复）
        try:
            self._maybe_write_diary()
        except Exception as e:
            logger.warning(f"[mind] 日记补写调度失败: {e}")

        lead_act = lead[0][0] if lead else "answer"
        lead_tone = decision.get("tone", "plain")
        voice_lines: list[str] = []
        if self.cfg.get("voice_enable", True) and lead:
            voice_lines = self.voice.pick(
                lead_act,
                3,
                relation=relation,
                tone=lead_tone,
                user_mood=user_mood,
            )

        if self.cfg.get("group_light", True) and scene == "group":
            pline = ""  # 群聊轻量版：不注入侧写，避免过度解读
            voice_lines = voice_lines[:1]
            beliefs = beliefs[:1]  # 群里只留一条：轻量模式不该比私聊还多两行

        block = render(
            affect=self.mood.effective(umo),
            emotion=emotion,
            thought=thought,
            profile_line=pline,
            intents=intent_scores,
            lead=lead,
            budget=budget,
            recent=profile.get("recent", []) if scene == "private" else [],
            kb_hits=kb_hits,
            voice_lines=voice_lines,
            diary_hits=diary_hits,
            claims=claim_lines,
            beliefs=beliefs,
            scene=scene,
            tier=tier_of(relation),
            lead_act=lead_act,
            lead_tone=lead_tone,
            max_chars=int(self.cfg["max_inject_chars"]),
            line_chars=int(self.cfg.get("chars_per_line", 15) or 15),
            decision=decision,
        )
        # 注入方式：不动 system_prompt（会破坏整个会话的前缀缓存，市场审核会拒），
        # 改为往「最后一条用户消息」追加临时内容块——AstrBot 官方推荐路径。
        # 每轮请求的 extra_user_content_parts 都是新列表，不会跨轮累积；
        # 同一轮内若钩子被触发多次，用对象引用去重，绝不按文本切割。
        inject_part = TextPart(text=block) if TextPart is not None else None
        target = getattr(req, "extra_user_content_parts", None)
        if inject_part is not None and isinstance(target, list):
            target[:] = [p for p in target if getattr(p, "_mind_injected", False) is not True]
            try:
                object.__setattr__(inject_part, "_mind_injected", True)
            except Exception:
                pass
            target.append(inject_part)
        else:  # 极老版本 AstrBot 没有这个字段时跳过注入，绝不碰 system_prompt
            logger.warning("[mind] 当前 AstrBot 不支持 extra_user_content_parts，本轮跳过注入")

        # 关系推进 + 侧写样本（关掉 profile_enable 就只读不写，不再偷偷采集）
        if self.cfg.get("profile_enable", True):
            try:
                profile = self.profiles.touch(
                    uid,
                    msg,
                    # 只有真骂才扣信任；开玩笑（tease）不记账
                    negative=(decision.get("bot_affect") == "insult"),
                    scene=scene,
                )
            except Exception as e:  # 档案被手工改坏时不能炸掉这一轮对话
                self._record_failure("profile", e)
                profile = self.profiles.load(uid)
        else:
            profile = self.profiles.load(uid)
        self._save_state()
        self._log(
            {
                "t": datetime.now().strftime("%m-%d %H:%M"),
                "uid": uid,
                "scene": scene,
                "emotion": emotion,
                "affect": self.mood.effective(umo),
                "thought": thought,
                "intent": intent_scores,
                "act": lead[0][0] if lead else None,
                "act_score": round(lead[0][1], 3) if lead else 0,
                "tone": decision.get("tone"),
                "act_reason": decision.get("reason"),
                "bot_affect": decision.get("bot_affect", "none"),
                "secondary_acts": decision.get("secondary_acts", []),
                "length_mode": decision.get("length_mode", "chat"),
                "budget": budget,
                "kb": len(kb_hits),
                "cost": self.__dict__.get("_last_cost") or {},
            }
        )

        if getattr(self.self_model, "mode", "rule") in ("daily", "deep"):
            _t = asyncio.create_task(self._run_daily_beliefs())
            # 保存引用，避免任务被 GC 提前回收
            self.__dict__.setdefault("_bg_tasks", set()).add(_t)
            _t.add_done_callback(self.__dict__["_bg_tasks"].discard)

        every = int(self.cfg.get("summarize_every") or 0)
        if self.cfg.get("profile_enable", True) and every > 0 and profile["count"] % every == 0:
            asyncio.create_task(self._summarize(uid))

    async def _run_daily_beliefs(self) -> None:
        """daily / deep 档：把当天排队的候选交给 LLM 归纳成信念。

        两次归纳至少间隔 evolution_daily_interval_hours（默认 6 小时），
        一次最多 20 条素材，产出 0-3 条，全部 hold 等人工复核——
        不让模型自己给自己发执照。

        并发：入口抢 _daily_busy 标记，await 期间其他消息直接返回，
        避免两条消息同时通过间隔检查造成重复 LLM 调用（专家 P1-4）。
        失败：也记录尝试时间并做指数退避，防止 provider 挂掉时每条消息都重试。
        """
        sm = getattr(self, "self_model", None)
        if sm is None or sm.mode not in ("daily", "deep"):
            return
        if self.__dict__.get("_daily_busy"):
            return                       # 上一轮还没跑完，别叠上来
        interval_h = float(self._int_cfg("evolution_daily_interval_hours", 6) or 6)
        last_at = float(self.__dict__.get("_daily_at", 0.0) or 0.0)
        fails = int(self.__dict__.get("_daily_fails", 0) or 0)
        # 失败退避：连续失败时把间隔按 2^fails 放大（上限 8 倍）
        wait_h = min(interval_h, 1.0) * (2 ** min(fails, 3))
        if time.time() - last_at < wait_h * 3600:
            return
        self.__dict__["_daily_busy"] = True
        try:
            await self._run_daily_beliefs_inner(sm)
        finally:
            self.__dict__["_daily_busy"] = False

    async def _run_daily_beliefs_inner(self, sm) -> None:
        """真正的归纳逻辑（拆出来是为了让外层只管锁与退避）。"""
        fails = int(self.__dict__.get("_daily_fails", 0) or 0)
        # 带上昨天的（整天没消息时不至于把素材永久晾着）
        pend = sm.pending(max_age_days=2)
        if not pend:
            return
        try:
            pid = self._resolve_provider()
            if not pid:
                # 没 provider 不算“今天做过”——不置位，下次还能重试
                logger.info("[mind] daily 归纳跳过：暂无可用 provider")
                return
            prompt = sm.daily_prompt(max_age_days=2)
            resp = await asyncio.wait_for(
                self.context.llm_generate(prompt=prompt, chat_provider_id=pid),
                timeout=float(self.cfg.get("evolution_daily_timeout", 30) or 30),
            )
            data = parse_json_block(getattr(resp, "completion_text", "") or "")
            # 解析失败 ≠ 归纳出 0 条：前者保留队列等下次，后者才算成功
            # 空 dict 也是「没解析出东西」：模型答「我不知道」不能当成功清空队列
            if not isinstance(data, dict) or not isinstance(data.get("beliefs"), list):
                logger.warning("[mind] daily 归纳返回无法解析，队列保留")
                self.__dict__["_daily_at"] = time.time()
                self.__dict__["_daily_fails"] = fails + 1
                return
            items = data.get("beliefs")
            n = sm.apply_daily(items if isinstance(items, list) else [])
            # 只清这一批真正提交过的前 20 条（与 prompt 的 rows[:20] 对齐），
            # await 期间新入队的不动（F02）
            sm.clear_pending([str(r.get("queued_at")) + str(r.get("claim")) for r in pend[:20]])
            self.__dict__["_daily_at"] = time.time()
            self.__dict__["_daily_fails"] = 0        # 成功即清零退避
            logger.info(f"[mind] daily 归纳：{len(pend)} 条素材 → {n} 条信念（待复核）")
        except Exception as e:
            logger.warning(f"[mind] daily 归纳失败: {e}")
            self.__dict__["_daily_at"] = time.time()
            self.__dict__["_daily_fails"] = fails + 1

    async def _summarize(self, uid: str) -> None:
        try:
            prompt = self.profiles.summarize_prompt(uid)
            kwargs = {"prompt": prompt}
            # 无条件解析：配置留空时也要走自动选择。
            # 取不到就明确跳过本次归纳，不要带着必填参数缺失去调用。
            pid = self._resolve_provider()
            if not pid:
                logger.info(f"[mind] 侧写归纳跳过：暂无可用 provider（{uid}）")
                return
            kwargs["chat_provider_id"] = pid
            resp = await asyncio.wait_for(self.context.llm_generate(**kwargs), timeout=25)
            patch = parse_json_block(getattr(resp, "completion_text", "") or "")
            if patch:
                self.profiles.merge_patch(uid, patch)
                logger.info(f"[mind] 已更新 {uid} 的侧写")
        except Exception as e:
            logger.warning(f"[mind] 侧写归纳失败: {e}")

    # ---------- 指令 ----------

    # 范围与枚举（复审 P2-5）：schema 里写过的限制在这里也拦一道
    _CFG_LIMITS = {
        "field_strength": (0.0, 1.0),
        "evolution_confirm_count": (1, 20),
        "evolution_confirm_window_days": (1, 90),
        "evolution_max_history_days": (1, 365),
        "evolution_max_new_beliefs_per_day": (0, 20),
        "evolution_max_candidates_per_day": (0, 50),
        "evolution_relevance_percentile": (50, 95),
        "diary_keep_days": (30, 3650),
        "diary_lookback": (1, 30),
        "diary_timeout": (5, 300),
        "chars_per_line": (5, 120),
        "max_inject_chars": (200, 4000),
        "think_timeout": (3, 120),
        "rel_trust_day_gain": (0.0, 5.0),
        "rel_trust_share_gain": (0.0, 5.0),
        "rel_trust_penalty": (0.0, 10.0),
        "rel_trust_cap_day": (0.0, 20.0),
    }
    _CFG_ENUMS = {
        "evolution_mode": ("off", "rule", "daily", "deep"),
        "drive_source": ("internal", "external"),
    }

    def _cfg_check(self, key: str, val):
        """返回 (ok, 错误说明或规范化后的值)。"""
        enum = self._CFG_ENUMS.get(key)
        if enum and val not in enum:
            return False, f"{key} 只能是 {'/'.join(enum)}"
        rng = self._CFG_LIMITS.get(key)
        if rng:
            lo, hi = rng
            try:
                if not (lo <= float(val) <= hi):
                    return False, f"{key} 应在 {lo}~{hi} 之间（收到 {val}）"
            except (TypeError, ValueError):
                return False, f"{key} 期望数字"
        return True, val

    def _cfg_file(self):
        """插件配置文件：AstrBot 的 data/config/<plugin>_config.json。"""
        from pathlib import Path

        return Path("data") / "config" / "astrbot_plugin_persona_mind_config.json"

    def _cfg_save(self) -> str:
        """把当前 cfg 里认识的键写回配置文件；返回错误串（空串=成功）。"""
        try:
            f = self._cfg_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            old = {}
            if f.is_file():
                try:
                    old = json.loads(f.read_text(encoding="utf-8-sig"))
                except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                    old = {}
            old.update({k: v for k, v in self.cfg.items() if k in DEFAULTS})
            f.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
            return ""
        except OSError as e:
            return str(e)

    def _cmd_config(self, raw: list) -> str:
        """管理面：配置查改（/mind config get|set|reset|list <key> [value]）。"""
        # raw = ['/mind', 'config', 'get', '<key>' ...]；把路由词摘掉再解析
        args = [x for x in raw if x.strip().lower() not in ("config", "配置")]
        sub = (args[1].strip().lower() if len(args) > 1 else "list")

        if sub in ("list", "列表", "all"):
            keys = sorted(DEFAULTS)
            lines = [f"配置项 {len(keys)} 个，当前值：" ]
            for k in keys[:45]:
                lines.append(f"{k} = {self.cfg.get(k, DEFAULTS[k])}")
            return "\n".join(lines)

        if len(args) < 3:
            return "用法：/mind config get|reset <key> ｜ set <key> <value> ｜ list"
        key = args[2].strip()
        if key not in DEFAULTS:
            near = [k for k in DEFAULTS if key.lower() in k.lower()][:5]
            return f"没有配置项 {key}。" + (f"相近的有：{'、'.join(near)}" if near else "用 /mind config list 看全部。")

        default = DEFAULTS[key]
        sm = getattr(self, "self_model", None)
        if sub in ("get", "查看", "查"):
            if key == "evolution_mode" and sm is not None:
                return (f"{key} = {sm.mode}（实际生效值，存在 self_model.json）"
                        f"；配置项 = {self.cfg.get(key, default)}")
            return f"{key} = {self.cfg.get(key, default)}（默认 {default}，类型 {type(default).__name__}）"

        if sub in ("reset", "重置"):
            self.cfg[key] = default
            err = self._cfg_save()
            extra = ""
            if key == "evolution_mode" and sm is not None:
                extra = "，演化档位已同步" if sm.set_mode(str(default)) else "，但演化档位同步失败"
            return f"已重置 {key} = {default}" + extra + (f"，但写盘失败：{err}" if err else "")

        if sub in ("set", "设置", "改"):
            if len(args) < 4:
                return f"缺值：/mind config set {key} <{type(default).__name__}>"
            raw_val = " ".join(args[3:]).strip()
            try:
                if isinstance(default, bool):
                    low = raw_val.lower()
                    if low in ("1", "true", "on", "yes", "是", "开", "y", "t"):
                        val = True
                    elif low in ("0", "false", "off", "no", "否", "关", "n", "f"):
                        val = False
                    else:
                        return f"值不合法：{key} 只接受 true/false、on/off、1/0、开/关"
                elif isinstance(default, int):
                    val = int(raw_val)
                elif isinstance(default, float):
                    val = float(raw_val)
                elif isinstance(default, list):
                    val = [x for x in raw_val.replace("，", ",").split(",") if x]
                else:
                    val = raw_val
            except (TypeError, ValueError):
                return f"类型不对：{key} 期望 {type(default).__name__}，收到 {raw_val!r}"
            ok, msg = self._cfg_check(key, val)
            if not ok:
                return f"值不合法：{msg}"
            old = self.cfg.get(key, default)
            self.cfg[key] = val
            err = self._cfg_save()
            extra = ""
            if key == "evolution_mode" and sm is not None:
                extra = "，演化档位已同步" if sm.set_mode(str(val)) else "，但演化档位同步失败"
            return (f"{key}: {old} → {val}" + extra
                    + (f"，但写盘失败：{err}" if err else "（已写入配置文件，重启后仍生效）"))

        return "支持：list / get <key> / set <key> <value> / reset <key>"

    async def _cmd_evolution(self, event: AstrMessageEvent, raw: list[str]) -> str:
        """管理面：status / mode / pause / resume / review / approve / reject / reset。

        权限原则：看是只读，改要管理员。模型只提候选，不批准自己提的变化。
        """
        sm = self.self_model
        args = [a.strip() for a in (raw[2:] or []) if a.strip()]
        action = (args[0] if args else "status").lower()
        try:
            admin = bool(event.is_admin())
        except Exception:
            admin = False

        if action in ("status", "状态"):
            st = sm.status()
            return (
                f"演化档位：{st['mode']}{'（已暂停）' if st['paused'] else ''}\n"
                f"信念 {st['beliefs']} 条（待观察 {st['hold']}）\n"
                f"今天候选 {st['today_candidates']} / 新信念 {st['today_new']}\n"
                f"分类：{st['by_kind'] or '暂无'}"
            )

        if action in ("mode", "档") and len(args) > 1:
            if not admin:
                return "只有管理员能切档。"
            mode = args[1].lower()
            if not sm.set_mode(mode):
                return "档位只能是 off / rule / daily / deep。"
            # 两个管理入口必须落到同一份持久化（复审 P1-2）：
            # self_model.json 是真相，配置项只是镜像，这里回写保持一致。
            self.cfg["evolution_mode"] = mode
            err = self._cfg_save()
            return f"已切到 {mode}。" + (f"（配置回写失败：{err}）" if err else "")

        if action in ("pause", "暂停"):
            if not admin:
                return "只有管理员能暂停。"
            sm.pause()
            return "已暂停演化（PAD、关系、聊天继续工作）。"

        if action in ("resume", "恢复"):
            if not admin:
                return "只有管理员能恢复。"
            sm.resume()
            return "已恢复演化。"

        if action in ("review", "复核"):
            n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
            rows = sm.review(n)
            # 存一份快照：序号只在这次列表里有意义（复审 P1-2：
            # 审批会改强度→重排序→同一个序号指向别的信念）
            self.__dict__[f"_review_ids:{event.get_sender_id()}"] = [str(r.get("id") or "") for r in rows]
            if not rows:
                return "还没有演化记录。"
            lines = []
            for i, r in enumerate(rows, 1):
                # review() 返回的是 belief 行：status / created_at / scope / id
                mark = {"accept": "✓", "hold": "◷", "reject": "✗"}.get(str(r.get("status")), "?")
                owner = str(r.get("scope") or "")
                who = "她自己" if owner == "self" else owner.replace("user:", "用户 ")
                lines.append(
                    f"{i}. {mark} [{who}] {r.get('claim') or '（无）'}\n"
                    f"   id={r.get('id', '?')} · {str(r.get('created_at') or '')[:10]}"
                    f" · {r.get('kind', '')} · 证据 {r.get('evidence_count', 1)} 次"
                    f" · 强度 {float(r.get('strength', 0) or 0):.2f}"
                )
            lines.append("（approve/reject 可以给序号，也可以直接给 id；序号只对这份列表有效）")
            return "\n".join(lines)

        if action in ("approve", "采纳", "reject", "拒绝") and len(args) > 1:
            if not admin:
                return "只有管理员能采纳或拒绝。"
            target = args[1].strip()
            verdict = "accept" if action in ("approve", "采纳") else "reject"
            belief_id = ""
            if target.startswith("b-"):
                belief_id = target           # 直接按 ID 走，不受排序影响
            elif target.isdigit():
                ids = self.__dict__.get(f"_review_ids:{event.get_sender_id()}") or []
                idx = int(target) - 1
                if not (0 <= idx < len(ids)):
                    return "序号超出范围（或列表已过期）。先 /mind evolution review 重新看一遍。"
                belief_id = ids[idx]
            else:
                return "用法：approve <序号|b-xxxxxx>。先 /mind evolution review 看列表。"
            if not belief_id:
                return "这条记录没有 id，先 /mind evolution review 重看。"
            out = sm.approve(belief_id, verdict)
            if not out.get("matched"):
                return f"没找到这条信念（{belief_id}）。"
            return f"已{'采纳' if verdict == 'accept' else '拒绝'}：{out.get('claim') or belief_id}"

        if action in ("events", "事件") and len(args) > 1 and args[1].isdigit():
            rows = sm.review_events(int(args[1]))
            if not rows:
                return "还没有事件。"
            lines = []
            for r in rows:
                lines.append(f"{r.get('id', '?')} · {str(r.get('ts', ''))[5:16]} · "
                             f"{r.get('decision')} · {r.get('reason') or ''}")
            return "最近事件（rollback 用第一列的 id）：" + chr(10) + chr(10).join(lines)

        if action in ("rollback", "回滚") and len(args) > 1:
            if not admin:
                return "只有管理员能回滚。"
            target = args[1].strip()
            if len(target) == 10 and target[4] == "-" and target[7] == "-":
                out = sm.restore_snapshot(target)
                if not out.get("ok"):
                    return f"恢复失败：{out.get('reason')}"
                return f"已恢复到 {target}（{out.get('count')} 条信念）。回滚前已另存一份。"
            out = sm.rollback_event(target)
            if not out.get("ok"):
                return f"回滚失败：{out.get('reason')}"
            if out.get("deleted"):
                return f"已删除这条信念：{out.get('claim') or target}"
            return f"已回滚：{out.get('claim') or target}"

        if action in ("reset", "重置"):
            if not admin:
                return "只有管理员能重置。"
            n = sm.reset("all")
            return f"已清空 {n} 条信念（含 self 信念与演化事件；快照保留在 evolution_snapshots/）。"

        return (
            "用法：/mind evolution [status | mode off/rule/daily/deep | pause | resume | "
            "review [n] | approve <n|id> | reject <n|id> | events [n] | "
            "rollback <事件id|YYYY-MM-DD> | reset]"
        )

    @filter.command("mind")
    async def cmd_mind(self, event: AstrMessageEvent):
        """查看/操作内心引擎：/mind [status|profile|kb|toggle|selftest|diary]"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能用")
            return
        raw = (event.message_str or "").split()
        sub = (raw[1].strip().lower() if len(raw) > 1 else "status") or "status"
        uid = str(event.get_sender_id() or "")
        umo = str(getattr(event, "unified_msg_origin", "") or "")

        if sub in ("diary", "日记"):
            if not self.cfg.get("diary_enable", True):
                yield event.plain_result("日记功能现在关着（diary_enable=false）。")
                return
            arg = (raw[2].strip() if len(raw) > 2 else "")
            today = datetime.now().date().isoformat()
            if arg in ("today", "今天"):
                days = [today]
            elif arg in ("yesterday", "昨天"):
                days = [(datetime.now().date() - timedelta(days=1)).isoformat()]
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
                days = [arg]
            else:
                days = self._diary_due_days() or [today]
            if self.__dict__.get("_diary_writing"):
                yield event.plain_result("正在写，等它写完。")
                return
            self.__dict__["_diary_writing"] = True
            try:
                await self._write_diaries(days)
            finally:
                self.__dict__["_diary_writing"] = False
            done = [d for d in days if self.diary.has_diary(d)]
            if not done:
                yield event.plain_result(
                    "这次没写成：看日志里的 [mind] 日记 报错。可能是模型没给出合规 JSON 或超时。")
                return
            lines = [f"写完了：{d} → kb/diary/{d}.md" for d in done]
            yield event.plain_result(chr(10).join(lines))
            return

            self.cfg["enable"] = not bool(self.cfg.get("enable", True))
            saved = True
            try:
                self._cfg_save()      # 与 /mind config set 一致，重启不丢（专家 P2-11）
            except Exception as e:
                saved = False
                logger.warning(f"[mind] toggle 落盘失败: {e}")
            note = "" if saved else "（仅本次运行，落盘失败）"
            yield event.plain_result(
                f"内心引擎：{'开' if self.cfg['enable'] else '关'}{note}")
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

        if sub in ("config", "配置"):
            yield event.plain_result(self._cmd_config(raw))
            return

        if sub in ("evolution", "演化", "自我"):
            yield event.plain_result(await self._cmd_evolution(event, raw))
            return

        if sub in ("kb", "知识"):
            hits = self.kb.query((" ".join(raw[2:]).strip() if len(raw) > 2 else "测试"), 3)
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
            lines.append(self._health_line())
            yield event.plain_result("\n".join(lines))
            return

        if sub in ("why", "诊断"):
            # 把上一轮的决策摊开：每个数值是怎么来的，这一轮为什么这么判
            last = self.last_decision.get(umo) or {}
            if not last:
                yield event.plain_result("还没聊过，没有可诊断的决策。")
                return
            # 用当轮快照，不重新读当前状态：否则「上一轮对 A、这一轮管理员查」
            # 会把 B 的关系值与 PAD 混进 A 的诊断里（复核指出）。
            a = last.get("pad") or {}
            if not all(k in a for k in ("P", "Ar", "D")):
                yield event.plain_result("上一轮情绪快照不完整，请在下一次对话后诊断。")
                return
            p = last.get("relation") or {}
            aff = last.get("aff") or {}
            act_txt = " > ".join(
                f"{k} {v:.2f}" for k, v in sorted((last.get("acts") or {}).items(),
                                                key=lambda kv: kv[1], reverse=True) if v > 0.05
            ) or "（无）"
            allows = self.voice.allowed_sections(
                last.get("act", "answer"),
                last.get("tone", "plain"),
                {"T": p.get("T"), "I": p.get("I")},
                last.get("user_mood", 50.0),
            )
            banks = "、".join(sorted(allows)) or "（空）"
            lines = [
                "【上一轮她是怎么想的】",
                f"上一条（{last.get('uid', '未知用户')}）：{last.get('msg', '')[:40]}",
                f"他情绪判断：{last.get('user_mood', 50):.0f}/100",
                f"关系阶段：tier {last.get('tier', 1)}（信任{p.get('T')} 亲密{p.get('I')}）",
                f"情绪来源：愉悦{a['P']:.1f} 唤醒{a['Ar']:.1f} 底气{a['D']:.1f}",
                f"证据：{last.get('reason', '')[:120] or '（无）'}",
                f"动作权重：{act_txt}",
                f"定下来的动作：{last.get('act', '?')} / 口吻 {last.get('tone', '?')}"
                f" / 回 {last.get('budget', '?')} 条 / {last.get('length_mode', 'chat')}",
                f"允许的语料分组：{banks}",
            ]
            _bl = last.get("beliefs") or []
            if _bl:
                lines.append("带进提示词的信念：" + "；".join(_bl))
            else:
                lines.append("带进提示词的信念：（无）")
            if aff:
                lines.append(
                    f"情绪管理面板：好感 {aff.get('affection', '—')}｜"
                    f"对他攻击性 {aff.get('aggression_other', '—')}"
                    "（>30 时怼人会从玩笑变锋利）"
                )
            else:
                lines.append("情绪管理面板：读不到（桥接关闭或没有这个人）")
            yield event.plain_result("\n".join(lines))
            return

        a = self.mood.effective(umo)
        p = self.profiles.load(uid)
        yield event.plain_result(
            f"人格 O{self.cfg['persona_o']} C{self.cfg['persona_c']} E{self.cfg['persona_e']} "
            f"A{self.cfg['persona_a']} N{self.cfg['persona_n']}\n"
            f"情绪 愉悦{a['P']:.0f} 唤醒{a['Ar']:.0f} 底气{a['D']:.0f}\n"
            f"和你的关系 信任{p.get('T')} 亲密{p.get('I')} 聊过{p.get('count')}轮\n"
            f"知识 {len(self.kb.chunks)} 段 · 语料 {self.voice.count()} 句\n"
            + self._health_line()
        )
