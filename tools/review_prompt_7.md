第 7 轮只读复核。项目 D:/qqaibot/astrbot/data/plugins/astrbot_plugin_persona_mind

你第 6 轮的意见已全部落地：
1. 【A 重复计数】331 行改为仅在 `any(v != 0 for v in core_eff.values())` 时追加 CoreEvent；`actual` 不再 round；历史淘汰改为「先按 CORE_WINDOW 时间保留，再兜底 2000 条」。
2. 【B 反向超额】按你给的最小改法替换为 `target = max(-CORE_CAP, min(CORE_CAP, prev + v)); out[k] = target - prev`。实测 prev=+12、v=-40 → 获准 -24；prev=+12、v=-5 → -5；prev=+12、v=+40 → 0。
3. 【C 剩余入口】main.py:348 decay 参数、main.py:394 field_strength 已改 num_or；Affect 加 `__post_init__`（P/Ar/D + base + ts 全收敛）；`Affect.decay` 参数已收敛；`normalize01` 已收敛。
4. 【profiles】I / I_today / intimacy_gain / cap_per_day 全部 num_or + clamp100，I_today 受 cap 约束。

请只回答两件事：
A) `clamp100` 本身仍是 `max(0.0, min(100.0, v))` 裸 float。它现在被 __post_init__、load、base_from_field、rebase、effective、apply 反复调用，每次入参都已经过 num_or。这种「调用方保证」的模式可以接受，还是应该把 num_or 塞进 clamp100 内部？给判断和理由，不要模棱两可。
B) 现在**是否可以说「PAD 数值入口已封闭（含构造/加载/配额/衰减）」**？如果还差，只列剩下的具体位置（文件:行），不要展开。

20 行内。
