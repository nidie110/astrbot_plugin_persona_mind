"""语料库：把「她平时怎么说话」从设定里分离出来。

为什么单独做一个模块：
- 人格设定描述的是「她是谁」，语料描述的是「她怎么开口」。
  两者混在一起时，模型更容易只记住设定、退回默认腔调。
- 语料是**可替换**的：换成任何角色/方言/圈层口吻，只要按分组写几行，
  引擎会按当前意图自动抽对应分组。
"""

from __future__ import annotations

import random
from pathlib import Path

# 意图 → 语料分组名
INTENT_SECTION = {
    "care": "关心",
    "tease": "怼回去",
    "share": "说自己的日子",
    "probe": "追问",
    "dodge": "敷衍",
}

# 额外混入的分组（让语气不单调）
EXTRA_SECTIONS = ("亲近", "闹着玩", "兴奋", "直球夸人", "撒娇")


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

    def pick(self, lead_intent: str, n: int = 3, rng: random.Random | None = None) -> list[str]:
        rng = rng or random
        sec = INTENT_SECTION.get(lead_intent)
        pool = list(self.sections.get(sec, [])) if sec else []
        extra: list[str] = []
        for name in EXTRA_SECTIONS:
            extra.extend(self.sections.get(name, []))
        out: list[str] = []
        if pool:
            out += rng.sample(pool, min(2, len(pool)))
        if extra and len(out) < n:
            out += rng.sample(extra, min(n - len(out), len(extra)))
        return out[:n]
