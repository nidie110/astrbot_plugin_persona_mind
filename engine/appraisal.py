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

请判断：
1. 对方此刻可能在做什么、在哪、什么情绪、真正想要什么（允许猜测，用「可能」「应该」）
2. 对这条消息做一次评价：相关性 relevance(0~1)、对我不利还是有利 desirability(-1~1)、责任在谁 agency(other/self/none)、我能改变吗 control(0~1)
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
