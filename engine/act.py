"""行为通道（act）+ 语气通道（tone）——v2 核心改动。

为什么要拆：
    旧版的「五意图」把两件不同的事混在了一起——她**做什么**，和**怎么说**。
    「敷衍」既是动作（不回/少回）也是口吻（冷淡），于是模型只能二选一，
    最后全都滑向情绪化表达；而「帮我写个脚本」这种正经请求无处可去。

    v2 拆成两条正交的轴：
        act  ∈ {answer, care, share, tease, probe, dodge, coy, seek}
        tone ∈ {plain, warm, dry, cold, playful, sharp, soft}

    组合才有细节：care+sharp（嘴上损、手上帮）、answer+plain（正经回答）、
    dodge+cold（真的不想理）、tease+playful（闹着玩）。

设计约束（给后来改代码的人）：
    1. answer 是保底动作。没有它，任何输入都会被推向情绪化方向。
    2. 自我否定（"我是废物"）走 care，不走 tease。触发词必须看"对象"。
    3. 所有分数都要能被配置覆盖，数值只是默认值。
"""

from __future__ import annotations

import math
import re

from .appraisal import normalize_user_mood

# ---------------------------------------------------------------- 名称表

ACT_NAMES = {
    "answer": "正常应答",
    "care": "关心",
    "share": "说点自己的",
    "tease": "逗他",
    "probe": "追问",
    "dodge": "躲开",
    "coy": "撒娇",
    "seek": "求安慰",
}

TONE_NAMES = {
    "plain": "平常",
    "warm": "温和",
    "dry": "淡淡",
    "cold": "冷淡",
    "playful": "闹",
    "sharp": "损",
    "soft": "软",
}

# 每个 act 的默认口吻（可被状态修正）
ACT_TONE = {
    "answer": "plain",
    "care": "warm",
    "share": "plain",
    "tease": "playful",
    "probe": "plain",
    "dodge": "dry",
    "coy": "playful",
    "seek": "soft",
}

# 每个 act 的一句话指令（给模型看的，不是给用户看的）
ACT_HINTS = {
    "answer": "把这句当正经事办，先回答/先解决，不卖萌不绕弯",
    "care": "先接住他的情绪，别急着讲道理",
    "share": "说一句自己的事，别只问他",
    "tease": "损他一句，但要让他听出是在乎",
    "probe": "问一个具体的问题，别泛泛地问",
    "dodge": "话可以短，可以不接，不用硬聊",
    "coy": "撒个娇，直白一点，别绕",
    "seek": "说一句自己的低落，允许他接住",
}

TONE_HINTS = {
    "plain": "正常说话",
    "warm": "语气放软，别损",
    "dry": "话少一点，别解释",
    "cold": "不加语气词，不哄",
    "playful": "可以带笑、带语气词",
    "sharp": "可以刺，但收着点",
    "soft": "声音放轻，短句",
}

# ---------------------------------------------------------------- 证据词表

COMFORT_WORDS = (
    "难受", "撑不住", "烦死", "好累", "累死", "不想活", "崩溃", "想哭", "emo", "扛不住",
    # 身体不适
    "头疼", "头痛", "感冒", "发烧", "生病", "胃疼", "不舒服", "失眠", "睡不着",
    # 受挫
    "挂了", "没过", "失败了", "被拒", "搞砸", "完蛋", "退学", "被骂",
    # 自伤式描述
    "没吃东西", "没吃饭", "吃不下", "瘦了",
    # 求承诺/求陪
    "陪着我", "一直陪", "别离开", "会不会走",
)
SELF_NEG_WORDS = (
    "我是废物", "我没用", "我好菜", "我真笨", "我废物", "我不行", "我太差", "什么都做不好",
    "我很没用", "是不是很没用", "我是不是很没用", "我很失败", "我太失败了",
)
MATERIAL_WORDS = (
    "你呢", "你怎么样", "你的事", "你今天", "你干嘛", "你会不会", "你怎么想", "你在做什么",
    "你喜欢", "你的", "你平时", "你最近", "你有没有", "你怎么看", "你那边", "你那里",
)
ASK_WORDS = ("吗", "什么", "怎么", "为什么", "多少", "哪", "谁", "是不是", "有没有")
# 指代不明 / 说不清：这类消息该追问，不是该正经回答
PROBE_WORDS = (
    "那个", "这个", "它", "有点问题", "算了吧", "你说呢", "怎么办", "弄好了没",
    "还记得", "上次说的", "不知道", "你觉得", "行不行",
)
PLAY_WORDS = ("笨蛋", "傻", "憨", "哈哈", "开玩笑", "逗你", "嘿嘿", "胖子", "小胖", "这么乖", "真乖", "肥鱼你", "小胖子")
# 只有明确的贬义组合才算「骂她」；叫她名字本身不是敌意
ANGRY_AT_BOT = ("你这胖鱼", "你个笨蛋", "你个死鱼", "臭鱼", "死肥鱼")
COMMAND_WORDS = (
    "帮我", "写个", "写一个", "写段", "生成", "查一下", "查查", "搜一下", "翻译",
    "解释一下", "怎么做", "怎么弄", "如何", "报错", "总结一下", "整理", "算一下",
)


