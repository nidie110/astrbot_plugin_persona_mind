"""PAD 情绪状态：三个连续值 + 衰减。

理论出处：Mehrabian & Russell, *An Approach to Environmental Psychology* (1974)。
Pleasure–Arousal–Dominance 三维能描述绝大多数情绪状态，
比"生气/开心"这种离散标签更适合连续变化的对话场景。

两层结构见文件末尾的 Mood：
- core 是「她自己的心情」，跨会话共享、慢衰减；
- scene 是「在这个场合的残留」，按会话隔离，存相对基线的偏移，新场景从 0 开始。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, field

DIMS = ("P", "Ar", "D")

# engine 层不 import astrbot（保证可单测），日志走标准库。
logger = logging.getLogger(__name__)


@dataclass
class Affect:
    """她此刻的情绪状态（0~100）。"""

    P: float = 60.0   # pleasure 愉悦
    Ar: float = 45.0  # arousal 唤醒
    D: float = 50.0   # dominance 支配/底气

    # 基线（衰减目标）
    base: dict = field(default_factory=lambda: {"P": 60.0, "Ar": 45.0, "D": 50.0})
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """公开构造入口也要收敛：Affect(80, nan, -5) 这种不再把脏值带进运算。"""
        for name, default in (("P", 60.0), ("Ar", 45.0), ("D", 50.0)):
            setattr(self, name, clamp100(num_or(getattr(self, name), default)))
        if not isinstance(self.base, dict):
            self.base = {"P": self.P, "Ar": self.Ar, "D": self.D}
        for k in ("P", "Ar", "D"):
            self.base[k] = clamp100(num_or(self.base.get(k), getattr(self, k)))
        self.ts = num_or(self.ts, time.time())

    def decay(self, minutes_per_step: float = 5.0, ratio_per_step: float = 0.2) -> None:
        """向基线回归。生气 5~10 分钟就淡了，不会记一天。"""
        minutes_per_step = max(0.1, num_or(minutes_per_step, 5.0))
        ratio_per_step = max(0.0, min(1.0, num_or(ratio_per_step, 0.2)))
        elapsed_min = max(0.0, (time.time() - num_or(self.ts, time.time())) / 60.0)
        steps = elapsed_min / minutes_per_step
        if steps <= 0:
            return
        k = min(1.0, steps * ratio_per_step)
        for name in DIMS:
            cur = getattr(self, name)
            setattr(self, name, round(cur + (self.base[name] - cur) * k, 1))
        self.ts = time.time()

    def apply(self, effects: dict, clamp: float = 20.0) -> None:
        """叠加一次情绪事件；单次变化幅度做上限，避免一条消息把人炸上天。

        这是底层公开入口，不能假设调用方已经过滤过数值：
        NaN/inf/bool 若进到 `float(delta)`，NaN 会绕过 max/min 直接把维度写成 NaN，
        之后整条链路（decay、effective、提示词）都被污染。
        """
        cap = max(0.0, num_or(clamp, 20.0))  # 负值等于不禁用，统一归零
        changed = False
        for name, delta in (effects or {}).items():
            if name not in DIMS:
                continue
            d = num_or(delta, None)
            if d is None:
                continue
            d = max(-cap, min(cap, d))
            if d == 0.0:
                continue
            setattr(self, name, clamp100(getattr(self, name) + d))
            changed = True
        if changed:
            self.ts = time.time()

    def as_dict(self) -> dict:
        return {"P": round(self.P, 1), "Ar": round(self.Ar, 1), "D": round(self.D, 1)}

    def load(self, data: dict) -> None:
        """从持久化数据恢复。所有数值都过 num_or：state.json 里写 Infinity/NaN 是合法的 JSON。"""
        for name in DIMS:
            if name in data:
                setattr(self, name, clamp100(num_or(data[name], getattr(self, name))))
        if "ts" in data:
            setattr(self, "ts", num_or(data["ts"], self.ts))
        if isinstance(data.get("base"), dict):
            for k, v in data["base"].items():
                if k in DIMS:
                    self.base[k] = clamp100(num_or(v, self.base[k]))

    def snapshot(self) -> dict:
        return {**asdict(self)}


@dataclass
class CoreEvent:
    """一次打进 core 的冲量事件，区分「获准」与「实际落地」。

    为什么分两个量（GPT 第 4/5 轮评审）：
    - granted（获准量）：配额计算用它。饱和事件也占额度，否则连刷饱和后
      真实事件仍能全额进入，防刷形同虚设。
    - actual（实际变化）：解释状态为什么没动（core 偏移撞到 ±60 边界）。
    重复折扣按「有效非零事件」计数，含饱和事件。
    """

    ts: float
    umo: str
    sign: int
    granted: dict = field(default_factory=dict)
    actual: dict = field(default_factory=dict)


def normalize01(v: float) -> float:
    return max(0.0, min(1.0, num_or(v, 0.0) / 100.0))


def clamp100(v: float) -> float:
    return max(0.0, min(100.0, float(v)))


def num_or(value, default: float) -> float:
    """只对「缺失/非法」回退默认值；真实的 0 和负数必须原样保留。

    不能用 `value or default`：好感度归零、攻击性为 0 都是合法状态，
    被当成缺失值会让势场基线突然跳回中性。
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    # 必须挡 NaN 和 ±inf：`n != n` 只能识别 NaN，"1e999" 会变成 inf 一路流进 PAD。
    return num if math.isfinite(num) else default


