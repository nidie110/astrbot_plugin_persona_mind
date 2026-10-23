"""人物侧写：每个对话者一份档案，慢慢认识。

理论出处：
- Altman & Taylor, *Social Penetration* (1973)：关系靠逐步加深的自我披露推进。
- 工程上没有直接照搬，只取了「三层结构」：事实层（身份/作息）、偏好层（兴趣/雷点）、
  动态层（近况/待确认），并且**把猜测和事实分开**——猜错一次比不猜更伤人。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .affect import clamp100, num_or

SUMMARIZE_PROMPT = """你在帮一个聊天机器人整理「她对某个人的了解」。只输出 JSON，不要解释。

这个人最近说的话：
{dialogues}

已知档案：
{profile}

请更新档案，只输出需要修改或新增的字段（没把握的放 note，别硬写）：
{{"称呼":"","身份":"","作息":"","在做什么":"","兴趣":[],"雷点":[],"说话习惯":{{}},"近况":[{{"时间":"","事":""}}],"note":[]}}"""

# 隐私白名单：只允许这些字段落盘
ALLOWED_TEXT = ("称呼", "身份", "作息", "在做什么")
ALLOWED_LIST = ("兴趣", "雷点", "note")

SENSITIVE = re.compile(r"\d{11,}|身份证|住址|小区|门牌|银行卡|密码|验证码")


def empty(uid: str, trust: float = 55.0, intimacy: float = 40.0) -> dict:
    return {
        "uid": uid,
        "昵称": "",
        "T": trust,
        "I": intimacy,
        "count": 0,
        "last_seen": 0,
        "称呼": "",
        "身份": "",
        "作息": "",
        "在做什么": "",
        "兴趣": [],
        "雷点": [],
        "说话习惯": {},
        "近况": [],
        "note": [],
        "recent": [],
    }


class ProfileStore:
    def __init__(
        self,
        directory: Path,
        trust: float = 55.0,
        intimacy: float = 40.0,
        new_intimacy: float = 20.0,
    ):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trust = trust
        self.intimacy = intimacy        # 老档案缺字段时的兜底
        self.new_intimacy = new_intimacy  # 全新的人：从生疏档开始

    def _path(self, uid: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z_.-]", "_", str(uid))[:64] or "unknown"
        return self.dir / f"{safe}.json"

    def load(self, uid: str) -> dict:
        p = self._path(uid)
        # 有档案用老默认值兜底，没有档案的新人从生疏档起步
        base = empty(uid, self.trust, self.intimacy if p.exists() else self.new_intimacy)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    base.update(data)
            except Exception:
                pass
        return base

    def save(self, profile: dict) -> None:
        keep = {k: v for k, v in profile.items() if k != "recent"}
        keep["recent"] = profile.get("recent", [])[-20:]
        self._path(profile["uid"]).write_text(
            json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def summarize_prompt(self, uid: str) -> str:
        prof = self.load(uid)
        msgs = prof.get("recent", [])[-20:]
        payload = {
            k: prof.get(k)
            for k in ("称呼", "身份", "作息", "在做什么", "兴趣", "雷点", "说话习惯", "近况", "note")
        }
        return SUMMARIZE_PROMPT.format(
            dialogues="\n".join(f"- {m}" for m in msgs),
            profile=json.dumps(payload, ensure_ascii=False),
        )

    def merge_patch(self, uid: str, patch: dict) -> dict:
        """把模型给的补丁并进档案；带隐私过滤与长度上限。"""
        prof = self.load(uid)
        for key in ALLOWED_TEXT:
            val = str(patch.get(key) or "").strip()
            if val and len(val) <= 40 and not SENSITIVE.search(val):
                prof[key] = val
        for key in ALLOWED_LIST:
            for item in patch.get(key) or []:
                item = str(item).strip()
                if not item or len(item) > 20 or SENSITIVE.search(item):
                    continue
                if item not in prof.get(key, []):
                    prof.setdefault(key, []).append(item)
            prof[key] = prof.get(key, [])[-8:]
        habits = patch.get("说话习惯")
        if isinstance(habits, dict):
            for k, v in habits.items():
                if isinstance(v, (str, int, float)) and len(str(v)) <= 20:
                    prof.setdefault("说话习惯", {})[str(k)[:12]] = v
        for item in patch.get("近况") or []:
            if isinstance(item, dict) and item.get("事"):
                event = str(item["事"])[:40]
                if SENSITIVE.search(event):
                    continue
                prof.setdefault("近况", []).append(
                    {"时间": str(item.get("时间", ""))[:10], "事": event}
                )
        prof["近况"] = prof.get("近况", [])[-10:]
        prof["last_seen"] = int(time.time())
        self.save(prof)
        return prof

    def touch(
        self,
        uid: str,
        message: str,
        intimacy_gain: float = 0.3,
        negative: bool = False,
        scene: str = "private",
        cap_per_day: float = 8.0,
    ) -> dict:
        """推进关系。

        比起「每说一句就 +0.3」，这里做了三件事：
        1. 有实质内容才涨（「嗯」「在吗」不该涨亲密度）；
        2. 每天有上限，防止刷屏把关系刷满；
        3. 被骂会掉（negative=True），关系不是只进不出的。
        群聊里别人说话不推进她对某个人的关系，只有私聊或被点名才算。
        """
        prof = self.load(uid)
        prof["count"] = int(prof.get("count", 0)) + 1

        today = time.strftime("%Y-%m-%d")
        if prof.get("I_date") != today:
            prof["I_date"] = today
            prof["I_today"] = 0.0

        text = (message or "").strip()
        # 档案来自磁盘 JSON，可能被手工改过或写过 NaN/inf；所有数值先收敛再算。
        gain = max(0.0, num_or(intimacy_gain, 0.0))
        cap = max(0.0, num_or(cap_per_day, 0.0))
        if negative:
            delta = -0.6
        elif scene == "private" and len(text) >= 4:
            used = max(0.0, num_or(prof.get("I_today"), 0.0))
            delta = min(gain, max(0.0, cap - used))
        else:
            delta = 0.0

        prev = clamp100(num_or(prof.get("I"), self.intimacy))
        prof["I"] = round(max(0.0, min(100.0, prev + delta)), 1)
        if delta > 0:
            prof["I_today"] = round(min(cap, max(0.0, num_or(prof.get("I_today"), 0.0)) + delta), 2)
        if scene == "private":
            prof.setdefault("recent", []).append(text[:60])
            prof["recent"] = prof["recent"][-20:]
        prof["last_seen"] = int(time.time())
        self.save(prof)
        return prof


def summary_line(profile: dict) -> str:
    """给注入用的 150 字摘要。"""
    bits = []
    if profile.get("称呼"):
        bits.append(f"叫他{profile['称呼']}")
    for key in ("身份", "作息"):
        if profile.get(key):
            bits.append(str(profile[key]))
    if profile.get("兴趣"):
        bits.append("喜欢" + "、".join(map(str, profile["兴趣"][:3])))
    if profile.get("雷点"):
        bits.append("别提" + "、".join(map(str, profile["雷点"][:2])))
    if profile.get("近况"):
        bits.append("最近：" + str(profile["近况"][-1].get("事") or ""))
    return "；".join([b for b in bits if b])[:150]
