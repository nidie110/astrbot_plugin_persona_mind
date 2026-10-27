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

    def _score(self, qt: set[tuple], text: str, meta: dict, query: str = "") -> float:
        common = qt & tokens(text)
        if not common:
            base = 0.0
        else:
            base = sum(1.0 / max(1, self.df.get(t, 1)) for t in common) / (len(text) ** 0.3)
        # 标签命中：必须命中「查询」本身，不能看 chunk 正文——
        # 否则带 tags 的日记在任何查询下都白拿加分，把常识全挤出去。
        hits = 0
        q = query or ""
        for tag in meta.get("tags") or []:
            tag = str(tag).strip()
            if len(tag) >= 2 and tag in q:
                hits += 1
        if hits:
            base += TAG_HIT_BONUS * hits
        if base and meta.get("kind") == "diary":
            base *= DIARY_PREFER
        return base

    def query(self, text: str, k: int = 3, min_score: float = 0.05) -> list[str]:
        if not self.chunks:
            return []
        qt = tokens(text)
        if len(qt) < 2:
            return []
        scored = []
        for meta, piece in self.chunks:
            s = self._score(qt, piece, meta, text)
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
                s = self._score(qt, piece, meta, text)
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


class RemoteKB:
    """AstrBot 自带知识库做后端（FAISS 稠密 + BM25 稀疏融合）。

    自带库的 chunk 只有 {kb_id, kb_doc_id, chunk_index} 三个元数据，
    旧版那两条偏好（tag 命中加分、日记 ×1.15）在这里补回来；标签与日期
    仍从本地 kb/*.md 的 frontmatter 读，只读索引，不写回。

    两个库分开查：常识走 query()，日记走 query_diary()——混在一起时
    日记的 ×1.15 会把常识挤出前排（实测「曼联」的前两条变成日记）。
    自带库没有相关性门槛（永远返回非空），所以注入前先用本地打分当闸门：
    本地对「常识」或「日记」都命中不了，这轮就不注入。
    自带库整个不可用时（服务没起、维度不对、库被删）直接退回本地。
    """

    def __init__(self, retrieve, kb_knowledge: str, kb_diary: str,
                 local: "KnowledgeBase | None" = None, kb_whale: str = "") -> None:
        self.retrieve = retrieve
        self.kb_knowledge = kb_knowledge
        self.kb_diary = kb_diary
        self.kb_whale = kb_whale
        self.local = local
        self._tags: dict[str, list[str]] = {}
        self._dates: dict[str, str] = {}
        if local is not None:
            self._index_local(Path(local.dir))

    # ---------- 本地 side info ----------

    def _index_local(self, directory: Path) -> None:
        for fp in list(directory.glob("*.md")) + list(directory.glob("*.txt")) + list(directory.glob("*/*.md")):
            try:
                meta, _body = parse_front_matter(fp.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            self._tags[fp.stem] = [str(t).strip() for t in (meta.get("tags") or []) if str(t).strip()]
            if meta.get("date"):
                self._dates[fp.stem] = str(meta["date"])

    def _adjust(self, stem: str, kind: str, score: float, query: str) -> float:
        hits = sum(1 for t in self._tags.get(stem, []) if len(t) >= 2 and t in query)
        if hits:
            score += TAG_HIT_BONUS * hits
        if kind == "diary":
            score *= DIARY_PREFER
        return score

    def _local_best(self, text: str):
        """本地视角下「常识」「日记」各自的最高分，用来判断该不该注入。"""
        if self.local is None or not getattr(self.local, "chunks", None):
            return None
        qt = tokens(text)
        if len(qt) < 2:
            return None
        best_kb = best_diary = 0.0
        for meta, piece in self.local.chunks:
            s = self.local._score(qt, piece, meta, text)
            if meta.get("kind") == "diary":
                best_diary = max(best_diary, s)
            else:
                best_kb = max(best_kb, s)
        return best_kb, best_diary

    def _gate(self, text: str, diary: bool = False) -> bool:
        got = self._local_best(text)
        if got is None:
            # 「在吗」「好」这种查不出词的，本地都没信号，就别拿自带库去硬凑
            return False
        best_kb, best_diary = got
        return best_diary >= 0.10 if diary else best_kb >= 0.08

    # ---------- 自带库 ----------

    async def _fetch(self, text: str, kb_name: str, kind: str, limit: int):
        """返回结果列表；整库不可用时返回 None（交给上层退回本地）。"""
        if not kb_name:
            return None
        try:
            got = await self.retrieve(
                text, [kb_name], top_k_fusion=20, top_m_final=max(limit, 5)
            )
        except Exception as e:
            logger.warning(f"[mind] 自带知识库「{kb_name}」不可用: {e}")
            return None
        if not isinstance(got, dict) or not got.get("results"):
            return None
        rows: list[dict] = []
        for item in got["results"] or []:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            doc_name = str(item.get("doc_name") or "")
            stem = Path(doc_name).stem
            date = self._dates.get(stem) or (stem if stem[:4].isdigit() else "")
            rows.append(
                {
                    "content": content,
                    "doc": doc_name,
                    "kind": kind,
                    "date": date,
                    "score": self._adjust(stem, kind, float(item.get("score") or 0.0), text),
                }
            )
        return rows

    async def query(self, text: str, k: int = 3) -> list[str]:
        if not self._gate(text):
            return []
        rows = await self._fetch(text, self.kb_knowledge, "kb", k)
        if rows is None:
            return self.local.query(text, k=k) if self.local is not None else []
        rows.sort(key=lambda r: r["score"], reverse=True)
        return [r["content"] for r in rows[:k]]

    async def query_whale(self, text: str, k: int = 1) -> list[dict]:
        """小鲸鱼的日记：相关性靠词面重合兜底（库里没有本地镜像可打分）。"""
        from . import whale as whale_mod

        if not self.kb_whale:
            return []
        rows = await self._fetch(text, self.kb_whale, "whale", max(k, 3))
        if not rows:
            return []
        out: list[dict] = []
        for r in rows:
            if not whale_mod.related(text, r["content"]):
                continue
            date = whale_mod.date_of(str(r.get("doc") or "")) or str(r.get("date") or "")
            out.append({"date": date, "text": whale_mod.clip(r["content"], 120),
                        "score": r["score"]})
            if len(out) >= k:
                break
        return out

    async def query_diary(self, text: str, k: int = 1) -> list[dict]:
        if not self._gate(text, diary=True):
            return []
        rows = await self._fetch(text, self.kb_diary, "diary", k)
        if rows is None:
            return self.local.query_diary(text, k=k) if self.local is not None else []
        rows.sort(key=lambda r: r["score"], reverse=True)
        return [{"date": r["date"], "title": "", "text": r["content"]} for r in rows[:k]]
