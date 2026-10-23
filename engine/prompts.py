"""把各层结果拼成一段「内心」，注入到 system prompt。

关键设计：这段**永远不发送**，只影响她怎么开口。
所以可以写细、写长，不必担心用户看到。
"""

from __future__ import annotations

from .appraisal import EMOTION_LABELS
from .intent import INTENT_NAMES

HEADER = "【此刻的内心（不要说出来，只用来决定怎么说）】"


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
    scene: str,
    max_chars: int = 1200,
) -> str:
    lines = [
        "",
        HEADER,
        f"- 我现在的状态：愉悦 {affect.get('P', 60):.0f}/100，唤醒 {affect.get('Ar', 45):.0f}/100，"
        f"底气 {affect.get('D', 50):.0f}/100；主要情绪：{EMOTION_LABELS.get(emotion, emotion)}",
        f"- 他可能在：{thought.get('doing') or '不清楚'}（{thought.get('where') or '不确定'}），"
        f"情绪：{thought.get('mood') or '看不出'}，真正想要：{thought.get('want') or '不知道'}",
    ]
    if profile_line:
        lines.append(f"- 我对他的了解：{profile_line}")

    if kb_hits:
        lines.append("- 你记得的相关常识：")
        for hit in kb_hits:
            lines.append("  · " + hit.replace("\n", " ")[:160])

    if voice_lines:
        lines.append("- 你以前这么说过（借味道，别照抄）：")
        for line in voice_lines:
            lines.append("  · " + line)

    top_txt = " + ".join(f"{INTENT_NAMES.get(k, k)}({v:.2f})" for k, v in lead)
    lines.append(f"- 我最想做的：{top_txt}")
    lines.append(
        f"- 这一轮说：{budget} 条左右，每条不超过 15 字"
        if budget > 0
        else "- 这一轮说：1 条，或者干脆不回"
    )
    if recent:
        lines.append(f"- 我们最近聊到：{'／'.join(recent[-2:])}")

    text = "\n".join(lines)
    return text[:max_chars]
