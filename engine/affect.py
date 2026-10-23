"""PAD 情绪状态：三个连续值 + 衰减。

理论出处：Mehrabian & Russell, *An Approach to Environmental Psychology* (1974)。
Pleasure–Arousal–Dominance 三维能描述绝大多数情绪状态，
比"生气/开心"这种离散标签更适合连续变化的对话场景。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


@dataclass
class Affect:
    """她此刻的情绪状态（0~100）。"""

    P: float = 60.0   # pleasure 愉悦
    Ar: float = 45.0  # arousal 唤醒
    D: float = 50.0   # dominance 支配/底气

    # 基线（衰减目标）
    base: dict = field(default_factory=lambda: {"P": 60.0, "Ar": 45.0, "D": 50.0})
    ts: float = field(default_factory=time.time)

    def decay(self, minutes_per_step: float = 5.0, ratio_per_step: float = 0.2) -> None:
        """向基线回归。生气 5~10 分钟就淡了，不会记一天。"""
        elapsed_min = max(0.0, (time.time() - self.ts) / 60.0)
        steps = elapsed_min / max(0.1, minutes_per_step)
        if steps <= 0:
            return
        k = min(1.0, steps * max(0.0, ratio_per_step))
        for name in ("P", "Ar", "D"):
            cur = getattr(self, name)
            setattr(self, name, round(cur + (self.base[name] - cur) * k, 1))
        self.ts = time.time()

    def apply(self, effects: dict, clamp: float = 20.0) -> None:
        """叠加一次情绪事件；单次变化幅度做上限，避免一条消息把人炸上天。"""
        for name, delta in effects.items():
            if name not in ("P", "Ar", "D"):
                continue
            d = max(-clamp, min(clamp, float(delta)))
            setattr(self, name, max(0.0, min(100.0, getattr(self, name) + d)))
        self.ts = time.time()

    def as_dict(self) -> dict:
        return {"P": round(self.P, 1), "Ar": round(self.Ar, 1), "D": round(self.D, 1)}

    def load(self, data: dict) -> None:
        for name in ("P", "Ar", "D"):
            if name in data:
                try:
                    setattr(self, name, float(data[name]))
                except (TypeError, ValueError):
                    pass
        if "ts" in data:
            try:
                self.ts = float(data["ts"])
            except (TypeError, ValueError):
                pass
        if isinstance(data.get("base"), dict):
            self.base.update({k: float(v) for k, v in data["base"].items() if k in ("P", "Ar", "D")})

    def snapshot(self) -> dict:
        return {**asdict(self)}


def normalize01(v: float) -> float:
    return max(0.0, min(1.0, float(v) / 100.0))
