"""把「能力自述 / 判断规则 / 调用格式 / 妈妈纸条」拼成一段，超预算就整块丢。

为什么要有这个文件：1.3.5 之前是 `block[:1300] + "…"` 硬切。
后果不是「少几个字」：
- 管理员会话实测 1806 字，被切掉的是格式尾注 + 整段妈妈纸条；
- 而且切点落在字符上，会把 `feiyu(action="发说说", text="<正文…` 切一半 ——
  半行规范比没有更危险，她会照抄半句；
- 被切了也没有任何日志，运维看不出丢了什么。

这里的规矩：
1. 按整块丢，绝不切半行；
2. 参与预算的三段按 guide > caps > format 排（判断规则最底层，不能动）；
3. 妈妈纸条不参与预算：它 7 天有效、一天只出现一次，丢一次就是丢一天，比省 200 字贵；
4. 丢谁就记谁，日志里点名。

预算实测（1.3.6）：guide 670 + caps 306 + format 232 + 段间空行 ≈ 1212。
所以非管理员 1250、管理员 1450 —— 刚好容得下「判断 + 能力 + 一次命中」，
再多的动词也进不来（MAX_VERBS=2 在 selector 那边就拦住了）。
"""

from __future__ import annotations

NL = chr(10)

# 参与预算裁剪的段，顺序即保留优先级
ORDER = ("guide", "caps", "format")

# 不参与预算的段：一天只出现一次，丢了就是丢一天
FREE = ("mom",)

BUDGET = {"all": 1250, "admin": 1450}


def build(parts: dict, *, budget: int | None = None, admin: bool = False) -> tuple[str, list[str]]:
    """parts: {guide, caps, format, mom} → (拼好的段落, 被丢掉的段名)。"""
    if budget is None:
        budget = BUDGET["admin" if admin else "all"]

    kept: list[tuple[str, str]] = []
    dropped: list[str] = []
    total = 0
    for name in ORDER:
        text = (parts.get(name) or "").strip()
        if not text:
            continue
        extra = len(text) + (2 if kept else 0)  # 段间空行
        if total + extra > budget:
            dropped.append(name)
            continue
        kept.append((name, text))
        total += extra

    for name in FREE:
        text = (parts.get(name) or "").strip()
        if text:
            kept.append((name, text))

    if not kept:
        return "", dropped
    block = (NL + NL).join(text for _, text in kept)
    return block, dropped


def summarize(parts: dict) -> str:
    """一行日志：每段多少字，没拼上的记为 0。"""
    return " ".join(f"{n}={len((parts.get(n) or '').strip())}" for n in ORDER + FREE)
