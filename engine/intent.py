"""五意图计算：把「性格 + 情绪 + 关系 + 这句话」压成五个方向。

设计取舍（重要，写给后来改代码的人）：
- 第一版用「各项加权再除以最大值」，结果所有分数都挤在 0.5 附近，
  排序几乎不变——永远在同一两个意图里打转。
- 现在改成「证据驱动 + softmax 拉开差距」：
  每个意图由一到两个**稀疏证据**主导（默认很低），
  再掺一点性格底色，最后用低温度 softmax 让差距显性化。
"""

from __future__ import annotations

import math

# 意图代号 → 中文名。发布版允许在配置里改文案。
INTENT_NAMES = {
    "care": "安慰",
    "tease": "损他两句",
    "share": "说点自己的事",
    "probe": "追问",
    "dodge": "敷衍",
}

# 触发词（可按语言/圈层改）
COMFORT_WORDS = ("难受", "撑不住", "烦死", "好累", "累死", "不想活", "崩溃", "想哭")
MATERIAL_WORDS = ("你呢", "你怎么样", "你的事", "你今天", "你干嘛", "你会不会", "你怎么想")
ASK_WORDS = ("吗", "什么", "怎么", "为什么", "多少", "哪", "是不是")
PLAY_WORDS = ("笨蛋", "傻", "憨", "哈哈", "开玩笑", "逗你")
ANGRY_WORDS = ("大肥鱼", "胖", "废物", "没用")


def _norm(v: float) -> float:
    return max(0.0, min(1.0, float(v) / 100.0))


def evidence(msg: str, play_bias: float = 0.3, user_mood: float = 50.0) -> dict:
    """从这句话里抽稀疏证据。

    注意 gap 的语义是「信息缺口」，不是「字数少」：
    一句很短的陈述（比如"不想上课"）没有信息缺口，不该触发追问。
    """
    msg = msg or ""
    mood = max(0.0, min(100.0, float(user_mood)))
    mood_need = max(0.0, min(1.0, (55.0 - mood) / 45.0))  # 对方越低落，越需要安慰
    question = ("？" in msg or "?" in msg) and len(msg) <= 20
    gap = 0.8 if question else (0.35 if len(msg) <= 4 else 0.2)
    return {
        "comfort": max(1.0 if any(w in msg for w in COMFORT_WORDS) else 0.15, mood_need),
        "material": 0.8 if any(w in msg for w in MATERIAL_WORDS) else 0.15,
        "gap": gap,
        "asked": 1.0 if any(w in msg for w in ASK_WORDS) else 0.0,
        "play": 0.6 if any(w in msg for w in PLAY_WORDS) else play_bias,
        "angry": 1.0 if any(w in msg for w in ANGRY_WORDS) else 0.0,
        "mood": mood,
    }


def scores(
    *,
    persona: dict,
    affect: dict,
    relation: dict,
    scene: str,
    msg: str,
    play_bias: float = 0.3,
    user_mood: float = 50.0,
) -> dict[str, float]:
    """返回五个意图的归一化分数（和为 1）。"""
    ev = evidence(msg, play_bias, user_mood)
    p = {k: _norm(persona.get(k, 50)) for k in ("O", "C", "E", "A", "N")}
    a = affect or {}
    P, Ar = _norm(a.get("P", 60)), _norm(a.get("Ar", 45))
    I = _norm(relation.get("I", 40))
    group = 1.0 if scene == "group" else 0.0

    care = 0.60 * ev["comfort"] + 0.25 * I + 0.15 * (1 - p["N"])
    tease = 0.50 * ev["play"] + 0.25 * P + 0.25 * (1 - p["A"])
    share = 0.60 * ev["material"] + 0.25 * P + 0.15 * p["E"]
    probe = 0.45 * ev["gap"] + 0.20 * p["O"] + 0.10 * p["E"]
    dodge = 0.60 * (1 - Ar) + 0.25 * (1 - I) + 0.15 * group
    # 对方越需要安慰，越不该敷衍（心软的时候犯困也不躲）
    dodge *= 1.0 - 0.6 * ev["comfort"]

    # 对方情绪很低时，追问会让 ta 更累
    if ev["mood"] < 40:
        probe *= 0.7

    # 问正经事时不许敷衍（信息型提问，长度超过 10 字）
    if ev["asked"] and len(msg) > 10:
        dodge *= 0.3
        probe *= 0.7

    raw = {"care": care, "tease": tease, "share": share, "probe": probe, "dodge": dodge}
    t = 0.12
    mx = max(raw.values()) or 1.0
    exp = {k: math.exp((v - mx) / t) for k, v in raw.items()}
    tot = sum(exp.values()) or 1.0
    return {k: round(v / tot, 3) for k, v in exp.items()}


def _force_share(out: dict[str, float], lead: str, share: float) -> dict[str, float]:
    """把某个意图的份额硬抬到 share，其余按原比例分剩下的。"""
    share = max(0.0, min(1.0, share))
    rest = {k: v for k, v in out.items() if k != lead}
    total = sum(rest.values()) or 1.0
    out[lead] = share
    for k in rest:
        out[k] = round(rest[k] / total * (1.0 - share), 3)
    return out


def apply_overrides(result: dict[str, float], msg: str) -> dict[str, float]:
    """硬规则优先于公式：有些场景算错了会很违和。

    注意：这里抬的是「份额」而不是权重——直接设绝对值再归一化，
    会被其它意图稀释掉（实测 0.8 会被摊回 0.46，等于没生效）。
    """
    out = dict(result)
    msg = msg or ""
    if any(w in msg for w in ANGRY_WORDS):
        out = _force_share(out, "tease", 0.8)
        out["care"] = min(out.get("care", 0.0), 0.1)
    if any(w in msg for w in ("难受", "撑不住", "崩溃", "想哭")):
        out = _force_share(out, "care", 0.8)
        out["tease"] = min(out.get("tease", 0.0), 0.2)
    total = sum(out.values()) or 1.0
    return {k: round(v / total, 3) for k, v in out.items()}


def top_two(result: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(result.items(), key=lambda kv: kv[1], reverse=True)[:2]


def line_budget(top: dict[str, float], mentioned: bool, scene: str) -> str:
    """这一轮该说几条。"""
    lead, score = next(iter(top.items())) if top else ("care", 0.0)
    if lead == "dodge" and score > 0.5:
        return 0  # 1 条或干脆不回
    if mentioned or (lead in ("share", "tease") and score > 0.5):
        return 3 if scene == "group" else 4
    return 2
