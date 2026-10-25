"""读隔壁「情绪管理」插件（astrbot_plugin_affection）的数值。

为什么读文件而不是 import 它：
两个插件是各自的仓库，互相 import 会把部署绑死——对方改个函数签名，
这边就起不来。这里只读 JSON，读不到就静默返回空字典，主流程照常跑。

用途：
- current_aggression_other（0~50）→ 决定她怼人时是玩笑还是锋利
- affection（0~100）→ 诊断展示，让她自己知道"我现在对他是什么态度"
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# 文件变化不频繁，30 秒读一次就够，避免每条消息都碰磁盘
CACHE_TTL = 30.0


class AffectionBridge:
    def __init__(self, root: Path | str | None, enabled: bool = True):
        self.root = Path(root) if root else None
        self.enabled = bool(enabled)
        self._cache: dict[str, dict] = {}
        self._ts: float = 0.0
        self._warned = False

    # ---------- 内部 ----------
    def _scan(self) -> dict[str, dict]:
        """扫描 affection 数据目录，返回 {bot_uid: {user_uid: 状态}}。"""
        out: dict[str, dict] = {}
        if not self.root or not self.root.is_dir():
            if not self._warned:
                self._warned = True
            return out
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            f = d / "user_data.json"
            if not f.is_file():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                out[d.name] = data
        return out

    # ---------- 对外 ----------
    def get(self, uid: str, bot_id: str | None = None) -> dict:
        """取某个用户的情感状态；取不到返回空字典。

        bot_id 给定时只查该机器人账号目录，避免多账号下取到别人的关系值。
        未给 bot_id 时：只有一个账号目录才回退查询，多个账号直接返回空
        （宁可没有数据，也不要张冠李戴）。
        """
        if not self.enabled:
            return {}
        now = time.time()
        if now - self._ts > CACHE_TTL:
            self._cache = self._scan()
            self._ts = now
        key = str(uid)
        item = None
        if bot_id:
            users = self._cache.get(str(bot_id))
            if isinstance(users, dict):
                item = users.get(key)
        elif len(self._cache) == 1:
            users = next(iter(self._cache.values()))
            if isinstance(users, dict):
                item = users.get(key)
        if isinstance(item, dict):
            return {
                "aggression_other": item.get("current_aggression_other"),
                "libido_other": item.get("current_libido_other"),
                "affection": item.get("affection"),
                "turns": item.get("turn_count"),
            }
        return {}

    def available(self) -> bool:
        """数据目录存在且至少读到过一个 bot 目录。"""
        if not self.root or not self.root.is_dir():
            return False
        if time.time() - self._ts > CACHE_TTL:
            self._cache = self._scan()
            self._ts = time.time()
        return bool(self._cache)
