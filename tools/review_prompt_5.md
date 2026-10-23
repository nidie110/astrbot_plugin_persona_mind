你在做第 5 轮静态复核（只读，不要改文件）。项目：D:/qqaibot/astrbot/data/plugins/astrbot_plugin_persona_mind

你第 4 轮提的问题，本轮已修：
1. main.py `_load_state` 里对 scene dict 执行 `_sc.base = ...` 的 AttributeError —— 整段删除，场景基线由 Mood.effective() 动态取 core.base。
2. main.py 配置基线 `affect_p/ar/d` 只做 float() —— 改为 `clamp100(num_or(cfg.get(...), 默认值))`。
3. `Affect.load()` 里 raw float —— 全部改 `clamp100(num_or(...))`。
4. `load_state()` 的 `_core_ts` / `_last_decay` raw float —— 改 num_or，且 `min(ts, time.time())` 防未来时间冻结衰减。
5. `rebase()` 直接 clamp100 —— 改 `clamp100(num_or(new_base[name], num_or(old, 50)))`，delta 也过 num_or。
6. `effective()` 未终夹 —— 结果过 clamp100（修了 core=0/基线=0/scene=-60 出负数）。
7. `Affect.apply()` 公开入口未过滤 —— 改为：维度白名单 + num_or 拒 NaN/inf/bool + cap 也过 num_or + 零增量不写 ts。
8. `_core_quota` prev=0 不设限 —— 判断从 `prev * v > 0` 改为 `prev * v >= 0`，首次也受 CORE_CAP 约束。

请只回答三件事，不要写长篇：
A) 这 8 处修复本身有没有引入新问题（尤其是 _core_quota 改 >= 后，正常单条消息的冲量会不会被压得过小、反向回归是否仍不受限）。
B) 还有没有仍然绕过 num_or/clamp100 的数值入口（我自查剩：`Mood.decay()` 的 keep 计算、`_halve`、`as_dict` 的 round，请确认这些会不会把 NaN 带出去）。
C) 下一轮最值得做的一件事是什么（在「六意图接入势场项 / 权重标定 / CoreEvent 两量记录 / 发布 1.1.0」里选一个并说理由）。

输出控制在 40 行内。
