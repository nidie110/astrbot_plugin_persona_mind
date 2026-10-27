"""挑「这一轮该给她哪几段调用格式」。

为什么不把 13 个动词的格式全塞进 system prompt：
每轮多 550 字（管理员 760 字），13 个动词里这一轮最多用得上 1 个；
而真正卡住她的从来不是「参数怎么写」，是「压根不动手」（6 天 1089 回合里只有 4.7% 有工具调用）。
所以判断规则常驻、格式按需 —— 这就是本模块存在的理由。

为什么用信号而不是让模型先问一句「怎么用」：
让她先做一次元动作（feiyu(action="help")）等于把「启动成本」再抬高一级，
而她本来就有启动困难。信号判断是本地纯计算，0 额外往返，判错了也只是少一段格式（软失败）。

信号表分两档（都是踩出来的）：
- strict：出现就注入。`搜/画/说/问妈妈/发说说` 这类词面专一，几乎不误报。
- weak：只在「上文确实有目标」（链接、媒体、她上一轮说过要去做）时才注入。
  语料里 19 次「说了不做」的真实说法是代词（问问她）、重叠动词（搜搜看）、
  条件句（实在不行去问她）、模糊授权（你自己看着办）—— 封闭短语表预测不了这些，
  所以别把 weak 当清单用，它只是「有目标时的加分项」。

已经踩过的两个反例，别改回去：
- 裸「说说」不能当信号：4 条全是「那你说说为什么」「陪我说说话」，100% 误命中。
- 裸「评论」也不够：10 条里 4 条在讨论「评论区能不能发」，得配上祈使/许可词。
"""

from __future__ import annotations

import re

from . import specs as S

# 否定/撤回：出现了就别动手
_NEG = re.compile(r"没在|不用|算了|别去|别搜|不要了|不用了|没事了|停止|别管")

# 她自己的回声：把调用语法发进了会话又被当用户消息收回来，下一轮照着抄
_ECHO = re.compile(r"feiyu\s*\(|action\s*[=:]\s*[\"']|target\s*[=:]\s*[\"']|question\s*[=:]\s*[\"']")

# 发评论要求「他在让我发」：光提「评论区」不算
_COMMENT_ASK = re.compile(
    r"帮我|你去|你试|你发|你能|能不能|可以|愿意|敢不敢|去发|发一?条|回他|评一句|留个言|发个评论"
)

_strict_cache: dict[str, re.Pattern] = {}
_weak_cache: dict[str, re.Pattern] = {}

# 视频站链接只可能走「看视频」，别让「读网页」的弱信号也跟着抢一段格式
_VIDEO_URL = re.compile(
    r"https?://[^\s]*(?:bilibili\.com|b23\.tv|douyin\.com|youtu\.be|youtube\.com"
    r"|kuaishou\.com|xiaohongshu\.com|weibo\.com|ixigua\.com)",
)


def _compiled(bucket: dict, key: str, patterns) -> re.Pattern | None:
    if key in bucket:
        return bucket[key] or None
    pat = "|".join(f"(?:{p})" for p in patterns) if patterns else ""
    try:
        bucket[key] = re.compile(pat) if pat else None
    except re.error:
        bucket[key] = None
    return bucket[key]


def _available_set(available) -> set | None:
    return None if available is None else {str(x) for x in available}


def pick(
    text: str,
    *,
    admin: bool = False,
    available=None,
    stick=(),
    promise: str = "",
    has_target: bool = False,
    max_n: int = S.MAX_VERBS,
) -> list[str]:
    """挑 0–max_n 个动词（按优先级）。任何异常都返回空列表 —— 少一段格式不影响她干活。"""
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return []
    if _NEG.search(t) or _ECHO.search(t):
        return []

    have = _available_set(available)
    stick_set = {str(x) for x in (stick or ())}
    promise = (promise or "").strip()

    scores: dict[str, float] = {}
    for verb, spec in S.SPECS.items():
        if have is not None and verb not in have:
            continue
        if spec.tier == "admin" and not admin:
            continue

        hit = False
        pat = _compiled(_strict_cache, verb, spec.strict)
        if pat is not None and pat.search(t):
            hit = True
            # 「评论」必须配上「他在让我发」，否则只是在聊评论区
            if verb == "发评论" and not _COMMENT_ASK.search(t):
                hit = False
        if hit:
            scores[verb] = 1.0
            continue

        # 弱信号：要有目标才认
        if has_target or verb in stick_set:
            if verb == "读网页" and _VIDEO_URL.search(t):
                continue  # 视频站链接走看视频，别两边都注入
            wpat = _compiled(_weak_cache, verb, spec.weak)
            if wpat is not None and wpat.search(t):
                scores[verb] = 0.5

    # 会话粘性：上一轮注入了、还没做成，这一轮继续给（钩子每轮只打一次，多步任务容易丢格式）
    for verb in stick_set:
        if have is not None and verb not in have:
            continue
        spec = S.SPECS.get(verb)
        if spec is not None and spec.tier == "admin" and not admin:
            continue
        scores[verb] = scores.get(verb, 0.0) + 1.2

    # 承诺追踪：上一轮说了「我这就去」却没动手 —— 把那个动词的格式连同压力一起递过去
    if promise and (have is None or promise in have):
        spec = S.SPECS.get(promise)
        if spec is not None and (admin or spec.tier != "admin"):
            scores[promise] = scores.get(promise, 0.0) + 1.5

    if not scores:
        return []
    # 读评论命中时不必再给「读网页」：同一件事给两条格式，她只会挑错那条
    if "读评论" in scores and "读网页" in scores:
        scores.pop("读网页", None)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], -S.PRIORITY.get(kv[0], 0)))
    return [v for v, _ in ranked[:max_n]]


def leak_filter(text: str) -> tuple[str, bool]:
    """出站清洗：把漏进正文的调用语法剪掉。返回 (clean, 是否命中)。"""
    if not text or not _ECHO.search(text):
        return text, False
    cleaned = re.sub(r"feiyu\s*\([^)\n]{0,200}\)?", "", text)
    cleaned = re.sub(r"(?:action|target|text|question|visibility)\s*[=:]\s*[\"'][^\"'\n]{0,200}[\"']", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True
