"""OCC 评价模型 + Theory of Mind 推断。

理论出处：
- Ortony, Clore & Collins, *The Cognitive Structure of Emotions* (1988)
  情绪不是凭空来的，是「评价」出来的。四个维度：
  目标相关性 relevance、目标一致性 desirability、责任归属 agency、可控性 control。
- Premack & Woodruff (1978) 提出 Theory of Mind；
  工程上落地成四个待填字段：他在做什么 / 在哪 / 情绪 / 真正想要什么。
"""

from __future__ import annotations

import json
import math
import re

# OCC 情绪 → PAD 变化。数值是经验参数，可按角色性格调整。
OCC_EFFECTS: dict[str, dict[str, float]] = {
    "Anger": {"P": -10, "Ar": 15, "D": 5},
    "Joy": {"P": 12, "Ar": 8, "D": 0},
    "Guilt": {"P": -8, "Ar": 10, "D": -10},
    "Worry": {"P": -5, "Ar": 8, "D": -5},
    "Tender": {"P": 3, "Ar": -5, "D": -3},
    "Annoyed": {"P": -6, "Ar": 5, "D": 3},
    "None": {"P": 0, "Ar": 0, "D": 0},
}

EMOTION_LABELS = {
    "Anger": "生气",
    "Joy": "开心",
    "Guilt": "自责",
    "Worry": "担心",
    "Tender": "心软",
    "Annoyed": "烦",
    "None": "无波动",
}

THINK_PROMPT = """你是一个聊天机器人的「内心活动」模块，只输出 JSON，不要任何解释。

当前时间：{now}
对方消息：{msg}
最近两条对话：{recent}
我对这个人的印象：{profile}
我自己现在：{self_state}

请判断：
1. 对方此刻可能在做什么、在哪、什么情绪、真正想要什么（允许猜测，用「可能」「应该」）
2. 对这条消息做一次评价（先站你自己的立场想「这事关不关我的事」，再看对方）：
   - relevance 相关性(0~1)：这条消息**跟她自己的目标、在意的人、当前心情的关联程度**，
     不是「话题有没有提到她」。分档参考：
     · 0.0~0.2 与她无关（陌生人在群里聊别的、她完全不关心的领域）
     · 0.3~0.5 有点关系（泛泛的日常，她在旁边听着，可能有一点点感想）
     · 0.6~0.8 跟她有关（她懂的话题、普通程度的诉苦、聊到她在意的人）
     · 0.9~1.0 直接冲着她来（叫她、骂她、夸她、明确求助、她在乎的人出事）
     0 表示她完全没被牵动，不会产生任何情绪。
   - desirability 有利性(-1~1)：对她或她在意的人有利还是不利
   - agency 责任归属(other/self/none)
   - control 可控性(0~1)：她能改变结果的程度
3. 这条消息让我产生的主要情绪，只能选一个：Anger（生气）、Joy（开心）、Guilt（自责）、Worry（担心）、Tender（心软）、Annoyed（烦）、None（无波动）
4. 对方情绪值 mood(0~100，0 很差 100 很好)

输出：
{{"doing":"","where":"","mood":"","want":"","relevance":0.0,"desirability":0.0,"agency":"other","control":0.0,"emotion":"None","user_mood":50}}"""


def parse_json_block(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象；失败返回空 dict。"""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def normalize_appraisal_number(value, low: float, high: float, default: float) -> float:
    """校验模型的数值字段；0 是合法值，异常类型与 NaN/Inf 使用默认值。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number)) if math.isfinite(number) else default


def normalize_user_mood(value) -> float:
    """保留合法的最低情绪值 0；无效值回退到中性。"""
    return normalize_appraisal_number(value, 0.0, 100.0, 50.0)


def thought_to_effects(thought: dict, intensity: float = 1.0) -> tuple[str, dict]:
    """把 OCC 评价结果落到 PAD 变化上。"""
    emotion = str(thought.get("emotion") or "None")
    effects = OCC_EFFECTS.get(emotion, OCC_EFFECTS["None"])
    if intensity == 1.0:
        return emotion, dict(effects)
    return emotion, {k: v * intensity for k, v in effects.items()}


def thought_brief(thought: dict) -> str:
    """一行摘要，给日志和注入用。"""
    if not thought:
        return "（没有推断出内容）"
    return (
        f"他在{thought.get('doing') or '忙什么'}（{thought.get('where') or '不确定'}），"
        f"情绪{thought.get('mood') or '看不出'}，想要{thought.get('want') or '不知道'}"
    )
