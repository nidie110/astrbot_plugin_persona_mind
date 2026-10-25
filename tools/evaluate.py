"""统一评估：一次输出四种口径，避免报告里混用。

口径定义（写死在这里，报告只引用本脚本的输出）：
- strict : 首选 == 主标签
- top2   : 主标签出现在前二
- accept : 首选落在可接受集合
- accept2: 前二中任一落在可接受集合

调参集没有定义可接受集合（calibrate.py 只给主标签），所以 accept 列对它无定义；
留出集/开发集有显式 accept 集合。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from engine.act import decide            # noqa: E402
from tools.calibrate import CASES        # noqa: E402
from tools.holdout import HOLDOUT        # noqa: E402

PERSONA = {"O": 75, "C": 35, "E": 55, "A": 40, "N": 60}
AFFECT = {"P": 60.0, "Ar": 45.0, "D": 50.0}
REL_PRIVATE = {"I": 55, "T": 50}
REL_GROUP = {"I": 60, "T": 70}


def _score(msg, scene, rel, want, accept, verbose, rows):
    d = decide(
        persona=PERSONA, affect=AFFECT, relation=rel, scene=scene, msg=msg,
        mentioned=(scene == "group" and ("@" in msg or "大肥鱼" in msg)),
    )
    names = [n for n, _ in d["lead"]]
    strict = bool(names) and names[0] == want
    top2 = want in names
    acc = names[0] in accept if accept else None
    acc2 = any(n in accept for n in names) if accept else None
    if verbose:
        rows.append((msg, want, names[0] if names else "-", strict))
    return strict, top2, acc, acc2


def evaluate(cases, scene, rel, has_accept, verbose=False):
    rows = []
    s = t = a = a2 = 0
    for item in cases:
        if has_accept:
            msg, want, accept = item
        else:
            msg, want = item[0], item[1]
            accept = None
        st, tp, ac, ac2 = _score(msg, scene, rel, want, accept, verbose, rows)
        s += st
        t += tp
        a += bool(ac)
        a2 += bool(ac2)
    n = len(cases)
    return {
        "n": n, "strict": s / n, "top2": t / n,
        "accept": (a / n) if has_accept else None,
        "accept2": (a2 / n) if has_accept else None,
        "rows": rows,
    }


def main():
    calib = [(m, w) for m, w, _n in CASES]
    table = []
    table.append(("调参 私聊", evaluate(calib, "private", REL_PRIVATE, False, verbose=True)))
    table.append(("调参 群聊", evaluate(calib, "group", REL_GROUP, False, verbose=True)))
    table.append(("开发 私聊", evaluate(HOLDOUT, "private", REL_PRIVATE, True, verbose=True)))
    table.append(("开发 群聊", evaluate(HOLDOUT, "group", REL_GROUP, True, verbose=True)))

    print(f"{'集合':<12}{'条数':>4}{'strict':>9}{'top2':>9}{'accept':>9}{'accept2':>9}")
    for name, r in table:
        acc = f"{r['accept']:.1%}" if r["accept"] is not None else "—"
        acc2 = f"{r['accept2']:.1%}" if r["accept2"] is not None else "—"
        print(f"{name:<12}{r['n']:>4}{r['strict']:>8.1%}{r['top2']:>8.1%}{acc:>9}{acc2:>9}")

    # 明细：只列表失样本
    print("\n=== 严格未命中 ===")
    for name, r in table:
        for msg, want, got, ok in r["rows"]:
            if not ok:
                print(f"[{name}] {msg[:22]:<24} 期望 {want:<7} 判成 {got}")
    return table


if __name__ == "__main__":
    main()
