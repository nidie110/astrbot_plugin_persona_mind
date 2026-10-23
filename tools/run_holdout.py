import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from engine.act import decide
from tools.holdout import HOLDOUT
from tools.run_calib import PERSONA, AFFECT, REL_PRIVATE, REL_GROUP


def run(scene, rel, verbose=False):
    ok1 = ok2 = 0
    for msg, want, accept in HOLDOUT:
        d = decide(persona=PERSONA, affect=AFFECT, relation=rel, scene=scene, msg=msg)
        names = [n for n, _ in d["lead"]]
        good1 = bool(names) and names[0] in accept
        good2 = bool(accept & set(names))
        ok1 += good1; ok2 += good2
        if verbose and not good1:
            print(f"  ✗ {msg[:20]:<22} 期望 {want:<7} 判成 {names[0]:<7} {round(d['lead'][0][1],2)}")
    return ok1 / len(HOLDOUT), ok2 / len(HOLDOUT)


if __name__ == "__main__":
    print("== 私聊留出集 ==")
    a1, a2 = run("private", REL_PRIVATE, verbose=True)
    print(f"top1={a1:.0%} top2={a2:.0%}")
    print("== 群聊留出集 ==")
    b1, b2 = run("group", REL_GROUP, verbose=True)
    print(f"top1={b1:.0%} top2={b2:.0%}")
