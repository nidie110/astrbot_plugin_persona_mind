"""内心引擎（Mind Engine）——可发布的通用实现。

分层：
    Big Five 人格（稳定）
      → OCC 评价（每条消息）
      → PAD 情绪（带衰减）
      → ToM 推断（对方情景）
      → 五意图（安慰/损/自述/追问/敷衍）
      → 内心注入（不发送）
      → 主模型开口

所有模块都不依赖 AstrBot，可单独测试。
"""

from .affect import Affect, normalize01
from .appraisal import (
    EMOTION_LABELS,
    OCC_EFFECTS,
    THINK_PROMPT,
    parse_json_block,
    thought_brief,
    thought_to_effects,
)
from .intent import (
    INTENT_NAMES,
    apply_overrides,
    evidence,
    line_budget,
    scores,
    top_two,
)
from .knowledge import KnowledgeBase, chunk_text, tokens
from .profiles import ProfileStore, empty, summary_line
from .prompts import render
from .voice import VoiceCorpus

__all__ = [
    "Affect",
    "normalize01",
    "EMOTION_LABELS",
    "OCC_EFFECTS",
    "THINK_PROMPT",
    "parse_json_block",
    "thought_brief",
    "thought_to_effects",
    "INTENT_NAMES",
    "apply_overrides",
    "evidence",
    "line_budget",
    "scores",
    "top_two",
    "KnowledgeBase",
    "chunk_text",
    "tokens",
    "ProfileStore",
    "empty",
    "summary_line",
    "render",
    "VoiceCorpus",
]