# 撒娇的语言标记：她不会主动撒娇，只有对方把话递到跟前才会接。
COY_WORDS = (
    "好不好嘛", "求你了", "哄我", "夸夸", "夸我", "抱抱", "抱我", "亲亲",
    "不理你了", "不理我", "再说一遍", "陪我", "你哄", "亲一个", "贴贴",
)

# 空转信号：没内容、没请求、也没情绪，只是把话头递过来。
EMPTY_WORDS = ("在吗", "在不在", "在么", "随便聊聊", "聊聊天", "没什么")

# 隐私/越界问题：她可以推掉，不必正经回答。
PRIVACY_WORDS = ("多大了", "几岁", "你多大", "真名", "叫什么名字", "住哪", "手机号", "地址", "照片")


def _norm(v: float) -> float:
    return max(0.0, min(1.0, float(v) / 100.0))


def _hit(msg: str, words: tuple) -> bool:
    return any(w in msg for w in words)


# 语法模式：比词表更能泛化到没见过的说法。
RE_ASK_HER = re.compile(r"你[^。！？!?]{0,6}(喜欢|爱好|饿|吃|睡|在干|干嘛|会|要不要|想)")
RE_HESITATE = re.compile(r"(是不是该|要不要|该不该|是不是应该|是不是得).{0,6}(了|吗|呢)?")
RE_MISS = re.compile(r"(想你了|想见你|想抱你|好想你|许久不见)")
RE_GIVEUP = re.compile(r"(算了|不说了|随便吧|懒得说)")
# 上一句是不是在问她：决定「嗯」是应答还是敷衍
RE_QUESTION = re.compile(r"[？?]\s*$|吗|呢|什么|怎么|哪|多少|几[号点岁]")


def _direct_text(msg: str) -> str:
    """提取可归于当前用户的直接表达，供攻击/玩笑与收口证据使用。"""
    text = re.sub(r"```[\s\S]*?```", " ", msg)
    text = re.sub(r'''“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"|'[^'\n]*'|`[^`]*`''', " ", text)
    clauses = re.split(r"[，,。！？!?；;\n]", text)
    return "，".join(
        clause for clause in clauses
        if not re.search(r"(?:他|她|别人|有人|对方).{0,6}(?:说|骂|叫)|(?:不是|并非|没有|没在|别|不要|不许).{0,10}(?:骂|叫|说)", clause)
    )


# ---------------------------------------------------------------- 证据

