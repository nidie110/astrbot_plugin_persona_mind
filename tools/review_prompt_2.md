# 二轮：按你的意见改完了，请复核

你上一轮指出了 7 条。我采纳了大部分，其中两条我不打算按原样改，理由写在下面。请判断我改对没有、有没有引入新问题。

## 我改了什么

### 1. 撤掉 control 分支（采纳你的第 3、7 条）

```python
des = normalize_appraisal_number(thought.get("desirability"), -1.0, 1.0, 0.0)
ctl = normalize_appraisal_number(thought.get("control"), 0.0, 1.0, 0.5)
# control 暂不参与：它对 Anger/Worry/Guilt 的语义方向不一致（能控制的过错反而更自责），
# 硬接会在 des=0 处造成约 43% 的强度跳变。等按情绪类别分别校准后再引入。
intensity = rel * (0.35 + 0.65 * abs(des))
impulse = {k: v * intensity for k, v in effects.items()}
if any(v != 0.0 for v in impulse.values()):
    self.mood.apply(impulse, umo=umo)
```

`ctl` 仍然读出来（保留在日志里），但不参与计算。

### 2. 在 Mood.apply 内部丢弃全零事件（采纳你的第 1 条）

```python
def apply(self, effects: dict, umo: str = "", clamp: float = 20.0) -> None:
    vals = [float(v) for k, v in effects.items() if k in DIMS]
    if not any(v != 0.0 for v in vals):
        return
    total = sum(vals)
    sign = 1 if total >= 0 else -1
    ...
```

原来 `sum({P:0,Ar:0,D:0}.values()) = 0` → `sign = +1`，写进 `_core_hist` 后，
同会话同符号的真实情绪会被 `_core_share_for()` 判定为重复事件而打折。

### 3. 提示词里把 relevance 说清楚（采纳你的第 2 条后半）

原来的提示只写「相关性 relevance(0~1)」，模型很可能理解成「话题有没有提到我」。
现在改成：

```
- relevance 相关性(0~1)：这条消息**跟她自己的目标、在意的人、当前心情的关联程度**。
  不是「话题有没有提到我」——别人诉苦、她在意的人出事，都算高度相关；
  只有确实与她无关（比如群里两个陌生人在聊别的）才给 0。
  0 表示她完全没被牵动，不会产生任何情绪。
```

### 4. 测试补强（采纳你的第 6 条）

- 保留 `assertEqual(P, 60.0)`（你确认精确相等合适）
- 新增 `test_zero_impulse_does_not_discount_later_real_emotion`：
  先 apply 全零 → 断言 `_core_hist` 为空、core 未变、场景偏移为 0；
  再 apply 真实 Anger → 断言 core.P 正好等于 `60 - 10*share`（没有被重复折扣）。
- 新增 `test_normalize_preserves_exact_zero`：直接验证 `normalize_appraisal_number(0) == 0`。

24 项测试全绿。

## 两条我没有照原样改

### A. 保留 0.35 常数，不改成纯 `rel * abs(des)`

你说「若契约是中性评价绝不能产生情绪，这个常数也该去掉」。我保留它，理由：

`0.35` 的语义不是「desirability=0 时的背景冲量」，而是**当小模型已经判定产生了某种情绪（emotion != None），就赋予一个基础强度**。
如果模型说「她心软了（Tender）」但 desirability 恰好是 0，纯乘法会得出强度 0 —— 即「判定她心软，但心软完全不发生」，这是自相矛盾的。
真正的背景冲量问题出在旧式的 0.55 常数项（它作用于**所有**消息，包括 emotion=None），已随新公式消失。

如果你认为这个理由不成立，请直说，我改。

### B. 一般事件强度下降 45%，我暂时接受（你的第 2 条前半）

`rel=0.5, des=-0.5, ctl=0.5`：旧 0.634 → 新 0.287。

我没有加补偿系数，理由是：旧式的绝对强度本来就偏高（0.55 底 + 0.45 上限，最小值 0.55，最大值 1.0）。
而且现在有势场 rebase（基线随关系变）和 PAD clamp，绝对幅度的意义变了。
但这确实是**行为改变**，需要真人对话验证是否「变迟钝」。我准备在标定集里放几条这类消息，看人工标注的期望强度。

## 请你回答

1. A 那条理由成立吗？「模型已判定有情绪 → 给基础强度」这个契约有问题吗？
2. B 里我把「幅度变小」归因为「旧式偏高」是否在为改动找借口？如果是，判断依据应该是什么？
3. `Mood.apply` 里我用 `any(v != 0.0 for v in vals)`，`vals` 只取 DIMS 内的键。如果 effects 里全是未知键（比如模型输出 `{"X": 5}`），`vals` 为空 → `any([])` 为 False → 直接 return。这个行为对吗？
4. 还有没有别的「零值被当成有效事件」的地方，比如 `_core_quota`、`decay`、`rebase`？
5. 提示词改动会不会让模型普遍给更高的 relevance，导致整体情绪变强？

只读，不要改文件。
