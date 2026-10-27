"""小鲸鱼的日记（DSH 世界里那个她写的）→ 大肥鱼的知识。

两件事：
1. 同步：把 `~/.dsh/worlds/default/diary/YYYY-MM-DD.md` 按内容哈希增量同步进
   AstrBot 自带知识库「小鲸鱼日记」，文档名带前缀，避免和她自己的日记撞名。
2. 检索：从库里拿出一两条，判断「这句问话是不是真跟那篇有关」，再交给
   prompts 用一套专门的口吻渲染——她可以提，但不能冒充自己亲历。

这里只放纯函数：不碰网络、不碰 KB 实例，方便单测。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

WHALE_KB = "小鲸鱼日记"
DOC_PREFIX = "小鲸鱼"
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# 世界日记里「想对主人说的话」那一段最值得让她记起
_LETTER = re.compile(r"^#{1,3}\s*(给他的信|给主人的信|写给主人的信|想对你说)\s*$", re.M)
_MD_NOISE = re.compile(r"^(#{1,6}\s*|[-*+]\s+|\d+[.)]\s+|>\s*)")


def doc_name(date: str) -> str:
    return f"{DOC_PREFIX}-{date}.md"


def date_of(name: str) -> str:
    m = _DATE_RE.search(name or "")
    return m.group(1) if m else ""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def scan(directory) -> list[dict]:
    """扫日记目录，返回 [{date, name, text, sha}]，按日期升序。"""
    root = Path(str(directory or "")).expanduser()
    if not root.is_dir():
        return []
    rows: list[dict] = []
    for fp in sorted(root.glob("*.md")):
        date = date_of(fp.name)
        if not date:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text.strip():
            continue
        rows.append(
            {"date": date, "name": doc_name(date), "path": str(fp),
             "text": text, "sha": sha(text)}
        )
    return rows


def _plain(text: str) -> str:
    body = text
    m = _LETTER.search(body)
    if m:  # 有「给他的信」就优先用那段：那是她真正想说的话
        body = body[m.end():]
    lines = []
    for raw in body.splitlines():
        line = _MD_NOISE.sub("", raw.strip()).strip()
        if not line or set(line) <= set("-—=*| "):
            continue
        lines.append(line.replace("|", " ").strip())
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def clip(text: str, limit: int = 80) -> str:
    """截成一小句：优先在句号处断，实在没有就硬切加省略号。"""
    s = _plain(text)
    if len(s) <= limit:
        return s
    window = s[:limit]
    cut = max(window.rfind(ch) for ch in "。！？；…")
    if cut >= max(20, limit // 3):
        return window[: cut + 1]
    return window.rstrip("，、；：,;: ") + "…"


def related(query: str, text: str, min_overlap: int = 2) -> bool:
    """这句问话真跟那篇日记有关吗：同一 BV 直通，否则至少两个词重合。"""
    if not query or not text:
        return False
    q, t = BV_RE.search(query), BV_RE.search(text)
    if q and t and q.group(0) == t.group(0):
        return True
    try:
        from .knowledge import tokens

        overlap = set(tokens(query)) & set(tokens(text))
    except Exception:
        return False
    return len(overlap) >= min_overlap


def day_label(date: str) -> str:
    """10-26 这种短标签：她说话时不该念全日期。"""
    return date[5:] if len(date) == 10 else date
