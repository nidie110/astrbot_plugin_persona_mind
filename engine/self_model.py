"""自我模型：让经历变成她自己的判断。

为什么单独一层：
- PAD 只记「此刻什么心情」，分钟级；
- profiles 只记「这个人是谁」，事实级；
- 缺一层「我怎么看他」「我是什么样的人」，周～月级。

设计取舍（三条，都来自 1.2.0 设计稿的复核）：
1. 不新增 LLM 调用：候选全部从已有的 appraisal 判读里提取，claim 用模板拼，
   这样同一个 kind 的句子高度同形，重复证据才匹配得上。
2. 三张表互斥，按「主语是谁」路由：
   他 → beliefs/<uid>.jsonl；我 → self_beliefs.jsonl；那个东西 → claims.jsonl（已有）。
3. 先验不是判决：人格稿给的 prior 只被证据削弱，不被时间冲淡。
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from pathlib import Path

# ── 词与阈值 ────────────────────────────────────────────────

STOP = {
    "的", "了", "是", "在", "和", "也", "就", "都", "还", "不", "我", "你",
    "他", "她", "它", "这", "那", "有", "没", "吗", "呢", "啊", "吧", "呀",
    "会", "能", "个", "很", "太", "又", "再", "但", "而", "被", "把", "让",
    "说", "做", "去", "来", "上", "下", "里", "时候", "什么", "怎么", "一下",
}

# claim 模板：同一个 kind 的句子必须同形，否则三元组匹配会失效
TEMPLATES = {
    ("trust", "comfort"): "他难受的时候会来找我",
    ("trust", "vent"):    "他难受的时候会来找我",
    ("trust", "praise"):  "他夸我的时候是真心的",
    ("routine", "chat"):  "跟他聊这件事我会开心",
    ("capability", "advice"): "他会问我该怎么办",
    ("boundary", "sharp"): "我不喜欢被这样说",
    ("boundary", "empty"): "我不喜欢被敷衍",
}

JACCARD_MIN = 0.5

# topic 语义组：判「是不是同一类事」，不判「用词像不像」。
# comfort 与 vent 都指向「他需要被接住」，必须合并成一条信念；
# 否则模型今天判 comfort、明天判 vent，两条各 hold 一次，永远到不了 accept。
TOPIC_GROUP = {
    "comfort": "comfort",
    "vent": "comfort",
    "praise": "praise",
    "advice": "advice",
    "sharp": "boundary",
    "empty": "boundary",
}

def _topic_group(topic) -> str:
    t = str(topic or "").strip()
    return TOPIC_GROUP.get(t, t)
          # 关键词相似度阈值
RELEVANCE_FLOOR = 0.30     # 绝对下限（相对阈值在调用方算）
DECAY_HALF_LIFE_DAYS = 45  # 信念半衰：比情绪慢得多，但也不是终身


def _now() -> float:
    return time.time()


def _int_cfg(cfg: dict, key: str, default: int) -> int:
    """读整数配置：0 是合法值（上限 0 = 禁止），不能被 `or` 顶掉。"""
    try:
        raw = cfg.get(key, default)
        if raw is None or raw == "":
            return default
        return int(raw)
    except (TypeError, ValueError):
        return default


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _days_ago(days: int) -> str:
    """N 天前的日期串，用于清理过期审计与快照。"""
    return time.strftime("%Y-%m-%d", time.localtime(_now() - max(0, days) * 86400))


def keywords(text: str, limit: int = 6) -> list[str]:
    """从一句话里抽实词（2-4 字），用于三元组匹配。"""
    out: list[str] = []
    for seg in re.findall(r"[\u4e00-\u9fa5]{2,4}", text or ""):
        if seg in STOP or len(seg) < 2:
            continue
        if seg not in out:
            out.append(seg)
        if len(out) >= limit:
            break
    return out


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


class SelfModel:
    """她对自己的判断 + 对某个人的预期。"""

    def __init__(self, data_dir: Path, cfg: dict | None = None):
        self.dir = Path(data_dir)
        self.cfg = cfg or {}
        self.model_path = self.dir / "self_model.json"
        self.self_beliefs_path = self.dir / "self_beliefs.jsonl"
        self.beliefs_dir = self.dir / "beliefs"
        self.events_path = self.dir / "evolution_events.jsonl"
        self.snap_dir = self.dir / "evolution_snapshots"

        self.state = self._load_state()
        self._daily = dict(self.state.get("daily") or {})
        if self._daily.get("date") != _today():
            self._daily = {"date": _today(), "candidates": 0, "believed": 0}

    # ── 状态 ────────────────────────────────────────────────

    def _save_daily(self) -> None:
        """把当日计数写回 self_model.json，重启不清零（复审 P2-6）。"""
        self.state["daily"] = dict(self._daily)
        try:
            self._save_state()
        except Exception:
            pass

    def _load_state(self) -> dict:
        default = {
            "charter_version": 1,
            "charter": [
                "不把听说的事情说成亲历",
                "不确定时承认不确定",
                "可以拒绝，不为了完成率强行迎合",
                "亲近不等于失去边界",
                "不凭一条消息给人下永久结论",
            ],
            "evolution_mode": str(self.cfg.get("evolution_mode", "rule") or "rule"),
            "paused": False,
            "priors": {},
            "last_reset": _today(),
        }
        if self.model_path.is_file():
            try:
                data = json.loads(self.model_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    default.update({k: v for k, v in data.items() if v is not None})
                    return default
            except (OSError, json.JSONDecodeError):
                pass
        self._save_state(default)
        return default

    def _save_state(self, state: dict | None = None) -> None:
        st = state if state is not None else self.state
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.model_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.model_path)
        except OSError:
            pass

    @property
    def mode(self) -> str:
        return str(self.state.get("evolution_mode") or "rule")

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and not self.state.get("paused", False)

    # ── 观察：从判读结果提取候选（零额外 LLM） ───────────────

    def observe(self, *, thought: dict, text: str, uid: str, scene: str,
                relevance: float = 0.0, relevance_cut: float = 0.0) -> dict | None:
        """返回候选 dict 或 None。调用方负责传 relevance（来自 appraisal）。"""
        if not self.enabled:
            return None
        # 消息去重：QQ 重试 / 测试重放会让同一句话变成多次「独立证据」（专家 P2-8）
        seen = self.__dict__.setdefault("_seen", {})
        fp = (uid, scene, (text or "")[:40])
        now = _now()
        if float(seen.get(fp, 0) or 0) > now - 120:
            return None
        seen[fp] = now
        if len(seen) > 200:
            self.__dict__["_seen"] = {k: v for k, v in seen.items() if v > now - 600}
        if self._daily["date"] != _today():
            self._daily = {"date": _today(), "candidates": 0, "believed": 0}
            self._save_daily()
        cap = _int_cfg(self.cfg, "evolution_max_candidates_per_day", 3)
        if self._daily["candidates"] >= cap:
            return None
        # relevance 双闸：相对阈值（调用方按当天分位算）+ 绝对地板
        cut = max(float(relevance_cut or 0.0), RELEVANCE_FLOOR)
        if float(relevance or 0.0) < cut:
            return None

        thought = thought if isinstance(thought, dict) else {}
        want = str(thought.get("want_from_me") or "").strip().lower()
        mood = str(thought.get("mood") or "")
        doing = str(thought.get("doing") or "")

        # 分支：want_from_me 决定关系类；强烈情绪决定边界类
        if want in ("comfort", "vent", "praise", "advice"):
            kind = "capability" if want == "advice" else "trust"
            key = (kind, want if want != "advice" else "advice")
            claim = TEMPLATES.get(key)
            if not claim:
                return None
            scope = f"user:{uid}"
            src = "user"
            topic = want
        elif _is_sharp(text) and _is_negative(mood):
            kind, claim = "boundary", TEMPLATES[("boundary", "sharp")]
            scope, src = "self", "self"
            topic = "sharp"
        elif _is_empty(text) and scene == "group":
            kind, claim = "boundary", TEMPLATES[("boundary", "empty")]
            # 「被敷衍」只记在这个人名下：群里的空 @ 是常态，记成全局
            # 会让所有陌生人都吃到她的态度（专家指出）。
            scope, src = f"user:{uid}", "user"
            topic = "empty"
        else:
            return None

        cand = {
            "id": f"evt-{uuid.uuid4().hex[:6]}",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "uid": uid,
            "scene": scene,
            "scope": scope,
            "kind": kind,
            "topic": topic,
            "claim": claim,
            "keywords": keywords(claim),
            "relevance": round(float(relevance or 0.0), 3),
            "evidence": [doing[:40] if doing else text[:40]],
        }
        self._daily["candidates"] += 1
        self._save_daily()
        return cand

    # ── 匹配：三元组（scope, kind, keywords） ───────────────

    @staticmethod
    def _safe_scope(scope: str) -> str:
        """把任意来源的 scope 收敛成 self 或 user:<安全 uid>。

        外部来源（适配器 uid、LLM 输出、pending 队列、state 文件）都可能带
        路径成分；这里统一挡一道，避免 beliefs_dir 之外被写入/读取。
        """
        raw = str(scope or "").strip()
        if not raw or raw == "self":
            return "self"
        m = re.fullmatch(r"user:([A-Za-z0-9_.\-]{1,64})", raw)
        if not m:
            return "self"
        uid = m.group(1)
        if uid in (".", "..") or "/" in uid or "\\" in uid:
            return "self"
        return f"user:{uid}"

    def _belief_path(self, scope: str) -> Path:
        """按 scope 决定落到哪张表——主语是谁就写哪张（三表互斥）。"""
        scope = self._safe_scope(scope)
        if scope.startswith("self"):
            return self.self_beliefs_path
        uid = scope.split(":", 1)[1] if ":" in scope else "unknown"
        return self.beliefs_dir / f"{uid or 'unknown'}.jsonl"

    def match(self, cand: dict) -> dict | None:
        """找同一条信念：scope + kind + topic 组相同即同类。

        C 方案（2026-10-24）：旧实现用 keywords 的 Jaccard 判同类，但 keywords
        来自模板 claim，同 want 才相同；模型把同一件事判成 comfort / vent 时
        两条 claim 不同、各 hold 一次，永远到不了 accept。改为按语义组判同类，
        keywords 只在同组内部挑最像的那一条。
        老数据没有 topic，退回原来的关键词判据。
        """
        rows = _read_jsonl(self._belief_path(cand["scope"]))
        g = _topic_group(cand.get("topic"))
        best, best_score = None, -1.0
        for row in rows:
            if row.get("scope") != cand["scope"] or row.get("kind") != cand["kind"]:
                continue
            rg = _topic_group(row.get("topic"))
            sim = jaccard(cand.get("keywords") or [], row.get("keywords") or [])
            if g and rg:
                if g != rg:
                    continue
            elif sim < JACCARD_MIN:
                continue
            if sim > best_score:
                best, best_score = row, sim
        return best

    # ── 应用：写入或加强 ────────────────────────────────────

    def apply(self, cand: dict, decision: str = "accept", auto: bool = True) -> dict:
        """decision: accept / hold / reject。返回写进事件审计的一行。

        auto=True 是模型每轮自动发起的（不是管理员批准）：新信念先记 hold，
        被独立命中 evolution_confirm_count 次才转 accept——charter 第 5 条
        「不凭一条消息给人下永久结论」必须由代码保证，不能靠文案自觉。
        """
        if not isinstance(cand, dict):
            return {"decision": decision, "reason": "无候选可应用", "matched": None}
        event = dict(cand, decision=decision, matched=None)
        if not self.enabled:
            event["reason"] = "evolution 未开启"
            return event

        hit = self.match(cand)
        if decision == "reject":
            if hit:
                event["before"] = dict(hit)     # F09：删除/削弱也要留底
                self._weaken(hit, cand)
                event["matched"] = hit.get("id")
                event["reason"] = "相反证据，强度回落"
            else:
                event["reason"] = "拒绝，未写入"
            self._append_event(event)
            return event

        if hit and hit.get("admin_reject"):
            # 复审 P1-3：管理员否决过的信念，普通消息不许复活（只有人工采纳能解）
            event["matched"] = hit.get("id")
            event["reason"] = "管理员已否决，不再采集这条证据"
            self._append_event(event)
            return event

        if hit:
            # 确认窗口：证据跨太久不算同一条（evolution_confirm_window_days）
            before_snap = dict(hit)   # 先抓快照，窗口重置也算一次改动
            window = int(self.cfg.get("evolution_confirm_window_days", 7) or 7)
            last = str(hit.get("last_confirmed") or "")[:10]
            if last:
                try:
                    gap = (time.mktime(time.strptime(_today(), "%Y-%m-%d"))
                           - time.mktime(time.strptime(last, "%Y-%m-%d"))) / 86400
                except (ValueError, OverflowError):
                    gap = 0
                if gap > window:
                    hit["evidence_count"] = 0
                    hit["evidence"] = []
                    hit["status"] = "hold"
            hit["evidence"] = (hit.get("evidence") or [])[-4:] + [cand["evidence"][0]]
            hit["evidence_count"] = int(hit.get("evidence_count", 1)) + 1
            hit["strength"] = _strength(hit["evidence_count"])
            hit["last_confirmed"] = _today()
            need = _int_cfg(self.cfg, "evolution_confirm_count", 3)
            if decision == "accept":
                if hit.get("manual"):
                    hit["status"] = "accept"      # 人工裁决不被支持证据撤销（F08）
                else:
                    hit["status"] = "accept" if (not auto or hit["evidence_count"] >= need) else "hold"
            self._rewrite(hit, cand)
            event["matched"] = hit.get("id")
            event["before"] = before_snap      # 回滚用：变更前的整行
            event["after"] = {"status": hit.get("status"),
                              "evidence_count": int(hit.get("evidence_count", 0))}
            event["reason"] = f"命中已有信念，证据 {hit['evidence_count']} 次"
        else:
            cap = _int_cfg(self.cfg, "evolution_max_new_beliefs_per_day", 1)
            if cap <= 0 or self._daily["believed"] >= cap:      # 0/负数 = 禁止自动长新信念
                event["reason"] = "当天新信念已达上限"
                self._append_event(event)
                return event
            need_new = _int_cfg(self.cfg, "evolution_confirm_count", 3)
            row = {
                "id": f"b-{uuid.uuid4().hex[:6]}",
                "scope": cand["scope"],
                "kind": cand["kind"],
                "topic": cand.get("topic"),
                "claim": cand["claim"],
                "keywords": (cand.get("keywords") or []),
                "evidence": [cand["evidence"][0]],
                "evidence_count": 1,
                "strength": _strength(1),
                "confidence": round(float(cand.get("relevance", 0.5)), 2),
                "status": ("accept" if (decision == "accept"
                                        and (not auto or need_new <= 1)) else "hold"),
                "created_at": _today(),
                "last_confirmed": _today(),
                "source_scene": cand.get("scene") or "",
            }
            self._append(row)
            event["created"] = True          # 新建事件：回滚 = 删掉这条信念
            self._daily["believed"] = int(self._daily.get("believed", 0)) + 1
            self._save_daily()
            event["matched"] = row["id"]
            event["before"] = dict(row)
            event["after"] = {"status": row.get("status"),
                              "evidence_count": int(row.get("evidence_count", 0))}
            event["created"] = True
            event["reason"] = "新信念"
        self._append_event(event)
        return event

    def _user_ids(self) -> list[str]:
        """beliefs/ 目录下的用户 uid 列表（用于按 id 反查信念）。"""
        if not self.beliefs_dir.is_dir():
            return []
        return [f.stem for f in self.beliefs_dir.glob("*.jsonl")]

    def approve(self, belief_id: str, verdict: str = "accept") -> dict:
        """管理员对现有信念直接裁决：只改状态，不再结算一次证据。

        旧实现把历史事件重放给 apply()，同一条证据会被算两次（专家 P1-4）。
        这里按信念 id 定位，evidence_count 不变，只补审计。
        """
        verdict = "accept" if verdict in ("accept", "采纳", "approve") else "reject"
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        for scope in ["self"] + [f"user:{u}" for u in self._user_ids()]:
            path = self._belief_path(scope)
            rows = _read_jsonl(path)
            for i, r in enumerate(rows):
                if str(r.get("id")) != str(belief_id):
                    continue
                rows_before = [dict(x) for x in rows]
                r["status"] = verdict
                if verdict == "accept":
                    # 人工采纳不伪造证据条数（复审 P2-7）：只标记 manual 并给满分。
                    r["manual"] = True
                    r["strength"] = 1.0
                    r["confidence"] = max(float(r.get("confidence", 0) or 0), 0.9)
                    r.pop("admin_reject", None)   # 人工采纳 = 解除否决锁
                else:
                    r["strength"] = 0.0
                    r["confidence"] = 0.0
                    r["admin_reject"] = True      # 人工否决 = 持久锁，普通消息不能复活
                r["last_confirmed"] = _today()
                rows[i] = r
                _write_jsonl(path, rows)
                event = {
                    "ts": now, "id": f"evt-{uuid.uuid4().hex[:6]}",
                    "decision": verdict, "matched": r.get("id"),
                    "before": dict(rows_before[i]),
                    "claim": r.get("claim"), "scope": scope,
                    "reason": f"管理员{verdict}", "by": "admin",
                }
                self._append_event(event)
                return event
        return {"decision": verdict, "reason": "未找到该信念", "matched": None}

    # ── daily 档：先排队，晚上统一想 ────────────────────────

    def _pending_path(self) -> Path:
        return self.dir / "pending_beliefs.jsonl"

    def enqueue(self, cand: dict | None) -> bool:
        """daily / deep 档：不当场结算，先记下来等当天统一归纳。"""
        if not isinstance(cand, dict) or not cand.get("claim"):
            return False
        row = dict(cand)
        row["queued_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        try:
            self._pending_path().parent.mkdir(parents=True, exist_ok=True)
            with self._pending_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    def pending(self, max_age_days: int = 2) -> list[dict]:
        """还没被归纳的候选；默认带上前一天的（避免整天没消息就永久丢失）。"""
        rows = _read_jsonl(self._pending_path())
        try:
            cutoff = time.mktime(time.localtime()) - max(0, int(max_age_days)) * 86400
        except (ValueError, OverflowError):
            cutoff = 0.0
        out = []
        for r in rows:
            if r.get("consumed"):
                continue
            stamp = str(r.get("queued_at", ""))[:10]
            try:
                ts = time.mktime(time.strptime(stamp, "%Y-%m-%d"))
            except (ValueError, OverflowError):
                continue
            if ts >= cutoff:
                out.append(r)
        return out

    def clear_pending(self, ids: list[str] | None = None) -> None:
        """清掉已归纳的行；不给 ids 就清到今天为止（兼容旧调用）。"""
        rows = _read_jsonl(self._pending_path())
        if ids is not None:
            drop = {str(x) for x in ids}
            keep = [r for r in rows if str(r.get("queued_at")) + str(r.get("claim")) not in drop]
        else:
            today = _today()
            keep = [r for r in rows if str(r.get("queued_at", ""))[:10] > today]
        _write_jsonl(self._pending_path(), keep)

    def daily_prompt(self, max_age_days: int = 2) -> str:
        """把队列整理成一段归纳请求。"""
        rows = self.pending(max_age_days=max_age_days)
        if not rows:
            return ""
        lines = []
        for r in rows[:20]:
            ev = (r.get("evidence") or [""])[0]
            lines.append(
                f"- [{r.get('kind')}] {r.get('claim')}"
                f"（scope={r.get('scope')}；场景：{ev}；相关度 {r.get('relevance')}）"
            )
        return (
            "下面是你（一个住在机房里的 AI）今天注意到的几件事，来自你和别人聊天的瞬间：\n"
            + "\n".join(lines)
            + "\n\n请你用自己的话，把它们归纳成 0-3 条**你真正相信的**判断。规则：\n"
            "1. 只写有证据支撑的；拿不准的一条都别写，可以返回空数组。\n"
            "2. 用第一人称，一句话，不超过 30 个字，像自己心里记的，不要书面语。\n"
            "3. 不要把同一件事拆成两条。\n"
            "4. 只输出 JSON：{\"beliefs\": [{\"claim\": \"...\", \"kind\": \"trust|boundary|preference|pattern\", "
            "\"scope\": \"self 或 user:QQ号\", \"confidence\": 0.0-1.0}]}\n"
        )

    def apply_daily(self, items: list[dict], confidence_floor: float = 0.55) -> int:
        """把 LLM 的归纳结果写成信念；一律 hold，等管理员 review。"""
        if not self.enabled:      # 暂停或 off 档：连写入都不许（F07）
            return 0
        n = 0
        for it in items or []:
            if not isinstance(it, dict):
                continue
            claim = str(it.get("claim") or "").strip()
            if not claim or len(claim) > 60:
                continue
            try:
                conf = float(it.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < confidence_floor:
                continue
            scope = str(it.get("scope") or "self").strip()
            # 只接受 user:纯数字；模型输出任何带路径成分的一律回落 self
            if not re.fullmatch(r"user:[A-Za-z0-9_.\-]{1,64}", scope):
                scope = "self"
            row = {
                "id": f"b-{uuid.uuid4().hex[:6]}",
                "scope": scope,
                "kind": str(it.get("kind") or "pattern").strip()[:20],
                "topic": None,
                "claim": claim,
                "keywords": keywords(claim),
                "evidence": [str(it.get("claim") or "")[:40]],
                "evidence_count": 1,
                "strength": _strength(1),
                "confidence": round(conf, 2),
                "status": "hold",              # daily 档一律人工复核
                "created_at": _today(),
                "last_confirmed": _today(),
                "source_scene": "daily",
                "manual": False,
            }
            if self.match(row):          # 同一件事别重复记
                continue
            cap = _int_cfg(self.cfg, "evolution_max_new_beliefs_per_day", 1)
            if cap <= 0 or _count_today(_read_jsonl(self._belief_path(scope))) >= cap:
                break
            self._append(row)
            self._append_event({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "id": f"evt-{uuid.uuid4().hex[:6]}", "decision": "hold",
                "matched": row["id"], "claim": claim, "scope": scope,
                "reason": "daily 归纳", "by": "daily"})
            n += 1
        return n

    # ── 回滚 ────────────────────────────────────────────────

    def full_snapshot(self) -> None:
        """每天一份全量快照（覆盖式）。

        原来的 _snapshot 是增量（只存最近的变更行），能审计但不能恢复整天状态，
        所以回滚另外存一份全量。
        """
        try:
            self.snap_dir.mkdir(parents=True, exist_ok=True)
            data = {"self": _read_jsonl(self.self_beliefs_path), "users": {}}
            if self.beliefs_dir.is_dir():
                for f in sorted(self.beliefs_dir.glob("*.jsonl")):
                    data["users"][f.stem] = _read_jsonl(f)
            _stamp = time.strftime("%Y-%m-%d-%H%M%S")
            (self.snap_dir / f"full-{_stamp}.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (OSError, TypeError):
            pass

    def restore_snapshot(self, date: str) -> dict:
        """把某天的全量快照写回（B 方案：日级回滚）。"""
        cands = sorted(self.snap_dir.glob(f"full-{date}*.json"))
        if not cands:
            return {"ok": False, "reason": f"没有 {date} 的全量快照"}
        f = cands[-1]          # 同一天多份时取最新那份
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"ok": False, "reason": f"快照读不动：{e}"}
        self.full_snapshot()                      # 先把当前存一份，能反悔
        n = 0
        self_rows = data.get("self") or []
        _write_jsonl(self.self_beliefs_path, self_rows)
        n += len(self_rows)
        users = data.get("users") or {}
        if self.beliefs_dir.is_dir():
            for old in self.beliefs_dir.glob("*.jsonl"):
                if old.stem not in users:
                    old.unlink(missing_ok=True)
        for uid, rows in users.items():
            self.beliefs_dir.mkdir(parents=True, exist_ok=True)
            _write_jsonl(self.beliefs_dir / f"{self._safe_scope(f"user:{uid}").split(":", 1)[1] or "unknown"}.jsonl", rows)
            n += len(rows)
        self._append_event({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                            "id": f"evt-{uuid.uuid4().hex[:6]}", "decision": "restore",
                            "matched": None, "claim": None, "scope": None,
                            "reason": f"恢复到 {date}（{n} 条）", "by": "admin"})
        return {"ok": True, "count": n}

    def rollback_event(self, event_id: str, force: bool = False) -> dict:
        """撤销某次事件（A 方案：单条操作级回滚）。

        F04：回滚前比对当前行是否仍等于该事件的 after——
        记了 after 就做版本校验，不一致默认拒绝（陈旧事件不许覆盖后续变更）。
        """
        events = _read_jsonl(self.events_path)
        ev = next((e for e in events if str(e.get("id")) == str(event_id)), None)
        if not ev:
            return {"ok": False, "reason": f"没找到事件 {event_id}"}
        before = ev.get("before")
        if ev.get("created") and isinstance(before, dict):
            path = self._belief_path(str(before.get("scope") or ""))
            cur_rows = _read_jsonl(path)
            cur = next((r for r in cur_rows if str(r.get("id")) == str(before.get("id"))), None)
            after_c = ev.get("after")
            if cur is not None and not force and isinstance(after_c, dict):
                same = (cur.get("status") == after_c.get("status")
                        and int(cur.get("evidence_count", 0)) == int(after_c.get("evidence_count", 0)))
                if not same:
                    return {"ok": False, "conflict": True,
                            "reason": "这条信念在建好之后又长过证据/被裁决过；确认要删请加 --force"}
            rows = [r for r in cur_rows if str(r.get("id")) != str(before.get("id"))]
            _write_jsonl(path, rows)
            self._append_event({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "id": f"evt-{uuid.uuid4().hex[:6]}", "decision": "rollback_delete",
                "matched": before.get("id"), "claim": before.get("claim"),
                "scope": before.get("scope"), "reason": f"撤销新建 {event_id}",
                "by": "admin"})
            self._daily["believed"] = max(0, int(self._daily.get("believed", 0)) - 1)
            self._save_daily()
            return {"ok": True, "deleted": True, "claim": before.get("claim")}
        if not isinstance(before, dict):
            return {"ok": False, "reason": "这条事件没有变更前快照（老数据无法回滚）"}
        path = self._belief_path(str(before.get("scope") or ""))
        rows = _read_jsonl(path)
        bid = str(before.get("id") or "")
        after = ev.get("after")
        if not force and isinstance(after, dict):
            cur = next((r for r in rows if str(r.get("id")) == bid), None)
            if cur is not None:
                same = (cur.get("status") == after.get("status")
                        and int(cur.get("evidence_count", 0)) == int(after.get("evidence_count", 0)))
                if not same:
                    return {"ok": False,
                            "reason": "该信念在此事件之后又变过；确认要覆盖请加 --force",
                            "conflict": True}
        for i, r in enumerate(rows):
            if str(r.get("id")) == bid:
                rows[i] = before
                _write_jsonl(path, rows)
                self._append_event({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                    "id": f"evt-{uuid.uuid4().hex[:6]}", "decision": "rollback",
                    "matched": bid, "claim": before.get("claim"),
                    "scope": before.get("scope"), "reason": f"撤销 {event_id}",
                    "by": "admin"})
                return {"ok": True, "claim": before.get("claim")}
        rows = [r for r in rows if str(r.get("id")) != bid]
        _write_jsonl(path, rows)
        return {"ok": True, "claim": None, "deleted": True}

    def _weaken(self, hit: dict, cand: dict) -> None:
        """相反证据：只被证实过一次的直接作废，否则强度打折。

        `max(1, int(1 * 0.6))` 恒等于 1 是死操作（专家指出）——所以
        单次证据的信念必须显式 reject，否则「拒绝」等于没按。
        """
        count = int(hit.get("evidence_count", 1) or 1)
        if count <= 1:
            hit["status"] = "reject"
            hit["confidence"] = 0.0
            hit["strength"] = 0.0
        else:
            count = max(1, int(count * 0.6))
            hit["evidence_count"] = count
            hit["strength"] = _strength(count)
            hit["confidence"] = max(0.0, float(hit.get("confidence", 0.5)) - 0.15)
        self._rewrite(hit, cand)

    def _append(self, row: dict) -> None:
        path = self._belief_path(row["scope"])
        rows = _read_jsonl(path)
        rows.append(row)
        self._snapshot(path, rows[-2:])
        _write_jsonl(path, rows)

    def _rewrite(self, row: dict, cand: dict) -> None:
        path = self._belief_path(row["scope"])
        rows = _read_jsonl(path)
        for i, r in enumerate(rows):
            if r.get("id") == row.get("id"):
                rows[i] = row
                break
        else:
            rows.append(row)
        self._snapshot(path, [row])
        _write_jsonl(path, rows)

    def _append_event(self, event: dict) -> None:
        try:
            # 审计行必须有时间戳：老代码构造的 event 不带 ts，
            # 会被 _prune 的 [r["ts"][:10] >= cut] 判成"上古"整批删掉。
            event.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()))
            event.setdefault("id", f"evt-{uuid.uuid4().hex[:6]}")
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
        self._prune()

    def _prune(self) -> None:
        """按配置裁掉过老的审计与快照——否则这两个文件会无限长大。

        配置项是承诺：evolution_max_history_days / evolution_snapshot_keep_days
        既然写了，就得有人读（专家指出它们此前是死配置）。
        """
        today = _today()
        if self.state.get("last_prune") == today:
            return
        self.state["last_prune"] = today
        self._save_state()
        self.full_snapshot()          # 每天一份全量快照，日级回滚的前提
        try:
            days = int(self.cfg.get("evolution_max_history_days", 180) or 180)
            rows = _read_jsonl(self.events_path)
            if rows:
                cut = _days_ago(days)
                keep = [r for r in rows
                        if not str(r.get("ts") or "")[:10] or str(r.get("ts"))[:10] >= cut]
                if len(keep) != len(rows):
                    _write_jsonl(self.events_path, keep)
        except (OSError, ValueError, TypeError):
            pass
        try:
            days = int(self.cfg.get("evolution_snapshot_keep_days", 30) or 30)
            cut = _days_ago(days)
            if self.snap_dir.is_dir():
                for f in self.snap_dir.glob("*.json"):
                    _m = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
                    stem = _m.group(1) if _m else f.name[:10]
                    if len(stem) == 10 and stem < cut:
                        f.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            pass

    # ── 快照（增量，一天一份） ──────────────────────────────

    def _snapshot(self, path: Path, rows: list[dict]) -> None:
        try:
            self.snap_dir.mkdir(parents=True, exist_ok=True)
            f = self.snap_dir / f"{_today()}.json"
            data = {}
            if f.is_file():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    data = {}
            data.setdefault(path.name, [])
            data[path.name].extend(rows)
            data[path.name] = data[path.name][-20:]
            f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    # ── 注入：取最强的几条 ──────────────────────────────────

    def brief(self, uid: str, limit: int = 2) -> list[dict]:
        """取最该被想起的两条：这个人的信念优先，她对自己的信念最多占一条。

        self 信念是跨用户全局的（专家指出：A 的一次冒犯会对所有人注入），
        所以给它硬性名额：最多 1 条，且排在个人信念之后。

        返回 [{kind, topic, claim, score}]：调用方按 `topic` 分派动作，
        不再靠中文子串——改一个字就静默失效（专家建议 A）。
        """
        if not self.enabled:
            return []

        def _score(rows: list[dict]) -> list[tuple[float, dict]]:
            out = []
            for r in rows:
                s = float(r.get("strength", 0.3)) * _time_factor(r.get("last_confirmed", ""))
                out.append((s, r))
            out.sort(key=lambda x: -x[0])
            return out

        user_rows = [r for r in _read_jsonl(self.beliefs_dir / f"{self._safe_scope(f"user:{uid}").split(":", 1)[1] or "unknown"}.jsonl")
                     if r.get("status") == "accept"]
        self_rows = [r for r in _read_jsonl(self.self_beliefs_path)
                     if r.get("status") == "accept"]

        user_scored = _score(user_rows)
        self_scored = _score(self_rows)[:1]
        take_user = max(0, limit - len(self_scored)) if self_scored else limit
        picked = [_pack(r, s) for s, r in user_scored[:take_user]]
        picked += [_pack(r, s) for s, r in self_scored]
        return picked[:limit]

    # ── 管理面 ──────────────────────────────────────────────

    def status(self) -> dict:
        rows: list[dict] = []
        if self.beliefs_dir.is_dir():
            for f in self.beliefs_dir.glob("*.jsonl"):
                rows.extend(_read_jsonl(f))
        rows += _read_jsonl(self.self_beliefs_path)
        cfg_mode = str((self.cfg or {}).get("evolution_mode") or "")
        return {
            "cfg_mode": cfg_mode,
            "mode": self.mode,
            "paused": bool(self.state.get("paused", False)),
            "beliefs": len([r for r in rows if r.get("status") == "accept"]),
            "hold": len([r for r in rows if r.get("status") == "hold"]),
            "today_candidates": self._daily["candidates"],
            "today_new": self._daily["believed"],
            "by_kind": _count_by(rows, "kind"),
        }

    def set_mode(self, mode: str) -> bool:
        if mode not in ("off", "rule", "daily", "deep"):
            return False
        self.state["evolution_mode"] = mode
        self._save_state()
        return True

    def pause(self) -> None:
        self.state["paused"] = True
        self._save_state()

    def resume(self) -> None:
        self.state["paused"] = False
        self._save_state()

    def review(self, limit: int = 10) -> list[dict]:
        """列出当前信念（按强度降序），供管理员 review / approve / reject。

        旧实现列的是审计事件，管理员点「approve 2」时拿到历史事件，
        没法定位到具体信念，还会把证据重复结算（专家 P1-4）。
        """
        paths = [self.self_beliefs_path]
        if self.beliefs_dir.is_dir():
            paths.extend(sorted(self.beliefs_dir.glob("*.jsonl")))
        out: list[dict] = []
        for p in paths:
            out.extend(_read_jsonl(p))
        out.sort(key=lambda r: -float(r.get("strength", 0) or 0))
        return out[:limit]

    def review_events(self, limit: int = 10) -> list[dict]:
        events = _read_jsonl(self.events_path)[-limit:]
        return list(reversed(events))

    def reset(self, which: str = "beliefs") -> int:
        n = 0
        if which in ("beliefs", "all"):
            for f in list(self.beliefs_dir.glob("*.jsonl")) if self.beliefs_dir.is_dir() else []:
                n += len(_read_jsonl(f))
                _write_jsonl(f, [])
        if which == "all":
            # 「全部」必须包括她对自己的判断——否则最该清的全局边界信念
            # 反而清不掉，管理员却以为清了（专家指出）。
            rows = _read_jsonl(self.self_beliefs_path)
            n += len(rows)
            _write_jsonl(self.self_beliefs_path, [])
            _write_jsonl(self.events_path, [])
        # 清队列与当日计数：否则 reset 后旧素材会被重新归纳成「新」信念，
        # 而 status 仍显示当天候选/新信念数（专家 P2-9）
        try:
            self.clear_pending(None)
        except Exception:
            pass
        self._daily = {"date": _today(), "candidates": 0, "believed": 0}
        self._save_daily()
        self.state["last_reset"] = _today()
        self._save_state()
        return n


def _pack(row: dict, score: float) -> dict:
    """注入用的最小结构：按 kind/topic 分派，claim 只负责显示。"""
    return {
        "kind": row.get("kind") or "",
        "topic": row.get("topic") or "",
        "claim": row.get("claim") or "",
        "score": round(float(score), 3),
    }


def _count_today(rows: list) -> int:
    """今天新建了多少条（日上限用）。"""
    today = _today()
    return sum(1 for r in rows if str(r.get("created_at", ""))[:10] == today)


def _strength(count: float) -> float:
    """证据累积曲线：3 次到 0.63，10 次到 0.85，永远不会到 1。"""
    c = max(0.0, float(count))
    return round(0.30 + 0.70 * (1 - math.exp(-c / 4.0)), 3)


def _time_factor(date_str: str) -> float:
    """越久没被确认，权重越低；半衰 45 天。"""
    try:
        t = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
    except (ValueError, TypeError):
        return 1.0
    days = max(0.0, (_now() - t) / 86400)
    return max(0.15, 0.5 ** (days / DECAY_HALF_LIFE_DAYS))


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or "?")
        out[k] = out.get(k, 0) + 1
    return out


SHARP_WORDS = ("废物", "没用", "垃圾", "滚", "闭嘴", "蠢", "傻", "恶心", "烦死")
# 转述/引用，不是用户在骂她（专家 P2-9：「他说你是废物」不该算她被攻击）
QUOTE_MARKERS = ("他说", "她说", "那句", "原话", "引用", "转述", "说我是", "说我")


def _is_sharp(text: str) -> bool:
    """判断这句是否在攻击她。

    两步：先把引号里的内容抹掉（引用别人的话不算攻击），
    再看去掉引用标记后的剩余部分有没有攻击词。
    """
    body = text or ""
    if not body:
        return False
    bare = re.sub(r"[\u300c\u300e\u201c\u201d\u0027\u2019](.*?)[\u300d\u300f\u201c\u201d\u0027\u2019]", "", body)
    hit = re.search(r"废物|没用|垃圾|滚|闭嘴|蠢|傻|恶心|烦死", bare)
    if not hit:
        return False
    # 引用标记出现在攻击词之前 = 在转述别人的话，不是她在被骂
    marks = [bare.find(m) for m in QUOTE_MARKERS if m in bare]
    if marks and min(marks) < hit.start():
        return False
    return True


def _is_negative(mood: str) -> bool:
    return any(w in (mood or "") for w in ("难受", "生气", "委屈", "烦", "低", "难过", "不好"))


def _is_empty(text: str) -> bool:
    return len((text or "").strip()) <= 1