def base_from_field(A: float, L: float, Ag: float, T: float,
                    strength: float = 1.0) -> dict:
    """由势场（好感/力比多/攻击性/信任）算 PAD 基线。

    直觉校验：
    - 越喜欢他 → 基线越愉快（P↑）
    - 对他越"热" → 越活泛（Ar↑）
    - 攻击性高 → 底气足（D↑）但不愉快（P↓）
    - 越信任 → 越稳（D↑）

    strength 是耦合强度（0 = 完全不用势场，等于写死基线）。
    """
    k = max(0.0, min(1.0, num_or(strength, 1.0)))
    a = clamp100(num_or(A, 50.0)) - 50.0
    l = clamp100(num_or(L, 50.0)) - 50.0
    ag = clamp100(num_or(Ag, 50.0)) - 50.0
    t = clamp100(num_or(T, 50.0)) - 50.0
    base_P = 50.0 + k * (0.30 * a + 0.15 * l - 0.20 * ag)
    base_Ar = 40.0 + k * (0.25 * l + 0.15 * ag)
    base_D = 45.0 + k * (0.25 * t + 0.20 * ag)
    return {"P": round(clamp100(base_P), 1),
            "Ar": round(clamp100(base_Ar), 1),
            "D": round(clamp100(base_D), 1)}


