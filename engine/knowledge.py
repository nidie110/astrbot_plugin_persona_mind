"""本地知识库：零依赖检索（汉字二元组 + IDF 加权）。

为什么不用向量库：
- 发布版要让别人**装完就能用**。向量检索要配 embedding（额外 API/额度），
  很多人卡在这一步就放弃了。
- 小语料（几千到几万字的角色设定/术语/梗）用字二元组 + IDF 已经够用，
  而且完全离线、零成本。
- 真需要向量时，可以再把 query() 换掉，接口保持不变。
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

TOKEN_RE = re.compile(r"[a-zA-Z0-9]{2,}")


def tokens(text: str) -> set[tuple]:
    text = (text or "").lower()
    out: set[tuple] = set()
    han = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(han) - 1):
        out.add(("zh", han[i] + han[i + 1]))
    for w in TOKEN_RE.findall(text):
        out.add(("en", w))
    return out


def chunk_text(text: str, size: int = 350) -> list[str]:
    chunks = []
    for part in re.split(r"\n(?=#+ )", text):
        part = part.strip()
        if len(part) < 20:
            continue
        for i in range(0, len(part), size):
            piece = part[i : i + size].strip()
            if len(piece) >= 20:
                chunks.append(piece)
    return chunks


class KnowledgeBase:
    def __init__(self, directory: Path, extra_files: list[str] | None = None):
        self.dir = Path(directory)
        self.extra_files = [f for f in (extra_files or []) if f]
        self.chunks: list[tuple[dict, str]] = []  # (meta, piece)
        self.df: dict[tuple, int] = {}
        self._mtime_sig = 0.0
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---------- 加载 ----------

    def _collect_files(self) -> list[Path]:
        files = sorted(self.dir.glob("*.md")) + sorted(self.dir.glob("*.txt"))
        for fp in self.extra_files:
            p = Path(fp)
            if p.is_file():
                files.append(p)
        return files

    def load(self) -> int:
        chunks: list[tuple[dict, str]] = []
        sig = 0.0
        for fp in self._collect_files():
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
                sig += fp.stat().st_mtime
            except Exception:
                continue
            for piece in chunk_text(text):
                chunks.append(({"source": fp.stem}, piece))
        df: dict[tuple, int] = {}
        for _meta, piece in chunks:
            for t in tokens(piece):
                df[t] = df.get(t, 0) + 1
        self.chunks, self.df, self._mtime_sig = chunks, df, sig
        return len(chunks)

    def reload_if_changed(self) -> bool:
        sig = 0.0
        for fp in self._collect_files():
            try:
                sig += fp.stat().st_mtime
            except Exception:
                pass
        if sig and sig != self._mtime_sig:
            self.load()
            return True
        return False

    # ---------- 检索 ----------

    def query(self, text: str, k: int = 3, min_score: float = 0.12) -> list[str]:
        if not self.chunks:
            return []
        qt = tokens(text)
        if len(qt) < 2:
            return []
        scored = []
        for meta, piece in self.chunks:
            common = qt & tokens(piece)
            if not common:
                continue
            score = sum(1.0 / max(1, self.df.get(t, 1)) for t in common) / (len(piece) ** 0.3)
            if score >= min_score:
                scored.append((score, meta, piece))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [piece for _s, _m, piece in scored[:k]]

    # ---------- 写入（给运行时追加内容用，比如日记同步） ----------

    def add_file(self, path: str) -> None:
        if path and path not in self.extra_files:
            self.extra_files.append(path)
            self.load()
