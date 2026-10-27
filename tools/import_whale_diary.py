"""把小鲸鱼（DSH 侧，dsh-world 世界「小鲸鱼」）的日记，导成共享判断库里的 claims。

只导「判断」，不导「经历」：
- 表格里的「想法」列是她的判断 → 进 claims，surface=diary, experience=heard
- 日记正文不进（那是她的场景资产，不该在 QQ 里被复述）

用法：
    python tools/import_whale_diary.py            # 导入默认路径（世界日记目录）
    python tools/import_whale_diary.py --diary <目录或文件> --dry-run

默认读 ~/.dsh/worlds/default/diary/ 下所有 YYYY-MM-DD.md；
老的单文件日记（~/.dsh/whale-diary.md）也能直接喂进来。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.claims import ClaimStore  # noqa: E402

DEFAULT_DIARY = Path.home() / ".dsh" / "worlds" / "default" / "diary"
_DATA = ROOT.parents[1] / "plugin_data" / "astrbot_plugin_persona_mind"
DEFAULT_CLAIMS = _DATA / "shared" / "claims.jsonl"

# | 时间 | BV | 标题 | 想法 |
ROW = re.compile(r"^\|\s*[^|]*\|\s*(BV[0-9A-Za-z]+)?\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")


def parse_rows(text: str, date_hint: str = "") -> list[dict]:
    """从日记的「看过」表格里取 标题 + 想法。"""
    out: list[dict] = []
    date = date_hint
    for line in text.splitlines():
        m = re.match(r"^#{1,2}\s*(\d{4}-\d{2}-\d{2})", line.strip())
        if m:
            date = m.group(1)
            continue
        if not line.strip().startswith("|"):
            continue
        rm = ROW.match(line.strip())
        if not rm:
            continue
        title = (rm.group(2) or "").strip()
        stance = (rm.group(3) or "").strip()
        if not title or title in ("标题", "---", ":---:") or set(title) <= {"-", ":"}:
            continue
        if not stance or stance in ("想法", "看过画面之后", "---"):
            continue
        # 「→ 收了」这类标记保留语义，但去掉箭头符号
        stance = stance.replace("→", "，").replace("  ", " ")
        stance = re.sub(r"\s+([，。！？])", r"\1", stance).strip("， ")
        if len(stance) < 4:
            continue
        out.append({"date": date, "bv": (rm.group(1) or ""),
                    "title": title[:80], "stance": stance[:300]})
    # 同一件事写过多次时，后写的覆盖先写的（判断会更新：先「看完就完」，后「服了」）
    latest: dict[str, dict] = {}
    for r in out:
        latest[r["title"]] = r
    return list(latest.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diary", default=str(DEFAULT_DIARY))
    ap.add_argument("--claims", default=str(DEFAULT_CLAIMS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.diary)
    if src.is_dir():
        files = sorted(src.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))
    elif src.is_file():
        files = [src]
    else:
        print(f"[import] 找不到日记：{src}")
        return 1
    if not files:
        print(f"[import] 目录里没有 YYYY-MM-DD.md：{src}")
        return 0
    rows: list[dict] = []
    for f in files:
        hint = f.stem if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem) else ""
        rows.extend(parse_rows(f.read_text(encoding="utf-8", errors="ignore"), hint))
        print(f"[import] 读 {f.name}")
    if not rows:
        print("[import] 没解析出任何一行")
        return 0

    store = ClaimStore(args.claims, current_surface="qq")
    store.load()
    known = {(r.get("title"), r.get("stance")) for r in store._items}  # noqa: SLF001
    added = 0
    for r in rows:
        key = (r["title"], r["stance"])
        if key in known:
            continue
        if args.dry_run:
            print(f"  待导入 [{r['date']}] {r['title']} —— {r['stance']}")
            added += 1
            continue
        ok = store.add(
            r["title"],
            r["stance"],
            surface="diary",
            experience="heard",
            kind="video" if "BV" in r.get("bv", "") else "topic",
            confidence=0.8,
            source="小鲸鱼",
            date=r["date"],
            source_scene="diary",
            canonical_id="deepseek_xiaojingyu",
            visibility="shared",
            material_id=(f"whale:{r['bv']}" if r.get("bv") else ""),
        )
        if ok:
            added += 1

    print(f"[import] 解析 {len(rows)} 行，新增 {added} 条，库内共 {store.stats()['claims']} 条")
    if args.dry_run:
        print("[import] dry-run，未写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
