# -*- coding: utf-8 -*-
"""回放五个场景，观察局部反应与跨场景残留。

用法：python tools/replay_mood.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.affect import Mood  # noqa: E402

A = "aiocqhttp:GroupMessage:111"
B = "aiocqhttp:GroupMessage:999"
P = "aiocqhttp:FriendMessage:u1"

JOY = {"P": 10, "Ar": 4}
HURT = {"P": -10, "Ar": 6}


def show(tag, m):
    a, b, p = (m.effective(x)["P"] for x in (A, B, P))
    core = m.core.as_dict()["P"]
    print(f"{tag:<22} A群={a:<6.1f} B群={b:<6.1f} 私聊={p:<6.1f} core={core:<6.1f}")


def case_single(kind, effects):
    m = Mood()
    m.apply(effects, umo=A)
    a, b = m.effective(A)["P"], m.effective(B)["P"]
    print(f"\n【{kind}】一次性 P{effects['P']:+d}")
    show("  事件后", m)
    d_local, d_other = a - 60, b - 60
    ratio = abs(d_other / d_local) * 100 if d_local else 0
    print(f"  本场景变化 {d_local:+.1f} / 其他场景变化 {d_other:+.1f} / 跨场景占比 {ratio:.0f}%")


def case_repeat():
    m = Mood()
    print("\n【连续重复辱骂】同一群连发 10 条 P-10")
    for i in range(1, 11):
        m.apply(HURT, umo=A)
        if i in (1, 3, 5, 10):
            show(f"  第 {i} 条后", m)
    print(f"  其他群 {m.effective(B)['P']:.1f}，私聊 {m.effective(P)['P']:.1f}")


def case_switch():
    m = Mood()
    print("\n【切换会话】A 群被骂 3 条后切到 B 群")
    for _ in range(3):
        m.apply(HURT, umo=A)
    show("  A 群挨骂后", m)
    m.apply(JOY, umo=B)
    show("  B 群被夸后", m)
    print("  说明：B 群的局部回弹盖过 core 的下沉，A 群仍在低谷")


def case_decay():
    m = Mood()
    m.apply(HURT, umo=A)
    m.apply(JOY, umo=B)
    before = (m.effective(A)["P"], m.effective(B)["P"], m.core.as_dict()["P"])
    m._core_ts -= 3600
    m._last_decay -= 3600
    m.decay()
    after = (m.effective(A)["P"], m.effective(B)["P"], m.core.as_dict()["P"])
    print("\n【静置一小时】")
    print(f"  衰减前 A={before[0]:.1f} B={before[1]:.1f} core={before[2]:.1f}")
    print(f"  衰减后 A={after[0]:.1f} B={after[1]:.1f} core={after[2]:.1f}")


def main():
    print("=" * 72)
    print(f"core_share={Mood.CORE_SHARE} scene_share={Mood.SCENE_SHARE} "
          f"权重 core={Mood.CORE_W}/scene={Mood.SCENE_W}")
    print(f"半衰 core={Mood.CORE_HALF_LIFE}min scene={Mood.SCENE_HALF_LIFE}min "
          f"上限={Mood.CORE_CAP}@{Mood.CORE_WINDOW}s dup={Mood.CORE_DUP_DECAY}")
    print("=" * 72)
    case_single("单次被夸", JOY)
    case_single("单次被骂", HURT)
    case_repeat()
    case_switch()
    case_decay()


if __name__ == "__main__":
    main()
