# 四轮：按你三轮的意见改完，请复核

你三轮指出最要紧的两点（非有限数值、重复折扣 vs 额度消耗的语义）。第一条我改了，第二条我理解了但想先确认契约再动。请复查。

## 1. `num_or` 改用 `math.isfinite`（采纳）

```python
def num_or(value, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    # 必须挡 NaN 和 ±inf：`n != n` 只能识别 NaN，"1e999" 会变成 inf 一路流进 PAD。
    return num if math.isfinite(num) else default
```

验证：`num_or(float('inf'),50)=50`、`num_or('1e999',50)=50`、`num_or(nan,50)=50`、`num_or(0,50)=0`、`num_or(-3,50)=-3`。

## 2. `valid` 拒绝 bool / NaN / ±inf（采纳，这条是我上一轮的实质遗漏）

```python
valid: dict[str, float] = {}
for k, v in effects.items():
    if k not in DIMS or isinstance(v, bool):
        continue
    try:
        num = float(v)
    except (TypeError, ValueError, OverflowError):
        continue
    if math.isfinite(num):
        valid[k] = num
dropped = len([k for k in effects if k in DIMS]) - len(valid)
if dropped > 0:
    logger.debug(f"[mind] 情绪冲量丢弃 {dropped} 个非法维度: keys={list(effects)}")
```

验证：`apply({'P':nan,'Ar':inf,'D':True})` → core 完全不动、`_core_hist` 为空；
`apply({'P':5.0,'Ar':'x','D':None})` → 只有 P 生效（62.0），历史只记一条。

日志用标准 `logging.getLogger(__name__)`，因为 engine 层不 import astrbot（保证可单测）。

## 3. 我还没改的：重复折扣 vs 额度消耗（想先确认契约）

你说得对：`core_eff` 经过 `_core_quota` 后还会被 `core.apply(clamp=20)` 再截断，所以
**`core_eff` 未必等于实际变化量**。我当前把 `dict(core_eff)` 写进历史，语义是「获准量」。

三种可选契约：

| 方案 | `_core_hist` 记什么 | 重复折扣按什么算 | 额度按什么扣 |
|---|---|---|---|
| A（当前） | 获准量 | 获准量 | 获准量 |
| B | 实际变化量（apply 前后相减） | 实际变化量 | 实际变化量 |
| C | 两者都记 | 实际变化量 | 获准量 |

我倾向 **B**，理由：防刷的目的是「她真的被这条消息推动了多少」，被 clamp 截掉的部分本来就没推动她；
按获准量算，一条被截断的消息仍会被当成"强刺激"，下一轮同样内容就会被过度折扣。

但 B 有个副作用：如果 clamp 截断导致实际变化为 0（比如 P 已经在 100，再 +10），
这条事件会变成"零变化"，按我上一轮加的规则就不能进历史了——导致"饱和状态下的骚扰"不被计入。

所以我倾向 **C**：历史里同时记获准量和实际量，重复折扣用实际量、额度用获准量、
零变化事件仍进历史但标记 `actual=0`（这样饱和骚扰也算一次尝试）。

## 4. 关于 decay / rebase 不进历史（接受你的解释）

你说得对，「停机三小时」应该靠**时间窗口过期**体现，不该插一条 decay 事件。
我确认当前实现：`_core_share_for()` 里的重复判定用 `time.time() - ts < CORE_DUP_WINDOW`（120 秒），
`_core_quota()` 用 `CORE_WINDOW`（600 秒）过滤窗口。所以只要时间戳是绝对时间，停机自然过期。
重启后历史是空的（不持久化），等于重启后防刷重置——这是我接受的取舍，不是 bug。

## 请你回答

1. 上面 A/B/C 三种契约，你选哪个？我的 C 方案里「零变化事件仍进历史并标 actual=0」会不会让 `_core_hist` 的语义变得太杂？
2. 如果选 C，`_core_hist` 的元组结构要从 4 元变 6 元左右，会影响 `_core_share_for` / `_core_quota` 两处读取。有更简单的方式吗？
3. `logger.debug` 在 AstrBot 里未必会写进日志文件（engine 层用的是标准 logging）。要不要把"丢弃了非法维度"这个信息通过返回值/属性抛给 main，由 main 用 `astrbot.api.logger` 记录？
4. 我扫了一遍 `x or default` 模式，除势场那处外只找到 `float(prof.get("I_today") or 0.0)`（`I_today=0` 时回退 0.0，等价）和 `int(cfg.get("summarize_every") or 0)`（同理）。你三轮说「不能仅凭怀疑认定」，我确认过这两处等价、无害。是否同意？
5. 还有没有我漏掉的、能让 NaN/inf/负数进入 PAD 的路径？（比如 `load_state` 读取持久化文件、`rebase` 的基线计算、`base_from_field` 的入参）

只读，不要改文件。
