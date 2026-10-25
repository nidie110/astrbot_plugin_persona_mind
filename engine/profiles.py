"""人物侧写：每个对话者一份档案，慢慢认识。

理论出处：
- Altman & Taylor, *Social Penetration* (1973)：关系靠逐步加深的自我披露推进。
- 工程上没有直接照搬，只取了「三层结构」：事实层（身份/作息）、偏好层（兴趣/雷点）、
  动态层（近况/待确认），并且**把猜测和事实分开**——猜错一次比不猜更伤人。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

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


def _pos(value, default: float, lo: float, hi: float) -> float:
    """配置值归一化：非数字/NaN 回落到默认值，再夹进区间。

    构造参数可能被直接调用方传脏（字符串、负数、inf），在这里兜一层，
    比让 touch() 在跑消息的路径上抛 TypeError 强。
    """
    v = float(num_or(value, default))
    return min(hi, max(lo, v))

# 侧写素材的门：整句都是语气词/标点/数字的不留（"哈哈哈哈""嗯""1"），
# 但「在成都」「叫我老王」「夜班」这种短事实必须留——它们正是称呼/身份/作息的原料。
_NOISE = re.compile(r"^[\s哈呵嘿嗨嗯哦啊呀吧呢嘛的了呃喔额噢0-9。，,\.!！?？~～…、\-—_]+$")
_CJK = re.compile(r"[\u4e00-\u9fa5]{2,}")
_LATIN = re.compile(r"[A-Za-z]{2,}")
# 有实词但没信息量的寒暄：单独列，靠长度判断会误杀短事实
_SMALLTALK = {
    "在吗", "你好", "晚安", "早安", "好的", "收到", "好耶", "谢谢", "么么", "牛逼",
    "厉害", "哈哈", "在不在", "睡了", "吃了吗", "干嘛呢", "没事", "算了",
}


def _worth_remembering(text: str, recent: list) -> bool:
    """这条消息值不值得进侧写素材：非语气词、有实词、且不重复。"""
    t = (text or "").strip()
    if not t or _NOISE.match(t):
        return False
    core = t.rstrip("啦呀啊哦哟嘛哈")
    if len(core) <= 4 and core.strip() in _SMALLTALK:
        return False
    if not [r for r in _CJK.findall(core) if r not in _SMALLTALK] and not _LATIN.search(core):
        return False
    return t[:60] not in recent


def empty(uid: str, trust: float = 55.0, intimacy: float = 40.0) -> dict:
    return {
        "uid": uid,
        "昵称": "",
        "T": trust,
        "I": intimacy,
        # 信任 T 的推进账本：T_day 记账日、T_today 当日量、T_days 来往过几天
        "T_day": "",
        "T_today": 0.0,
        "T_days": 0,
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
        trust_day_gain: float = 0.4,
        trust_share_gain: float = 0.1,
        trust_penalty: float = 0.8,
        trust_cap_day: float = 1.5,
    ):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trust = trust
        self.intimacy = intimacy        # 老档案缺字段时的兜底
        self.new_intimacy = new_intimacy  # 全新的人：从生疏档开始
        self.trust_day_gain = _pos(trust_day_gain, 0.4, 0.0, 5.0)      # 隔天还来：可预期
        self.trust_share_gain = _pos(trust_share_gain, 0.1, 0.0, 5.0)  # 愿意多讲：自我披露
        self.trust_penalty = _pos(trust_penalty, 0.8, 0.0, 10.0)       # 被攻击：一次抵两天
        self.trust_cap_day = _pos(trust_cap_day, 1.5, 0.0, 20.0)       # 当日净额度，防刷屏

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
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 解析不了 ≠ 档案不存在：留一份 .corrupt 备查，别让下一次 save 把
                # 默认档写回去、把称呼/身份/兴趣这些一次性抹掉。
                bad = p.with_suffix(".json.corrupt")
                try:
                    p.replace(bad)
                except OSError:
                    pass
                logger.warning("[mind] 档案损坏，已另存为 %s 并重新建档", bad)
            except OSError:
                logger.warning("[mind] 档案读取失败：%s", p)
        return base

    def save(self, profile: dict) -> None:
        keep = {k: v for k, v in profile.items() if k != "recent"}
        keep["recent"] = profile.get("recent", [])[-20:]
        path = self._path(profile["uid"])
        path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写 .tmp 再 os.replace，进程半路被 kill 也不会留下半个 JSON
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

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
        today = time.strftime("%Y-%m-%d")
        text = (message or "").strip()
        # 同一条消息在一轮里可能触发多次钩子（重试/多请求）：按指纹只结算一次，
        # 否则 -0.8 会被扣两三次
        digest = hashlib.md5(f"{uid}\x00{text}".encode("utf-8")).hexdigest()
        seen = self.__dict__.setdefault("_touch_seen", {})
        if seen.get(digest) == today:
            return self.load(uid)
        if len(seen) > 400:
            # 先剪枝再把局部名指到新 dict，否则下面写入的指纹会留在被丢弃的旧对象里，
            # 超过 400 条之后当天去重就整个失效了
            seen = self.__dict__["_touch_seen"] = {
                k: v for k, v in seen.items() if v == today
            }
        seen[digest] = today

        prof = self.load(uid)
        prof["count"] = int(prof.get("count", 0)) + 1
        if prof.get("I_date") != today:
            prof["I_date"] = today
            prof["I_today"] = 0.0

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

        # ── 信任 T：靠「跨天还在」和「愿意交底」，不靠刷屏 ─────────
        # 账本口径（复审后统一）：T_today 记当天「净变化」，涨和跌共用 trust_cap_day
        # 这一个额度，所以一天最多 +cap、最多 -cap，刷屏和连骂都不会把账算飞。
        # 三条规则：
        # 1. 第一次来不算忠诚；私聊里隔天又出现，才说明可预期（群聊只滚动日期）；
        # 2. 私聊里愿意多讲几句是自我披露，慢慢加；
        # 3. 被攻击会掉，一次抵两天。
        cap_t = max(0.0, float(num_or(self.trust_cap_day, 1.5)))
        spent = num_or(prof.get("T_today"), 0.0)
        delta_t = 0.0
        if prof.get("T_day") != today:
            prof["T_day"] = today
            prof["T_today"] = 0.0
            spent = 0.0
            if scene == "private":
                prev_days = int(num_or(prof.get("T_days"), 0))
                prof["T_days"] = prev_days + 1
                if prev_days >= 1:
                    delta_t += min(self.trust_day_gain, max(0.0, cap_t - spent))
        if (not negative) and scene == "private" and _worth_remembering(text, []):
            delta_t += min(self.trust_share_gain, max(0.0, cap_t - spent - delta_t))
        if negative:
            delta_t -= min(self.trust_penalty, max(0.0, cap_t + spent))
        if delta_t:
            prof["T_today"] = round(spent + delta_t, 2)
            base_t = clamp100(num_or(prof.get("T"), self.trust))
            prof["T"] = round(max(0.0, min(100.0, base_t + delta_t)), 2)

        if (
            scene == "private"
            and not SENSITIVE.search(text)
            and _worth_remembering(text, prof.get("recent") or [])
        ):
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
