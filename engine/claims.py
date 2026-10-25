"""跨界面共享的「判断」，不是共享「经历」。

共享记录保留来源场景、经历方式、可见范围和稳定的 material_id：
QQ 可以用小鲸鱼形成的看法，但不能把另一边的现场经历冒充成刚刚发生。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from pathlib import Path

logger = logging.getLogger(__name__)

EXPERIENCE_HINT = {
    "watched": "看过",
    "heard": "听人说过",
    "talked": "跟人聊过",
    "read": "读到过",
    "diary_judgment": "记过",
}


def _confidence(value, default: float = 0.6) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    return round(max(0.0, min(1.0, value)) if math.isfinite(value) else default, 2)


def _material_id(source: str, source_scene: str, title: str) -> str:
    """生成不随日期/判断更新变化的回退 ID。

    外部来源应传入自己的稳定 ID（例如 whale:BV...）。回退值只用来源、场景和标题，
    这样同一素材更新判断时会覆盖旧判断，而不是不断追加重复记录。

    刻意不含日期与 stance：日期变化、判断被改写都应该落在同一条记录上。
    """
    raw = "\x1f".join((str(source), str(source_scene), str(title).strip().casefold()))
    return "claim-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class ClaimStore:
    """共享判断库。JSONL 追加写，旧记录可读取并自动补齐新字段。"""

    def __init__(self, path: Path | str, current_surface: str = "qq"):
        self.path = Path(path)
        self.current = str(current_surface or "qq")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: list[dict] = []
        self._sig = -1

    @staticmethod
    def _normalize(rec: dict) -> dict | None:
        if not isinstance(rec, dict):
            return None
        title = str(rec.get("title") or "").strip()[:80]
        stance = str(rec.get("stance") or "").strip()[:300]
        if not title or not stance:
            return None
        surface = str(rec.get("surface") or rec.get("source_scene") or "qq")[:16]
        source_scene = str(rec.get("source_scene") or surface)[:16]
        date = str(rec.get("date") or "")[:32]
        source = str(rec.get("source") or "")[:80]
        material_id = str(rec.get("material_id") or "").strip()
        # 1.1.1 生成的 claim-* ID 含日期和 stance，加载时重建成稳定 ID。
        if not material_id or material_id.startswith("claim-"):
            material_id = _material_id(source, source_scene, title)
        try:
            ts = float(rec.get("ts") or time.time())
        except (TypeError, ValueError, OverflowError):
            ts = time.time()
        if not math.isfinite(ts):
            ts = time.time()
        raw_visibility = rec.get("visibility")
        if raw_visibility in (None, ""):
            visibility = "shared"  # 旧记录没有该字段，按旧行为兼容
        elif raw_visibility in ("shared", "private"):
            visibility = raw_visibility
        else:
            logger.warning("[claims] 未知 visibility=%r，按 private 处理", raw_visibility)
            visibility = "private"
        return {
            "ts": ts,
            "date": date or time.strftime("%Y-%m-%d"),
            "surface": surface,
            "source_scene": source_scene,
            "canonical_id": str(rec.get("canonical_id") or "deepseek_xiaojingyu")[:64],
            "material_id": material_id[:80],
            "visibility": visibility,
            "kind": str(rec.get("kind") or "topic")[:16],
            "experience": str(rec.get("experience") or "heard")[:24],
            "title": title,
            "stance": stance,
            "confidence": _confidence(rec.get("confidence")),
            "source": source,
        }

    def load(self) -> int:
        sig = self.path.stat().st_mtime_ns if self.path.is_file() else 0
        items: list[dict] = []
        if not self.path.is_file():
            self._items = items
            self._sig = sig
            return 0
        try:
            for line in self.path.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    rec = self._normalize(json.loads(line))
                except Exception:
                    continue
                if rec is not None:
                    items.append(rec)
        except Exception as e:
            logger.warning(f"[claims] 读取失败: {e}")
        # 同一 material_id 可能因为判断更新而在 JSONL 中出现多次；重启后只保留最新一条。
        latest: dict[str, dict] = {}
        for item in items:
            key = item.get("material_id") or ""
            prev = latest.get(key)
            if prev is None or (item.get("ts") or 0) >= (prev.get("ts") or 0):
                latest[key] = item
        self._items = list(latest.values())
        self._sig = sig
        return len(self._items)

    def reload_if_changed(self) -> bool:
        try:
            sig = self.path.stat().st_mtime_ns if self.path.is_file() else 0
        except OSError:
            return False
        if sig != self._sig:
            self.load()
            return True
        return False

    def add(
        self,
        title: str,
        stance: str,
        *,
        surface: str,
        experience: str = "heard",
        kind: str = "topic",
        confidence: float = 0.6,
        source: str = "",
        date: str = "",
        source_scene: str = "",
        canonical_id: str = "deepseek_xiaojingyu",
        visibility: str = "shared",
        material_id: str = "",
    ) -> bool:
        source_scene = str(source_scene or surface or "qq")[:16]
        date = str(date or time.strftime("%Y-%m-%d"))[:32]
        rec = self._normalize({
            "ts": time.time(), "date": date, "surface": surface,
            "source_scene": source_scene, "canonical_id": canonical_id,
            "material_id": material_id or _material_id(source, source_scene, title),
            "visibility": visibility, "kind": kind, "experience": experience,
            "title": title, "stance": stance, "confidence": confidence, "source": source,
        })
        if rec is None:
            return False
        same = [old for old in self._items if old.get("material_id") == rec["material_id"]]
        if same and all(
            old.get("stance") == rec.get("stance")
            and old.get("visibility") == rec.get("visibility")
            for old in same
        ):
            return False
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if same:
                self._items = [old for old in self._items if old.get("material_id") != rec["material_id"]]
            self._items.append(rec)
            try:
                self._sig = self.path.stat().st_mtime_ns
            except OSError:
                self._sig = -1
            return True
        except Exception as e:
            logger.warning(f"[claims] 写入失败: {e}")
            return False

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        han = [c for c in str(text or "") if "\u4e00" <= c <= "\u9fff"]
        return {han[i] + han[i + 1] for i in range(len(han) - 1)}

    @staticmethod
    def _visible(rec: dict) -> bool:
        return rec.get("visibility", "shared") == "shared"

    def query(self, text: str, k: int = 2, min_score: float = 0.34) -> list[dict]:
        """按话题找共享判断；私有记录永远不进入检索。"""
        if not self._items:
            return []
        q = self._bigrams(text)
        if len(q) < 2:
            return []
        scored = []
        for rec in self._items:
            if not self._visible(rec):
                continue
            title = str(rec.get("title") or "")
            t = self._bigrams(title)
            s = self._bigrams(rec.get("stance") or "")
            hit_t = len(q & t) if t else 0
            hit_s = len(q & s) if s else 0
            if not hit_t and not hit_s:
                continue
            # 标题按「标题被覆盖多少」算；正文按「查询被覆盖多少」算（正文长，反过来算会永远很小）。
            # 正文权重 0.5：它能补召回，但不足以压过标题直接命中。
            cover_t = hit_t / max(1, len(t)) if t else 0.0
            cover_s = (hit_s / max(1, len(q))) * 0.5 if s else 0.0
            cover = max(cover_t, cover_s)
            score = cover * (0.7 + 0.3 * _confidence(rec.get("confidence")))
            if score >= min_score:
                scored.append((score, rec))
        scored.sort(key=lambda x: (x[0], x[1].get("ts") or 0), reverse=True)
        out, seen = [], set()
        for _score, rec in scored:
            key = (rec.get("title"), rec.get("source_scene"))
            if key in seen:
                continue
            seen.add(key)
            item = dict(rec)
            item["same_surface"] = item.get("surface") == self.current
            out.append(item)
            if len(out) >= k:
                break
        return out

    def recent(self, days: int = 3, k: int = 3) -> list[dict]:
        """最近几天的共享判断；调用方应谨慎使用，避免无关插话。"""
        cutoff = time.time() - days * 86400
        recent = [r for r in self._items if self._visible(r) and float(r.get("ts") or 0) >= cutoff]
        recent.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        out, seen = [], set()
        for rec in recent:
            key = (rec.get("title"), rec.get("source_scene"))
            if key in seen:
                continue
            seen.add(key)
            item = dict(rec)
            item["same_surface"] = item.get("surface") == self.current
            out.append(item)
            if len(out) >= k:
                break
        return out

    @staticmethod
    def to_line(rec: dict) -> str:
        title = str(rec.get("title") or "")
        stance = str(rec.get("stance") or "")
        exp = EXPERIENCE_HINT.get(str(rec.get("experience") or ""), "接触过")
        if rec.get("same_surface"):
            return f"{title}——你{exp}，{stance}"
        if rec.get("source") in ("小鲸鱼", "whale", "deepseek_xiaojingyu"):
            return f"{title}——听小鲸鱼说过，{stance}"
        origin = "日记那边" if rec.get("source_scene") in ("diary", "whale") else "另一边"
        return f"{title}——你在{origin}{exp}，{stance}"

    def stats(self) -> dict:
        return {
            "claims": len(self._items),
            "shared": sum(1 for item in self._items if self._visible(item)),
            "surface": self.current,
        }
