"""日记：把当天发生的事写成她自己的记录，存进知识库。

为什么不“看完视频立刻写”：
- 立刻写会写成观后感/作业，一天看三条就是三篇作业
- 隔天补写才有“过日子”的味道：视频、聊天、心情混在一起，是她的一天

数据流：
    feiyu_video ──写──> diary_materials/YYYY-MM-DD.jsonl
                              │
    persona_mind 每天第一次开口 ─┤
                              ▼
                       补写 → kb/diary/YYYY-MM-DD.md（带 tags / mood）
                              │
                        检索时优先被“想起来”

这个模块不 import astrbot，全部依赖注入，单测直接跑。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_MATERIALS_PER_DAY = 40     # 一天最多留这么多条素材，防止刷屏灌爆 prompt
MAX_MATERIAL_CHARS = 400       # 单条素材进 prompt 的截断长度
DIARY_MAX_CHARS = 500          # 日记正文上限


def _today() -> str:
    return date.today().isoformat()


def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


class Diary:
    def __init__(self, plugin_dir: Path | str, kb_dir: Path | str, keep_days: int = 30):
        self.dir = Path(plugin_dir)
        self.kb_dir = Path(kb_dir) / "diary"
        self.mat_dir = self.dir / "diary_materials"
        self.mat_dir.mkdir(parents=True, exist_ok=True)
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self.keep_days = max(1, int(keep_days))

    # ---------- 素材 ----------

    def _mat_file(self, day: str) -> Path:
        return self.mat_dir / f"{day}.jsonl"

    def add(self, kind: str, payload: dict, umo: str = "", day: str | None = None) -> bool:
        """记一条素材。kind: video / chat / mood / event。"""
        day = day or _today()
        rec = {
            "ts": time.time(),
            "kind": str(kind)[:16],
            "umo": str(umo)[:120],
            **(payload or {}),
        }
        try:
            with self._mat_file(day).open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return True
        except Exception as e:  # 写素材失败不能影响主流程
            logger.warning(f"[diary] 素材写入失败: {e}")
            return False

    def materials(self, day: str) -> list[dict]:
        fp = self._mat_file(day)
        if not fp.is_file():
            return []
        out = []
        try:
            for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"[diary] 素材读取失败: {e}")
        return out[-MAX_MATERIALS_PER_DAY:]

    # ---------- 状态 ----------

    def diary_file(self, day: str) -> Path:
        return self.kb_dir / f"{day}.md"

    def has_diary(self, day: str) -> bool:
        return self.diary_file(day).is_file()

    def pending_days(self, lookback: int = 7) -> list[str]:
        """有素材、但还没写日记的日子（从旧到新）。"""
        out = []
        today = date.today()
        for i in range(1, lookback + 1):
            day = (today - timedelta(days=i)).isoformat()
            if self.has_diary(day):
                continue
            if self.materials(day):
                out.append(day)
        return list(reversed(out))

    # ---------- 生成 ----------

    def build_prompt(self, day: str, persona_name: str = "") -> str:
        """把当天素材拼成一段给模型看的材料，不是让她复述，是让她回想。"""
        mats = self.materials(day)
        lines = []
        for m in mats:
            kind = m.get("kind", "")
            if kind == "video":
                title = str(m.get("title") or "").strip()
                summary = str(m.get("summary") or m.get("主题") or "").strip()
                mood = str(m.get("feel") or "").strip()
                piece = f"看了个视频《{title}》" if title else "看了个视频"
                if summary:
                    piece += f"，讲的是{summary}"
                if mood:
                    piece += f"（当时的反应：{mood}）"
                lines.append("- " + piece[:MAX_MATERIAL_CHARS])
            elif kind == "chat":
                text = str(m.get("text") or "").strip()
                if text:
                    lines.append("- 聊到：" + text[:MAX_MATERIAL_CHARS])
            elif kind == "event":
                text = str(m.get("text") or "").strip()
                if text:
                    lines.append("- " + text[:MAX_MATERIAL_CHARS])
        material_block = "\n".join(lines) if lines else "（今天没留下什么特别的事）"

        return (
            f"现在是 {day} 的深夜。你要给自己写一篇日记。\n\n"
            "今天留下的这些事：\n"
            f"{material_block}\n\n"
            "要求：\n"
            "- 用你自己的口吻，第一人称，像随手记，不要作文腔\n"
            "- 挑你**真的在意**的一两件写，别逐条复述上面的清单\n"
            "- 可以带情绪、可以吐槽、可以写到自己最近的状态\n"
            "- 200~400 字，分段，不用标题，不用 Markdown\n"
            "- 不要写「今天看了视频」这种流水账式开头，从具体的感受或画面切入\n"
            "- 不要在日记里提到「素材」「系统」「记录」这类词\n\n"
            "另外给我四样东西：\n"
            "1. mood：0-100，你这篇日记写完时的心情（0 很沉，100 很好）\n"
            "2. tags：3-5 个触发词，将来聊到这些词时这篇日记会被想起来。"
            "要用具体的词（比如「鲸鱼」「深海」「加班」），不要「视频」「日记」「心情」这种废话\n"
            "3. title：给这篇日记起个短名字，不超过 8 个字\n"
            "4. claims：这篇日记里你形成了什么判断，0-2 条；没有就给空数组。\n"
            "   每条写 title（在说哪个具体的东西，越具体越好）和 stance（你的看法，一句话，带态度）。\n"
            "   只写你确实形成了看法的；夸一句「不错」不算判断。\n\n"
            "严格输出 JSON，不要多余文字：\n"
            '{"mood": 55, "tags": ["鲸鱼", "深海"], "title": "鲸鱼的声音", '
            '"claims": [{"title": "猫咪地铁站", "stance": "之前低估了，真搭了个地铁站，值得再看"}], '
            '"text": "日记正文…"}'
        )

    def parse_reply(self, raw: str) -> dict | None:
        """从模型回复里取 JSON。"""
        if not raw:
            return None
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        text = str(data.get("text") or "").strip()
        if len(text) < 30:
            return None
        try:
            mood = float(data.get("mood"))
        except (TypeError, ValueError):
            mood = 55.0
        mood = max(0.0, min(100.0, mood))
        tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
        tags = [t for t in tags if 1 <= len(t) <= 12][:5]
        title = str(data.get("title") or "").strip()[:20] or "没起名字"
        claims = []
        raw_claims = data.get("claims")
        if isinstance(raw_claims, list):
            for raw in raw_claims:
                if not isinstance(raw, dict):
                    continue
                claim_title = str(raw.get("title") or "").strip()[:80]
                stance = str(raw.get("stance") or "").strip()[:300]
                if claim_title and stance:
                    claims.append({"title": claim_title, "stance": stance})
                    if len(claims) >= 2:
                        break
        return {
            "mood": mood,
            "tags": tags,
            "title": title,
            "claims": claims,
            "text": text[:DIARY_MAX_CHARS],
        }

    def save(self, day: str, entry: dict, sources: list[str] | None = None) -> Path:
        """落盘成带 frontmatter 的 md，检索时自带走标签加权。"""
        tags = ", ".join(entry.get("tags") or [])
        mood = entry.get("mood")
        mood_line = f"{mood:.0f}" if isinstance(mood, (int, float)) else ""
        head = (
            "---\n"
            f"date: {day}\n"
            "kind: diary\n"
            f"title: {entry.get('title') or ''}\n"
            f"tags: {tags}\n"
            f"mood: {mood_line}\n"
            + (f"source: {', '.join(sources[:5])}\n" if sources else "")
            + "---\n\n"
        )
        fp = self.diary_file(day)
        fp.write_text(head + str(entry.get("text") or "").strip() + "\n", encoding="utf-8")
        return fp

    # ---------- 清理 ----------

    def prune(self) -> int:
        """删掉太旧的素材文件（日记本身保留）。"""
        cutoff = date.today() - timedelta(days=self.keep_days * 3)
        removed = 0
        for fp in self.mat_dir.glob("*.jsonl"):
            stem = fp.stem
            try:
                d = datetime.strptime(stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff:
                try:
                    fp.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def stats(self) -> dict:
        return {
            "diaries": len(list(self.kb_dir.glob("*.md"))),
            "material_days": len(list(self.mat_dir.glob("*.jsonl"))),
            "pending": len(self.pending_days()),
        }
