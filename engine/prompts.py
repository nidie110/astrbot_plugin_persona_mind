"""把各层结果拼成一段「内心」，注入到 system prompt。

关键设计：这段**永远不发送**，只影响她怎么开口。
所以可以写细、写长，不必担心用户看到。
"""

from __future__ import annotations

from .act import ACT_HINTS, ACT_NAMES, TONE_HINTS, TONE_NAMES
from .appraisal import EMOTION_LABELS
from .intent import INTENT_NAMES

HEADER = "【此刻的内心（不要说出来，只用来决定怎么说）】"

# 数值不进提示词：数字会被模型复述（"我现在愉悦 54"），而且它对"怎么说话"没有指导作用。
# 换成定性档位，模型拿到的是"感觉"，不是读数。
_MOOD_P = [(30.0, "心里很沉"), (45.0, "有点闷"), (55.0, "还算平静"), (70.0, "挺愉快")]
_MOOD_AR = [(30.0, "提不起劲"), (45.0, "懒懒的"), (55.0, "平常"), (70.0, "有精神")]
_MOOD_D = [(35.0, "没什么底气"), (50.0, "有点虚"), (65.0, "挺稳")]


def _word(value, table: list[tuple[float, str]], top: str) -> str:
    """把 0~100 的分值翻成一个词；表里没有的区间用 top。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return top
    for bound, word in table:
        if v < bound:
            return word
    return top


def feel_line(affect: dict, emotion: str) -> str:
    """「她现在什么感觉」——只用定性词，不暴露任何数值。"""
    p = _word(affect.get("P", 60), _MOOD_P, "心情很好")
    ar = _word(affect.get("Ar", 45), _MOOD_AR, "很上头")
    d = _word(affect.get("D", 50), _MOOD_D, "很有把握")
    mood = EMOTION_LABELS.get(emotion, emotion)
    return f"- 我现在的感觉：{p}，{ar}，{d}；此刻主要是{mood}"


def render(
    *,
    affect: dict,
    emotion: str,
    thought: dict,
    profile_line: str,
    intents: dict,
    lead: list[tuple[str, float]],
    budget: int,
    recent: list[str],
    kb_hits: list[str],
    voice_lines: list[str],
    diary_hits: list[dict] | None = None,
    claims: list[str] | None = None,
    beliefs: list[str] | None = None,
    scene: str,
    max_chars: int = 1200,
    line_chars: int = 15,
    decision: dict | None = None,
    tier: int = 1,
    lead_act: str = "",
    lead_tone: str = "",
) -> str:
    lines = [
        "",
        HEADER,
        feel_line(affect, emotion),
        f"- 他可能在：{thought.get('doing') or '不清楚'}（{thought.get('where') or '不确定'}），"
        f"情绪：{thought.get('mood') or '看不出'}，真正想要：{thought.get('want') or '不知道'}",
    ]
    if profile_line:
        lines.append(f"- 我对他的了解：{profile_line}")

    if beliefs:
        # 信念是她自己沉淀的，措辞必须写成「你已经这样认为了」，
        # 而不是再给她一条任务——否则会变成第五个指令源。
        lines.append("- 这些是你自己慢慢得出的看法，不用每句都提，但说话时会带着：")
        for _b in beliefs[:2]:
            _t = (_b.get("claim") if isinstance(_b, dict) else _b) or ""
            if _t:
                lines.append(f"  · {_t}")

    if decision:
        lead_act = (decision.get("lead") or [("answer", 0.0)])[0][0]
        tone = decision.get("tone", "plain")
        hint = decision.get("act_hint") or ACT_HINTS.get(lead_act, "")
        if tier == 0 and lead_act == "share":
            # 生疏档的「说自己的」只能是被问之后的简短回答，不是主动交代
            hint = "他问到了，就照实简短答一句，别自己另外起头讲"
        lines.append(
            f"- 这一轮你想：{ACT_NAMES.get(lead_act, lead_act)}。{hint}。"
        )
        lines.append(
            f"- 口吻：{TONE_NAMES.get(tone, tone)}——{TONE_HINTS.get(tone, '正常说话')}。"
        )
        if "care" in decision.get("secondary_acts", []):
            lines.append("- 同时照顾他的情绪：简短接住低落，再继续解决他明确提出的问题；不要用安慰替代办事。")
        # 关系阶段对披露深度的约束，必须写在这里——
        # 只清空语料池而保留「说点自己的」这句要求，等于什么都没拦住。
        if tier == 0 and lead_act == "share":
            lines.append(
                "- 但我们还不熟：他问到就简短答一句，别主动展开自己的生活，也别编细节。"
            )
        elif tier == 0:
            lines.append("- 我们还不熟：别撒娇、别用亲昵称呼，也别主动讲自己的事。")
    else:
        top_txt = " + ".join(f"{INTENT_NAMES.get(k, k)}({v:.2f})" for k, v in lead)
        lines.append(f"- 我最想做的：{top_txt}")
    if decision and decision.get("length_mode") == "task":
        lines.append("- 本轮是明确任务：日常碎聊的字数与条数限制不适用。按任务需要给完整代码、步骤或解释，保持口语，不截断答案。")
    else:
        lines.append(
            f"- 这一轮说：{budget} 条左右，每条不超过 {line_chars} 字"
            if budget > 0
            else "- 这一轮说：1 条，或者干脆不回"
        )
    # 决策与任务长度约束放在可裁剪素材前，避免素材挤掉主要求。
    if claims:
        lines.append("- 你以前对这类东西有过判断（可以拿出来用）：")
        for c in claims[:2]:
            lines.append("  · " + str(c).replace("\n", " ")[:140])
        lines.append("  （只有当前界面亲自经历的，才能说“我刚看过”；如果写着“听小鲸鱼说过”，就保留这个来源，不要冒充刚刚亲历。）")
    if diary_hits:
        lines.append("- 你想起自己以前写过的：")
        for d in diary_hits[:2]:
            day = d.get("date") or ""
            body = (d.get("text") or "").replace("\n", " ")[:120]
            lines.append(f"  · {day}：{body}")
        lines.append("  （这是你自己写过的东西，不是资料；想说就用自己的话提一句，不想提就放着。）")
    if kb_hits:
        lines.append("- 你记得的相关常识：")
        for hit in kb_hits:
            lines.append("  · " + hit.replace("\n", " ")[:160])
    if voice_lines:
        lines.append("- 你以前这么说过（借味道，别照抄）：")
        for line in voice_lines:
            lines.append("  · " + line)
    if recent:
        lines.append(f"- 我们最近聊到：{'／'.join(recent[-2:])}")

    text = "\n".join(lines)
    return text[:max_chars]