def evidence(msg: str, play_bias: float = 0.2, user_mood: float = 50.0, prev: str = "", scene: str = "private", prev_asked: bool | None = None) -> dict:
    """从这句话里抽稀疏证据。数值都是 0~1 的「有/没有」强度，不是概率。"""
    msg = (msg or "").strip()
    mood = normalize_user_mood(user_mood)
    mood_need = max(0.0, min(1.0, (55.0 - mood) / 45.0))
    joy = max(0.0, min(1.0, (mood - 65.0) / 35.0))  # 他高兴的时候，她也该高兴

    question = ("？" in msg or "?" in msg) and len(msg) <= 20
    gap = 0.8 if question else (0.30 if len(msg) <= 4 else 0.15)

    command = 1.0 if _hit(msg, COMMAND_WORDS) else 0.0
    direct = _direct_text(msg)
    # 不将“我不是废物”里的否定对象重新当成自我评价。
    self_text = re.sub(r"我(?:真的|真|也)?(?:不是|并非|不算|不觉得自己是).{0,4}(?:废物|没用|不行|笨|菜)", "", direct)
    self_neg = 1.0 if _hit(self_text, SELF_NEG_WORDS) else 0.0
    # 兜底：句子里同时出现"我"和明确的否定组合，才算自我否定
    # （单字"菜/笨"会误伤"我在菜市场""我买菜"，所以只认双字组合）
    if not self_neg and re.search(
        r"我(?:自己)?(?:真的?|觉得自己|感觉自己|就是|是|很|好|太|个|简直|什么都)*"
        r"(?:废物|没用|不行|很菜|太菜|菜鸟|好笨|真笨|太笨)", self_text
    ):
        self_neg = 0.9
    angry = 1.0 if _hit(direct, ANGRY_AT_BOT) else 0.0
    explicit_play = _hit(direct, PLAY_WORDS)

    coy_hint = 1.0 if _hit(msg, COY_WORDS) else 0.0
    probe_hint = 1.0 if _hit(msg, PROBE_WORDS) else 0.0
    ask_her = 1.0 if RE_ASK_HER.search(msg) else 0.0
    hesitate = 1.0 if RE_HESITATE.search(msg) else 0.0
    miss = 1.0 if RE_MISS.search(msg) else 0.0
    # 引用/转述以及「别说算了」都不是当前用户要求结束话题。
    # 「没事了」也可能表示已经恢复，不能单凭这三个字让对话收口。
    giveup = float(any(
        not re.search(r"(?:别|不要|不许)\s*$", direct[:match.start()])
        for match in RE_GIVEUP.finditer(direct)
    ))
    # 攻击证据去掉的引用仍有内容；不能把整段引用误当成空消息。
    empty = 1.0 if (_hit(msg, EMPTY_WORDS) or len(msg) <= 1) else 0.0
    # main 提供的是「上一条机器人回复确实在问当前用户」的明确布尔。
    # False 必须覆盖旧 prev；仅旧调用未提供标志时才兼容 prev 问句推断。
    asked_before = (prev_asked is True) if prev_asked is not None else bool(prev and RE_QUESTION.search(prev))
    if len(msg) <= 1 and asked_before:
        empty = 0.0
    # 私聊「在吗」是来找她，不是空转；群里无人指名的「在吗」仍可不管。
    if str(scene) == "private" and msg.strip() in ("在吗", "在不在", "在么", "在嘛", "在"):
        empty = 0.0
    elif str(scene) == "group" and msg.strip() in ("在吗", "在不在", "在么", "在嘛"):
        empty = 1.0  # 群里没点名，先不插话
    if coy_hint > 0:
        empty = 0.0  # 有明确的撒娇请求就不是空转
    # 隐私词必须指向她本人：「我的地址变了」是在说自己的事，不是打听她。
    privacy = 1.0 if (_hit(msg, PRIVACY_WORDS) and ("你" in msg or "多少" in msg)) else 0.0

    return {
        "coy_hint": coy_hint,
        "probe_hint": probe_hint,
        "ask_her": ask_her,
        "hesitate": hesitate,
        "miss": miss,
        "giveup": giveup,
        "empty": empty,
        "privacy": privacy,
        "comfort": max(1.0 if _hit(msg, COMFORT_WORDS) else 0.15, mood_need),
        "self_neg": self_neg,
        "material": 0.8 if _hit(msg, MATERIAL_WORDS) else 0.15,
        "gap": gap,
        "asked": 1.0 if _hit(msg, ASK_WORDS) else 0.0,
        "play": 0.6 if explicit_play else play_bias,
        "explicit_play": explicit_play,
        "command": command,
        "angry": angry,
        "mood": mood,
        "joy": joy,
        "len": len(msg),
    }


# ---------------------------------------------------------------- 决策