@dataclass
class Mood:
    """两层情绪：她自己的心情（core）+ 当前场合的残留（scene）。

    为什么不共用一份全局状态：A 群被骂了，回 B 群还是那副臭脸，
    这是把「场合反应」当成了「人格」。
    为什么不做成完全独立：群里被夸之后回私聊一点余温都没有，同样不像人。

    所以冲量同时打到两层，core 吃得少、退得慢，scene 吃满、退得快；
    表现值 = core * CORE_W + (基线 + scene偏移) * SCENE_W。

    两个实现要点（均来自外部评审）：
    1. scene 存**相对基线的偏移**，新场景从 0 开始。若复制当前 core，
       等于把全局心情又带进每个新场景，耦合会翻倍。
    2. core 的累计冲量要设限：一个活跃群连刷几十条负面，即使 core 只吃 40%，
       也可能长期压过其他场景。故有重复折扣 + 窗口内单向累计上限。
    """

    # 冲量分配：不归一化。当前场合全额吃，core 只吃一部分。
    CORE_SHARE: float = 0.40
    SCENE_SHARE: float = 1.00

    # 表现值混合。这个比例下跨场景变化约占本场景的 18%
    # （w*0.4 / (w*0.4 + 1-w)）；core 权重越高串得越厉害，0.65 会到 43%。
    CORE_W: float = 0.35
    SCENE_W: float = 0.65

    # 半衰期（分钟）
    CORE_HALF_LIFE: float = 120.0
    SCENE_HALF_LIFE: float = 30.0

    # core 防刷
    CORE_WINDOW: float = 600.0      # 秒，累计冲量统计窗口
    CORE_CAP: float = 12.0          # 窗口内单维同向累计上限
    CORE_DUP_WINDOW: float = 120.0  # 秒，重复事件判定窗口
    CORE_DUP_DECAY: float = 0.5     # 每重复一次，core 份额乘这个
    CORE_OFFSET_LIMIT: float = 60.0  # 内部偏移边界；绝对表现值只在输出时夹到 0~100

    core: Affect = field(default_factory=Affect)
    scenes: dict = field(default_factory=dict)   # umo -> {"P": 偏移, "Ar":, "D":, "ts":}

    _core_ts: float = field(default_factory=time.time)
    _last_decay: float = field(default_factory=time.time)
    _core_hist: list = field(default_factory=list)  # [CoreEvent]

    def __post_init__(self) -> None:
        """Mood 的构造入口同样要能接住脏数据（GPT 第 7/8 轮）。

        防御边界说明：这里只清洗**可能来自外部**的东西——state.json 反序列化、
        配置、以及它们经 load_state 进来的容器。代码内部自己 new 的 Mood 不需要防，
        但容器是同一个对象，成本极低就不区分了。类常量（CORE_SHARE 等）不校验：
        只有改代码才会传错，测试期就该暴露，运行时兜底反而掩盖 bug。
        """
        if not isinstance(self.core, Affect):
            self.core = Affect()
        for name in DIMS:
            self._set_core_delta(name, getattr(self.core, name) - self.core.base[name])
        if not isinstance(self.scenes, dict):
            self.scenes = {}
        # 场景是 {umo: {P,Ar,D,ts}}；非 dict 的条目直接丢，维度过 num_or。
        clean_scenes = {}
        for k, v in self.scenes.items():
            if not isinstance(v, dict):
                continue
            clean_scenes[str(k)] = {
                **{n: max(-60.0, min(60.0, num_or(v.get(n), 0.0))) for n in DIMS},
                "ts": num_or(v.get("ts"), time.time()),
            }
        self.scenes = clean_scenes
        self._core_ts = min(num_or(self._core_ts, time.time()), time.time())
        self._last_decay = min(num_or(self._last_decay, time.time()), time.time())
        self._restore_history(self._core_hist)

    # ---------- 内部 ----------

    def _set_core_delta(self, name: str, delta: float) -> None:
        """core 的唯一内部边界是相对基线 ±60，不能夹绝对值丢掉偏移。"""
        delta = max(-self.CORE_OFFSET_LIMIT,
                    min(self.CORE_OFFSET_LIMIT, num_or(delta, 0.0)))
        setattr(self.core, name, self.core.base[name] + delta)

    def _restore_history(self, raw) -> None:
        """恢复仍在窗口内的有效额度；拒绝未来/非法事件并统一时间顺序。"""
        now = time.time()
        hist = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, CoreEvent):
                item = asdict(item)
            if not isinstance(item, dict):
                continue
            ts = num_or(item.get("ts"), None)
            if ts is None or not 0 <= now - ts < self.CORE_WINDOW:
                continue
            granted_raw = item.get("granted")
            if not isinstance(granted_raw, dict):
                continue
            granted = {k: num_or(v, 0.0) for k, v in granted_raw.items() if k in DIMS}
            if not any(granted.values()):
                continue
            actual_raw = item.get("actual")
            actual = ({k: num_or(v, 0.0) for k, v in actual_raw.items() if k in DIMS}
                      if isinstance(actual_raw, dict) else {})
            sign = num_or(item.get("sign"), None)
            if sign not in (-1, 1):
                sign = 1 if sum(granted.values()) >= 0 else -1
            hist.append(CoreEvent(ts, str(item.get("umo") or ""), int(sign), granted, actual))
        self._core_hist = sorted(hist, key=lambda ev: ev.ts)

    def _prune_history(self) -> None:
        now = time.time()
        self._core_hist = [ev for ev in self._core_hist
                           if 0 <= now - ev.ts < self.CORE_WINDOW]

    def _scene(self, umo: str) -> dict:
        """取（必要时创建）某会话的偏移表。新场景从 0 开始，不继承 core。"""
        key = str(umo or "")
        sc = self.scenes.get(key)
        if sc is None:
            sc = {"P": 0.0, "Ar": 0.0, "D": 0.0, "ts": time.time()}
            self.scenes[key] = sc
        return sc

    @staticmethod
    def _halve(a: Affect, minutes: float, half_life: float) -> None:
        """按半衰期把偏差衰减掉：过了 1 个半衰，偏差只剩一半。"""
        if minutes <= 0 or half_life <= 0:
            return
        keep = 0.5 ** (minutes / half_life)
        for name in DIMS:
            cur = getattr(a, name)
            # 不在内部状态上舍入：高频调用时「每次舍掉一点、时间却照扣」会让衰减停止。
            # 精度只在输出边界（effective / as_dict / 日志）处理。
            setattr(a, name, a.base[name] + (cur - a.base[name]) * keep)
        a.ts = time.time()

    def _core_share_for(self, umo: str, sign: int) -> float:
        """同一会话短时间内重复同向事件，进 core 的份额递减。"""
        now = time.time()
        dup = 0
        for ev in reversed(self._core_hist):
            if (0 <= now - ev.ts <= self.CORE_DUP_WINDOW
                    and ev.umo == umo and ev.sign == sign):
                dup += 1
        return self.CORE_SHARE * (self.CORE_DUP_DECAY ** min(dup, 4))

    def _core_quota(self, want: dict) -> dict:
        """窗口内 core 的单向累计冲量有上限，防止连刷把人钉死。"""
        self._prune_history()
        # 正负分开记：混在一起算净额时，负数方向的「历史越界」
        # 会在事件过期后变成下一次负冲量的「补偿」，把负冲量反转成正的
        # （复核复现：prev=-24 时再来 -4，净额算法给出 +12，core 反而升高）。
        used_pos: dict = {}
        used_neg: dict = {}
        for ev in self._core_hist:
            for k, v in ev.granted.items():
                if v > 0:
                    used_pos[k] = used_pos.get(k, 0.0) + v
                elif v < 0:
                    used_neg[k] = used_neg.get(k, 0.0) - v
        out = {}
        for k, v in want.items():
            if v >= 0:
                remaining = max(0.0, self.CORE_CAP - used_pos.get(k, 0.0))
                out[k] = min(v, remaining)
            else:
                remaining = max(0.0, self.CORE_CAP - used_neg.get(k, 0.0))
                out[k] = -min(-v, remaining)
            # 即使旧历史已超新额度，也只能拒绝本次输入，不能制造反向补偿。
        return out

    # ---------- 对外 ----------

    def rebase(self, new_base: dict) -> None:
        """把 core 的基线换成新的，同时保持它与基线的偏移不变。

        基线现在由势场（跟这个人的关系）决定，换个人说话基线就不同；
        若直接改 base 而不动绝对值，core 会被"拽"一截，
        等于每次切会话心情都跳一下。
        """
        if not isinstance(new_base, dict):
            return
        old = dict(self.core.base)
        for name in DIMS:
            if name not in new_base:
                continue
            # 先验数值再截断：clamp100 遇到 NaN 的行为不可依赖（min/max 的比较会穿透）。
            nb = clamp100(num_or(new_base[name], num_or(old.get(name), 50.0)))
            delta = num_or(getattr(self.core, name), nb) - num_or(old.get(name), nb)
            self.core.base[name] = nb
            self._set_core_delta(name, delta)

    def decay(self, minutes_per_step: float = 5.0, ratio_per_step: float = 0.2) -> None:
        """两层都回归，core 慢、scene 快。参数保留是为兼容旧配置。"""
        now = time.time()
        elapsed_core = max(0.0, (now - self._core_ts) / 60.0)
        elapsed_scene = max(0.0, (now - self._last_decay) / 60.0)
        if elapsed_core > 0:
            self._halve(self.core, elapsed_core, self.CORE_HALF_LIFE)
            self._core_ts = now
        if elapsed_scene > 0:
            keep = 0.5 ** (elapsed_scene / self.SCENE_HALF_LIFE)
            for sc in self.scenes.values():
                for name in DIMS:
                    sc[name] = num_or(sc.get(name, 0.0), 0.0) * keep
            self._last_decay = now

    def apply(self, effects: dict, umo: str = "", clamp: float = 20.0) -> None:
        """一次情绪事件，同时打到 core 和当前场合。"""
        if not isinstance(effects, dict):
            return
        cap = max(0.0, num_or(clamp, 20.0))
        if cap == 0.0:
            return
        # 先把输入收敛成「只含 P/Ar/D 的干净字典」：判零、算总额、写历史、算场景，
        # 全都用同一份数据。bool / NaN / ±inf 也一律拒绝——NaN 满足 `!= 0.0`，
        # 会一路流进求和与 PAD（外部评审指出）。
        valid: dict[str, float] = {}
        for k, v in effects.items():
            if k not in DIMS or isinstance(v, bool):
                continue
            try:
                num = float(v)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(num):
                valid[k] = num
        dropped = len([k for k in effects if k in DIMS]) - len(valid)
        if dropped > 0:
            # 只在出错时记一条，且不打印整段模型输出；限频交给调用方（每轮最多一次）。
            logger.debug(f"[mind] 情绪冲量丢弃 {dropped} 个非法维度: keys={list(effects)}")
        # 全零事件必须直接丢弃：否则 sum()>=0 会把它记成 sign=+1 的一次重复事件，
        # 让此后同会话同符号的真实情绪被重复折扣吃掉。
        if not any(v != 0.0 for v in valid.values()):
            return
        # 新冲量不能被当作发生在上次衰减时刻；等待模型期间经过的时间先结算。
        self.decay()
        umo = str(umo or "")
        sign = 1 if sum(valid.values()) >= 0 else -1
        share = self._core_share_for(umo, sign)
        core_eff = self._core_quota({k: max(-cap, min(cap, v * share))
                                    for k, v in valid.items()})
        before = {k: getattr(self.core, k) for k in DIMS}
        for k, delta in core_eff.items():
            self._set_core_delta(k, before[k] - self.core.base[k] + delta)
        self.core.ts = time.time()
        actual = {k: getattr(self.core, k) - before[k] for k in DIMS}

        sc = self._scene(umo)
        for k, v in valid.items():
            d = max(-cap, min(cap, v * self.SCENE_SHARE))
            sc[k] = max(-60.0, min(60.0, sc.get(k, 0.0) + d))
        sc["ts"] = time.time()

        # 配额后全零就不算「有效事件」：否则一次被额度吃光的冲量
        # 也会参与后续重复折扣，把真实情绪也一起打折。
        if any(v != 0.0 for v in core_eff.values()):
            self._core_hist.append(CoreEvent(time.time(), str(umo or ""), sign, dict(core_eff), actual))
        # 仅按时间淘汰；丢掉窗口内的有效事件会提前释放额度。

    def offset(self, umo: str = "") -> dict:
        sc = self.scenes.get(str(umo or ""))
        return {k: round(float((sc or {}).get(k, 0.0)), 1) for k in DIMS}

    def effective(self, umo: str = "") -> dict:
        """她此刻对外表现的情绪值。查询不创建场景。"""
        sc = self.scenes.get(str(umo or "")) or {}
        out = {}
        for name in DIMS:
            base_v = num_or(self.core.base.get(name), 50.0)
            scene_abs = base_v + num_or(sc.get(name), 0.0)
            # 混合结果必须再夹一次：core=0、基线=0、scene=-60 时会算出负数。
            out[name] = round(clamp100(
                getattr(self.core, name) * self.CORE_W + scene_abs * self.SCENE_W
            ), 1)
        return out

    def as_dict(self, umo: str = "") -> dict:
        return self.effective(umo)

    def snapshot(self) -> dict:
        return {
            "core": self.core.as_dict(),
            "offset": self.offset(""),
            "scenes": len(self.scenes),
            "half_life": {"core": self.CORE_HALF_LIFE, "scene": self.SCENE_HALF_LIFE},
            "share": {"core": self.CORE_SHARE, "scene": self.SCENE_SHARE},
            "weight": {"core": self.CORE_W, "scene": self.SCENE_W},
        }

    def dump_state(self) -> dict:
        # 存偏移 + 参照基线，而不是绝对值：
        # 基线由「她对这个人」的态度决定，重启后先恢复基线，再把偏移贴回去，
        # 否则别人的态度会被当成她自己的残留心情（复核复现：69 -> 78）。
        self._prune_history()
        return {
            "schema_version": 2,
            "core": {**{name: getattr(self.core, name) for name in DIMS}, "ts": self._core_ts},
            "core_base": dict(self.core.base),
            "core_delta": {name: getattr(self.core, name) - self.core.base[name] for name in DIMS},
            "core_hist": [asdict(ev) for ev in self._core_hist],
            "scenes": {k: dict(v) for k, v in list(self.scenes.items())[-200:]},
            "_last_decay": self._last_decay,
        }

    def load_state(self, data: dict) -> None:
        """兼容两种输入：新格式（scenes 存偏移）与旧的单层 affect。"""
        if not isinstance(data, dict):
            return
        core = data.get("core")
        if not isinstance(core, dict):
            core = {}
        # 基线先恢复，再按偏移还原绝对值。旧状态若未存基线，只能使用调用方
        # 预设的配置基线；已丢失的关系基线无法从旧绝对值中反推。
        base_raw = data.get("core_base")
        if not isinstance(base_raw, dict):
            base_raw = core.get("base")
        if isinstance(base_raw, dict):
            for name in DIMS:
                if name in base_raw:
                    self.core.base[name] = clamp100(num_or(base_raw[name], self.core.base[name]))
        delta_raw = data.get("core_delta")
        for name in DIMS:
            delta = (num_or(delta_raw.get(name), None)
                     if isinstance(delta_raw, dict) else None)
            if delta is None:
                # 原版 core 是 0~100 的绝对值；新版带偏移的格式以偏移为准。
                absolute = num_or(core.get(name), self.core.base[name])
                delta = absolute - self.core.base[name]
            self._set_core_delta(name, delta)
        self._restore_history(data.get("core_hist"))
        if "ts" in core:
            # 时间戳同样要过 num_or：NaN/inf/未来时间会让衰减永久停摆或瞬间归零。
            ts = num_or(core["ts"], self._core_ts)
            self._core_ts = min(ts, time.time())
        self.core.ts = self._core_ts
        self.scenes = {}
        scenes_raw = data.get("scenes")
        if not isinstance(scenes_raw, dict):
            scenes_raw = {}
        for k, v in scenes_raw.items():
            if not isinstance(v, dict):
                continue
            sc = {"P": 0.0, "Ar": 0.0, "D": 0.0, "ts": time.time()}
            for name in DIMS:
                sc[name] = max(-60.0, min(60.0, num_or(v.get(name), 0.0)))
            sc["ts"] = num_or(v.get("ts"), time.time())
            self.scenes[str(k)] = sc
        if "_last_decay" in data:
            self._last_decay = min(num_or(data["_last_decay"], self._last_decay), time.time())
