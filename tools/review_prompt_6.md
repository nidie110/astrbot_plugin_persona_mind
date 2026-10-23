第 6 轮只读静态复核。项目 D:/qqaibot/astrbot/data/plugins/astrbot_plugin_persona_mind

你第 5 轮的三条我已落地：
1. 【A 配额虚占】新增 `@dataclass CoreEvent(ts, umo, sign, granted, actual)`。
   - `_core_quota` 用 `ev.granted` 累加（饱和事件也占额度）。
   - `_core_share_for` 按 CoreEvent 计数。
   - apply 里 `before = {k: getattr(core,k)}` → `core.apply()` → `actual = 差值`，一起存进事件。
2. 【B scene 分支用原始 clamp】改为 `cap = abs(num_or(clamp, 20.0))`，负 cap 不再翻转正冲量。
3. 【B profiles raw float】profiles.py 的 `I`/`I_today`/`intimacy_gain`/`cap_per_day` 全部过 `num_or`+`clamp100`，`I_today` 也受 cap 约束。

请只回答：
A) CoreEvent 落地有没有算错的地方？（尤其 actual 的差值是否会被 decay/rebase 混入、granted 与 actual 各用在哪、重复折扣计数是否正确）
B) 第 5 轮你提到的「反向大冲量可越过零点超额」（prev=12, v=-40 全放行）现在仍在。这是 bug 还是可接受设计？如果该修，给最小改法。
C) 现在是否可以认为「PAD 数值入口已封闭」？如果否，列出还漏的具体位置（文件:行号）。

40 行内，不要客气。