def _force_share(out: dict, lead: str, share: float) -> dict:
    share = max(0.0, min(1.0, share))
    rest = {k: v for k, v in out.items() if k != lead}
    total = sum(rest.values()) or 1.0
    out[lead] = share
    for k in rest:
        out[k] = rest[k] / total * (1.0 - share)
    return out


def decide(
    *,
    persona: dict,
    affect: dict,
    relation: dict,
    scene: str,
    msg: str,
    prev: str = "",
    prev_asked: bool | None = None,
    play_bias: float = 0.2,
    user_mood: float = 50.0,
    aggression_self: float | None = None,
    mentioned: bool = False,
    dodge_streak: int = 0,
    aggression_other: float | None = None,
    affection: float | None = None,
    libido_other: float | None = None,
) -> dict:
    """返回这一轮该做什么、用什么口吻、说几条。

    返回结构::

        {
          "acts": {"answer": 0.31, ...},        # 归一化，和为 1
          "lead": [("answer", 0.31), ...],      # 前两名
          "tone": "plain",
          "tone_hint": "...",
          "act_hint": "...",
          "budget": 2,
          "reason": ["command", "low_affect"],
        }
    """
    # 调用方可能传 None（状态未加载完 / 新用户），一律兜底，别让决策层崩掉整条回复
    persona = persona or {}
    affect = affect or {}
    relation = relation or {}
    scene = scene or "private"

    ev = evidence(msg, play_bias, user_mood, prev=prev, scene=scene, prev_asked=prev_asked)
    p = {k: _norm(persona.get(k, 50)) for k in ("O", "C", "E", "A", "N")}
    a = affect or {}
    P, Ar = _norm(a.get("P", 60)), _norm(a.get("Ar", 45))
    T = _norm(relation.get("T", 55))
    I = _norm(relation.get("I", 40))
    group = 1.0 if scene == "group" else 0.0
    # 对他攻击性使用情绪管理的 0~50 单位。旧 aggression_self 参数保留兼容，
    # 自我攻击性不再被解释成对用户说话尖锐的许可；尚未接通时保持轻松口吻。
    agg = None
    if aggression_other is not None:
        try:
            value = float(aggression_other)
            if math.isfinite(value):
                agg = max(0.0, min(1.0, value / 50.0))
        except (TypeError, ValueError, OverflowError):
            pass
    # 好感与欲望：只做小幅加权（±0.1），不做双向写回，
    # 避免两套数值互相喂养产生震荡。
    aff_lvl = None
    if affection is not None:
        try:
            v = float(affection)
            if math.isfinite(v):
                aff_lvl = max(0.0, min(1.0, v / 100.0))
        except (TypeError, ValueError, OverflowError):
            pass
    lib_lvl = None
    if libido_other is not None:
        try:
            v = float(libido_other)
            if math.isfinite(v):
                lib_lvl = max(0.0, min(1.0, v / 50.0))
        except (TypeError, ValueError, OverflowError):
            pass
    support_needed = bool(ev["self_neg"]) or ev["comfort"] > 0.6
    # 明确任务和强烈关怀优先；普通的退出请求不再靠分数碰运气。
    close_topic = bool(ev["giveup"]) and not ev["command"] and not support_needed
    reason: list[str] = []

    # ---- 八个动作的基础分（证据主导，性格只作底色）
    answer = 0.32 + 0.50 * ev["command"] + 0.20 * ev["asked"] * (1.0 - ev["play"])
    care = 0.50 * ev["comfort"] + 0.30 * ev["self_neg"] + 0.20 * I * (1.0 - p["N"])         + 0.35 * ev["hesitate"] + 0.45 * ev["miss"]
    share = 0.55 * ev["material"] + 0.25 * P + 0.20 * p["E"] + 0.55 * ev["ask_her"]
    tease = 0.45 * ev["play"] + 0.25 * P + 0.20 * (1.0 - p["A"]) + 0.10 * T
    probe = 0.42 * ev["gap"] + 0.18 * p["O"] + 0.12 * p["E"] + 0.55 * ev["probe_hint"]         + 0.30 * ev["hesitate"]
    # 躲开：对方只是递话头（空转/越界），或她这一刻本来就不想接。
    dodge = 0.30 * (1.0 - Ar) + 0.15 * (1.0 - I) + 0.18 * group         + 0.55 * ev["empty"] + 0.35 * ev["privacy"]
    # 撒娇：语言标记为主，关系近 + 心情好只作加成（缺标记就不成立）
    coy = 0.65 * ev["coy_hint"] + 0.18 * T * P
    # 求安慰：自己情绪低 + 神经质高（内在驱动，不由对方消息决定）
    seek = 0.70 * (1.0 - P) * (0.35 + 0.65 * p["N"])
    # 对方说「算了/不说了」：收口，不追问也不再自说自话
    if close_topic:
        probe *= 0.15
        share *= 0.30
        answer += 0.25
        reason.append("giveup_close")
    # 好感高：更愿意哄、也更敢损（亲近的人才敢开玩笑）
    if aff_lvl is not None:
        care += 0.10 * (aff_lvl - 0.5) * 2.0
        tease += 0.08 * (aff_lvl - 0.5) * 2.0
    # 欲望高：更愿意讲自己的、也更黏
    if lib_lvl is not None:
        share += 0.06 * lib_lvl
        coy += 0.08 * lib_lvl

    # ---- 弱信号兜底：对方只回了个「嗯」「哦」，没有值得反应的证据。
    # 这时候不该演，也不该装死——正常接一句就是最优解。
    signal = max(
        ev["command"], ev["comfort"], ev["self_neg"], ev["angry"],
        ev["play"] if ev["play"] > play_bias + 1e-6 else 0.0,
        ev["material"] if ev["material"] > 0.2 else 0.0,
        ev["asked"] * 0.6,
    )
    if signal < 0.30:
        # 群里没人点她名，就安分点；私聊则正常接话
        quiet = group and not mentioned
        # 群里没人点名也要能答事实题：answer 兜底不能低于 share 的底噪，
        # 否则「曼城 17 号是谁」会被判成"讲自己的事"（标定集实测）。
        answer = max(answer, 0.36 if quiet else 0.40)
        dodge *= 1.0 if ev["empty"] or ev["privacy"] else 0.55
        if quiet:
            tease *= 0.40  # 群里没人逗她，别自己凑上去
        reason.append("weak_signal")

    # 他心情好：跟着高兴，别一本正经
    if ev["joy"] > 0.35:
        answer += 0.22 * ev["joy"]
        reason.append("user_joy")

    # ---- 硬规则（场景算错了会很违和，优先于公式）
    if ev["command"]:
        answer = max(answer, 0.72)
        tease *= 0.25
        dodge *= 0.20
        probe *= 0.60
        reason.append("command")
    if ev["self_neg"]:
        care = max(care, 0.78)
        tease *= 0.10
        probe *= 0.50
        reason.append("self_negative")
    if ev["angry"]:
        tease = max(tease, 0.70)
        # 他若同时在示弱/求安慰，先接住情绪，别趁机怼
        if not ev["self_neg"] and ev["comfort"] <= 0.6:
            care = min(care, 0.12)
        reason.append("angry_at_bot")
    if ev["comfort"] > 0.6:
        care = max(care, 0.65)
        dodge *= 0.25
        probe *= 0.50
        reason.append("comfort_need")
    if ev["mood"] < 40:
        probe *= 0.70
    if ev["asked"] and ev["len"] > 10:
        dodge *= 0.30
        probe *= 0.70
        reason.append("factual_question")
    if group and ev["command"]:
        dodge *= 0.5  # 群里被点名办事，不能装死
    if mentioned:
        # @ 她或叫了她的名字：必须接话，不能靠群聊轻量模式滑过去。
        # 但若他同时在示弱/求安慰（care 已高），别用「应答」把关心压掉。
        dodge *= 0.15
        if care < 0.6 and tease < 0.6:
            answer = max(answer, max(share, probe) + 0.06, 0.55)
        reason.append("mentioned")

    raw = {
        "answer": answer, "care": care, "share": share, "tease": tease,
        "probe": probe, "dodge": dodge, "coy": coy, "seek": seek,
    }

    # 主行为资格先于排序。明确任务与关怀可以并存，不竞争唯一名额。
    secondary_acts = []
    if ev["command"]:
        allowed = {"answer"}
        if support_needed:
            secondary_acts.append("care")
            reason.append("task_with_support")
    elif support_needed:
        allowed = {"care"}
        reason.append("distress_guard")
    elif close_topic:
        allowed = {"answer"}
    else:
        allowed = set(raw)
        if not (ev["explicit_play"] or ev["angry"]):
            allowed.discard("tease")
        # 撒娇资格由语言标记决定：对方递了撒娇的话头（嘛/抱抱/夸夸）才接。
        # 旧条件要求 explicit_play + T/I 都很高，结果 coc 永远进不了候选。
        if not (ev["coy_hint"] > 0 and T >= 0.5 and I >= 0.40):
            allowed.discard("coy")
        # 与生疏档「别主动讲自己的事」保持一致；低落不构成越过关系边界的理由。
        if I < 0.40:
            allowed.discard("seek")

    # ---- softmax 拉开差距（温度低=更坚决）
    t = 0.10
    mx = max(raw[k] for k in allowed)
    exp = {k: math.exp((v - mx) / t) if k in allowed else 0.0 for k, v in raw.items()}
    tot = sum(exp.values()) or 1.0
    acts = {k: v / tot for k, v in exp.items()}

    lead = sorted(((k, acts[k]) for k in raw if k in allowed), key=lambda kv: kv[1], reverse=True)[:2]
    lead_act = lead[0][0]

    # ---- 口吻：以 act 的默认口吻为骨，用状态微调
    tone = ACT_TONE.get(lead_act, "plain")
    if lead_act == "tease":
        tone = "sharp" if agg is not None and agg > 0.6 else "playful"
    elif lead_act == "dodge":
        tone = "cold" if Ar < 0.35 else "dry"
    elif lead_act == "share":
        tone = "warm" if P > 0.60 else "plain"
    elif lead_act == "care" and P > 0.55:
        tone = "warm"
    elif lead_act == "answer" and (support_needed or ev["joy"] > 0.35):
        tone = "warm"
    elif lead_act == "answer" and T > 0.7 and ev["play"] > 0.4:
        tone = "playful"  # 熟人问正经事也可以带点笑

    # ---- 这一轮说几条
    lead_score = lead[0][1]
    if close_topic:
        budget = 1
    elif lead_act == "dodge":
        # 预算由证据决定，不由归一化分数决定（否则调温度就会改回几条）。
        # 对方只是在递话头 / 越界提问 -> 可以不出声；有实质内容 -> 至少回一句。
        substantive = (ev["material"] > 0.5 or ev["comfort"] > 0.5
                       or ev["self_neg"] > 0.5 or bool(ev["command"]))
        silent_ok = ((not substantive) and ev["privacy"] < 0.5) or ev["empty"] > 0.5
        if ev["privacy"] > 0.5:
            budget = 1
            reason.append("privacy_deflect")
            silent_ok = False
        budget = 0 if (silent_ok and dodge_streak < 1) else 1
        if budget == 1:
            # 连续第二次装死强制开口：态度像人可以，体验像宕机不行
            reason.append("dodge_streak_break" if dodge_streak >= 1 else "dodge_one_line")
    elif lead_act in ("share", "tease", "care") and lead_score > 0.5:
        budget = 3 if scene == "group" else 4
    elif lead_act == "answer":
        budget = 3 if ev["command"] else 2
    else:
        budget = 2

    return {
        "acts": acts,
        "lead": lead,
        "tone": tone,
        "tone_hint": TONE_HINTS.get(tone, ""),
        "act_hint": (
            "顺着他的意思收住这个话题，简短回应一句，不追问、不续聊，也不把话题转到自己身上"
            if close_topic else ACT_HINTS.get(lead_act, "")
        ),
        "budget": budget,
        "reason": reason,
        "secondary_acts": secondary_acts,
        "length_mode": "task" if ev["command"] else "chat",
        "close_topic": close_topic,
    }


def brief(decision: dict) -> str:
    """一行摘要，给日志和调试用。"""
    lead = decision.get("lead") or [("answer", 0.0)]
    name = ACT_NAMES.get(lead[0][0], lead[0][0])
    tone = TONE_NAMES.get(decision.get("tone", "plain"), "")
    return f"{name}({lead[0][1]:.2f}) / {tone} / {decision.get('budget', 2)}条"
