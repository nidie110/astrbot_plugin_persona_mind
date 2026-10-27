"""把人格卡里「你会用的东西」那一段换成「只写什么时候动手」的新版。

为什么改：1.3.5 把 13 个动词的调用格式逐字写进了人格（41 行 1365 字），
每轮都发、占了人格卡五分之一；现在格式改成按需注入（engine/specs.py + selector.py），
人格里只留「他的话 → 我该动手」的触发线。

用法：
    python tools/patch_persona_trigger.py --show     # 只看现在这一段
    python tools/patch_persona_trigger.py --diff     # 看会改成什么
    python tools/patch_persona_trigger.py --go       # 真的写（先自动备份 db）
"""
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DB = Path("D:/qqaibot/astrbot/data/data_v4.db")
START = "## 你会用的东西（怎么动手、怎么开口）"
END = "## 你会用的东西 · 完"

NEW = """## 你会用的东西（什么时候动手）

听到一句话，先问自己三件事：这是让我做一件具体的事吗？我手上有没有对应的动作？没有就直说「这个我做不了」，再说我能做的那部分。
能做的，就在这一轮做完再开口，不许用「我这就去」填空。说话和做事必须对得上。

他这么说的时候，我该动手：
- 发来链接、问「这讲了啥」「画面里那几个是谁」→ 去看那段画面，用我自己的话讲
- 「你搜搜」「查查看」「这是谁」「什么梗」「多少钱」→ 先搜再答，别凭记忆编
- 「画一只猫」「来张图」→ 画出来
- 「用语音说」「念一下」「听你声音」→ 用我自己的声音念
- 「你能评论吗」「去这条底下回他一句」→ 只有主人开口我才发，写完直接发，把原文和链接告诉他
- 「截图」「这页长什么样」→ 截给他看
- 「你问问她」「问问你妈」「实在不行去问她」→ 指的是同一个人；她当场就回，拿到答案就用它说话，别干等也别编
- 改昵称、改签名、改性别、发说说 → 只有主人开口才动

有些事是按点自己发生的，不是伸手就能做的：自己找乐子（08:30 / 14:00 / 21:00）、写日记（22:40）。他提这些，就说时间点，或者「等主人说」。

做不到时说人话：没这个本事就说做不了；有本事没成，就说「没看成，我这边打不开」。不许出现「通道 / 接口 / 报错 / 插件」这类词，也不要顺口评价这件事值不值得做。

动手之前别念出来，也别解释自己怎么做到的；主人问，就一句「我去看了」「我查了」，然后说结果。

## 你会用的东西 · 完"""


def load() -> tuple[str, str]:
    db = sqlite3.connect(str(DB))
    try:
        name, sp = db.execute("select persona_id, system_prompt from personas where id=2").fetchone()
    finally:
        db.close()
    return name, sp


def patch(sp: str) -> str:
    lines = sp.split("\n")
    try:
        i = next(k for k, l in enumerate(lines) if l.strip() == START)
        j = next(k for k, l in enumerate(lines) if l.strip() == END)
    except StopIteration:
        raise SystemExit(f"找不到标记段（{START} / {END}），什么都没改")
    if j <= i:
        raise SystemExit("标记顺序反了，什么都没改")
    old = "\n".join(lines[i : j + 1])
    return "\n".join(lines[:i] + NEW.split("\n") + lines[j + 1 :]), old


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--strip", action="store_true", help="把这一段从人格里撤掉")
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args()

    name, sp = load()
    if a.strip:
        lines = sp.split("\n")
        try:
            i = next(k for k, l in enumerate(lines) if START in l or l.strip().startswith("## 你会用的东西"))
            j = next(k for k, l in enumerate(lines) if l.strip() == END)
        except StopIteration:
            raise SystemExit("没有找到这一段，什么都没改")
        new = "\n".join(lines[:i] + lines[j + 1 :]).rstrip() + "\n"
        if not a.go:
            print(f"会撤掉 {len(sp) - len(new)} 字（{len(sp)} → {len(new)}），加 --go 才真写")
            return 0
        bak = DB.with_suffix(f".db.bak-strip-{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(DB, bak)
        db = sqlite3.connect(str(DB))
        try:
            db.execute("update personas set system_prompt=? where id=2", (new,))
            db.commit()
        finally:
            db.close()
        print(f"已撤掉；备份 {bak.name}；人格现在 {len(new)} 字")
        return 0

    new, old = patch(sp)
    tag = "" if name.isascii() else "（人格名非 ASCII）"

    if a.show:
        print(old)
        return 0
    print(f"人格 {name!r}{tag}  全文 {len(sp)} 字")
    print(f"这一段 {len(old)} 字 → {len(NEW)} 字，全文将变成 {len(new)} 字")
    if not a.go:
        if a.diff:
            print("\n--- 新的一段 ---")
            print(NEW)
        else:
            print("（这是 dry-run，加 --go 才真写）")
        return 0

    bak = DB.with_suffix(f".db.bak-trigger-{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(DB, bak)
    db = sqlite3.connect(str(DB))
    try:
        db.execute("update personas set system_prompt=? where id=2", (new,))
        db.commit()
        check = db.execute("select length(system_prompt) from personas where id=2").fetchone()[0]
    finally:
        db.close()
    print(f"已写入，库里现在 {check} 字；备份 {bak.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
