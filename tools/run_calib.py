"""跑标定集：算 top1 / top2 准确率，打印混淆情况。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from engine.act import decide
from tools.calibrate import CASES

PERSONA = {"O": 75, "C": 35, "E": 55, "A": 40, "N": 60}
AFFECT = {"P": 60.0, "Ar": 45.0, "D": 50.0}
REL_PRIVATE = {"I": 55, "T": 50}
REL_GROUP = {"I": 60, "T": 70}   # 她实际在的那个群：熟人群


def run(scene="private", rel=None, verbose=False):
    rel = rel or REL_PRIVATE
    top1 = top2 = 0
    rows = []
    for msg, want, note in CASES:
        d = decide(persona=PERSONA, affect=AFFECT, relation=rel, scene=scene,
                   msg=msg, mentioned=(scene == "group" and ("@" in msg or "大肥鱼" in msg)))
        lead = d["lead"]
        names = [n for n, _ in lead]
        ok1 = bool(names) and names[0] == want
        ok2 = want in names
        top1 += ok1
        top2 += ok2
        rows.append((msg, want, names[0] if names else "-", round(lead[0][1], 2) if lead else 0, ok1, note))
    if verbose:
        print(f"{'消息':<28}{'期望':<8}{'判成':<8}{'分':<6}备注")
        for msg, want, got, sc, ok, note in rows:
            mark = " " if ok else "✗"
            print(f"{mark}{msg[:26]:<27}{want:<8}{got:<8}{sc:<6}{note}")
    return top1 / len(CASES), top2 / len(CASES), rows


if __name__ == "__main__":
    a1, a2, _ = run("private", verbose=True)
    print(f"\n私聊 top1={a1:.0%} top2={a2:.0%}")
    b1, b2, _ = run("group", REL_GROUP)
    print(f"群聊 top1={b1:.0%} top2={b2:.0%}")
