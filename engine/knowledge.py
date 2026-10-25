"""本地知识库：零依赖检索（汉字二元组 + IDF 加权）。

为什么不用向量库：
- 发布版要让别人**装完就能用**。向量检索要配 embedding（额外 API/额度），
  很多人卡在这一步就放弃了。
- 小语料（几千到几万字的角色设定/术语/梗）用字二元组 + IDF 已经够用，
  而且完全离线、零成本。
- 真需要向量时，可以再把 query() 换掉，接口保持不变。

日记（kb/diary/*.md）走同一条管道，但多两件事：
- frontmatter 里的 `tags` 参与打分（命中一个 +0.8，权重高到能压过正文）
- 日记条目的基础分乘一个系数（默认 1.15）——她自己写过的东西，
  比百科式的知识点更容易被“想起来”。
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

TOKEN_RE = re.compile(r"[a-zA-Z0-9]{2,}")
FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
TAG_SPLIT_RE = re.compile(r"[,，、;；]")

# 检索参数
TAG_HIT_BONUS = 0.8      # 命中一个 tag 的加分
DIARY_PREFER = 1.15      # 日记条目的偏好系数


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


def parse_front_matter(text: str) -> tuple[dict, str]:
    """极简 frontmatter：只认 `key: value` 与 `tags: a, b, c`，不引 yaml 依赖。"""
    m = FM_RE.match(text or "")
    if not m:
        return {}, text or ""
    meta: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        if key in ("tags", "标签"):
            meta["tags"] = [t.strip() for t in TAG_SPLIT_RE.split(val) if t.strip()]
        elif key in ("mood", "心情"):
            try:
                meta["mood"] = float(val)
            except ValueError:
                meta["mood_text"] = val
        else:
            meta[key] = val
    return meta, (text or "")[m.end():]


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
        # 子目录（diary/ 等）也收
        for sub in sorted(p for p in self.dir.iterdir() if p.is_dir()):
            files += sorted(sub.glob("*.md")) + sorted(sub.glob("*.txt"))
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
            meta, body = parse_front_matter(text)
            is_diary = fp.parent.name == "diary" or meta.get("kind") == "diary"
            base = {
                "source": fp.stem,
                "tags": list(meta.get("tags") or []),
                "kind": "diary" if is_diary else "kb",
                "date": str(meta.get("date") or ""),
                "title": str(meta.get("title") or ""),
                "mood": meta.get("mood"),
            }
            for piece in chunk_text(body):
                chunks.append((dict(base), piece))
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

    def _score(self, qt: set[tuple], text: str, meta: dict) -> float:
        common = qt & tokens(text)
        if not common:
            base = 0.0
        else:
            base = sum(1.0 / max(1, self.df.get(t, 1)) for t in common) / (len(text) ** 0.3)
        # 标签命中：标签是短词，用「原文包含」判断，比二元组更准
        hits = 0
        for tag in meta.get("tags") or []:
            tag = str(tag).strip()
            if len(tag) >= 2 and tag in text:
                hits += 1
        if hits:
            base += TAG_HIT_BONUS * hits
        if base and meta.get("kind") == "diary":
            base *= DIARY_PREFER
        return base

    def query(self, text: str, k: int = 3, min_score: float = 0.12) -> list[str]:
        if not self.chunks:
            return []
        qt = tokens(text)
        if len(qt) < 2:
            return []
        scored = []
        for meta, piece in self.chunks:
            s = self._score(qt, piece, meta)
            if s >= min_score:
                scored.append((s, meta, piece))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [piece for _s, _m, piece in scored[:k]]

    def query_diary(self, text: str, k: int = 2, min_score: float = 0.10) -> list[dict]:
        """只在她自己的日记里找，返回带日期的结构（用于“你以前写过…”）。"""
        if not self.chunks:
            return []
        qt = tokens(text)
        scored = []
        for meta, piece in self.chunks:
            if meta.get("kind") != "diary":
                continue
            # 日记检索放宽：query 太短时也允许 tag 命中
            if len(qt) >= 2:
                s = self._score(qt, piece, meta)
            else:
                s = sum(TAG_HIT_BONUS for t in (meta.get("tags") or []) if len(str(t)) >= 2 and str(t) in text)
            if s >= min_score:
                scored.append((s, meta, piece))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"date": meta.get("date") or "", "title": meta.get("title") or "", "text": piece}
            for _s, meta, piece in scored[:k]
        ]

    def mood_diary(self, want_happy: bool = True, k: int = 1) -> list[dict]:
        """按心情检索旧日记：低落时想起开心的，开心时想起难过的（对比感）。"""
        out = []
        for meta, piece in self.chunks:
            if meta.get("kind") != "diary":
                continue
            mood = meta.get("mood")
            if not isinstance(mood, (int, float)):
                continue
            if want_happy and mood >= 62:
                out.append((mood, meta, piece))
            elif not want_happy and mood <= 38:
                out.append((-mood, meta, piece))
        out.sort(key=lambda x: x[0], reverse=True)
        return [
            {"date": meta.get("date") or "", "title": meta.get("title") or "", "text": piece}
            for _m, meta, piece in out[:k]
        ]

    # ---------- 写入（给运行时追加内容用，比如日记同步） ----------

    def add_file(self, path: str) -> None:
        if path and path not in self.extra_files:
            self.extra_files.append(path)
            self.load()
