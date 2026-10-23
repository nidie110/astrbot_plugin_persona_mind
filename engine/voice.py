"""语料库：把「她平时怎么说话」从设定里分离出来。

为什么单独做一个模块：
- 人格设定描述的是「她是谁」，语料描述的是「她怎么开口」。
  两者混在一起时，模型更容易只记住设定、退回默认腔调。
- 语料是**可替换**的：换成任何角色/方言/圈层口吻，只要按分组写几行，
  引擎会按当前意图自动抽对应分组。

抽样要过三道闸，缺一道就会出洋相：
1. 行为（act）：关心的时候不能递一句损人的
2. 口吻（tone）：冷着脸的时候不能递一句撒娇的
3. 关系（I）：刚认识不能递一句「别跑啊 我还没说完」
"""

from __future__ import annotations

import random
from pathlib import Path

# 主行为 → 语料分组名
# answer 故意不配：正常回答问题不该被口吻样本带偏，让模型自己组织。
# seek（求安慰）配「撒娇」：她自己也难过时，是先示弱再要人哄，不是去关心别人。
INTENT_SECTION = {
    "care": "关心",
    "tease": "怼回去",
    "share": "说自己的日子",
    "probe": "追问",
    "dodge": "敷衍",
    "coy": "撒娇",
    "seek": "撒娇",
}

# 额外混入的分组（让语气不单调）
EXTRA_SECTIONS = ("热情", "亲近", "闹着玩", "撒娇")

# 关系阶段 → 允许用的分组。
# 「热情」放在最低档：夸人厉害、看到好事就叫好，不要求关系深度；
# 但「撒娇」「亲近」「直球夸人（亲密向）」「诗与狂言」要有关系才成立
# ——生疏期掉书袋，比不说更尴尬。
TIER_SECTIONS = {
    0: {"关心", "追问", "敷衍", "怼回去", "热情"},
    1: {"关心", "追问", "敷衍", "怼回去", "诗与狂言", "热情", "说自己的日子", "闹着玩"},
    2: {"关心", "追问", "敷衍", "怼回去", "诗与狂言", "热情", "说自己的日子", "闹着玩",
        "兴奋", "直球夸人", "撒娇", "亲近"},
}

# 行为 → 语境上不该出现的分组
ACT_BAN = {
    # 关心的时候损他、或者"说中了说中了"，前面所有铺垫都白做
    "care": {"闹着玩", "怼回去", "兴奋", "热情", "诗与狂言", "亲近", "撒娇"},
    "seek": {"闹着玩", "怼回去", "兴奋", "热情"},
    "tease": {"诗与狂言", "撒娇"},             # 损人的时候文绉绉或撒娇都不对味
    "probe": {"闹着玩"},
    "coy": {"怼回去"},
}

# 口吻 → 不该出现的分组
TONE_BAN = {
    "cold": {"撒娇", "亲近", "直球夸人", "热情", "闹着玩"},
    "sharp": {"撒娇", "亲近"},
    "dry": {"撒娇"},
    "soft": {"怼回去"},
}

# 他情绪很低时，玩笑类一律不进
LOW_MOOD_BAN = {"闹着玩", "兴奋", "热情"}

# 「诗与狂言」不跟固定行为绑定，只在自述且口吻轻快时小概率冒出来
POETIC_SECTION = "诗与狂言"
POETIC_RATE = 0.15


def tier_of(relation: dict | None) -> int:
    """按亲密度 I 分关系阶段：<30 生疏 / 30-60 熟 / ≥60 亲近。"""
    try:
        i = float((relation or {}).get("I", 20))
    except (TypeError, ValueError):
        i = 20.0
    if i >= 60:
        return 2
    if i >= 30:
        return 1
    return 0


class VoiceCorpus:
    def __init__(self, path: str | Path):
        self.path = Path(path) if path else None
        self.sections: dict[str, list[str]] = {}
        self.load()

    def load(self) -> int:
        self.sections = {}
        if not self.path or not self.path.is_file():
            return 0
        cur = "其他"
        for line in self.path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("## "):
                cur = line[3:].strip()
                continue
            if not line or line.startswith(("#", ">")):
                continue
            self.sections.setdefault(cur, []).append(line)
        return sum(len(v) for v in self.sections.values())

    def count(self) -> int:
        return sum(len(v) for v in self.sections.values())

    def allowed_sections(
        self,
        act: str,
        tone: str = "plain",
        relation: dict | None = None,
        user_mood: float = 50.0,
    ) -> set[str]:
        """行为 × 口吻 × 关系 三道闸，返回这一轮允许用的分组名。"""
        allowed = set(TIER_SECTIONS[tier_of(relation)])
        allowed -= ACT_BAN.get(act, set())
        allowed -= TONE_BAN.get(tone, set())
        try:
            if float(user_mood) < 30:
                allowed -= LOW_MOOD_BAN
        except (TypeError, ValueError):
            pass
        return allowed

    def pick(
        self,
        act: str,
        n: int = 3,
        rng: random.Random | None = None,
        relation: dict | None = None,
        tone: str = "plain",
        user_mood: float = 50.0,
    ) -> list[str]:
        rng = rng or random
        allowed = self.allowed_sections(act, tone, relation, user_mood)
        sec = INTENT_SECTION.get(act)
        pool = list(self.sections.get(sec, [])) if sec and sec in allowed else []
        extra: list[str] = []
        for name in EXTRA_SECTIONS:
            if name in allowed:
                extra.extend(self.sections.get(name, []))
        # 主池空了就不硬凑：为了凑满三句去跨语境补样本，比少给两句更糟。
        # 生疏期的 share 就属于这种情况——没有自述样本，让模型自己说。
        if not pool:
            return []
        out: list[str] = list(rng.sample(pool, min(2, len(pool))))
        if extra and len(out) < n:
            out += rng.sample(extra, min(n - len(out), len(extra)))
        # 诗与狂言：不绑行为，自述 + 轻快时才偶尔冒一句。
        # 注意要"顶掉"最后一条而不是 append——append 之后会被 out[:n] 截掉，
        # 那样这一组等于永远抽不到。
        if (
            act == "share"
            and tone in ("playful", "warm")
            and POETIC_SECTION in allowed
            and self.sections.get(POETIC_SECTION)
            and rng.random() < POETIC_RATE
        ):
            poem = rng.choice(self.sections[POETIC_SECTION])
            if out:
                out[-1] = poem
            else:
                out.append(poem)
        return out[:n]
