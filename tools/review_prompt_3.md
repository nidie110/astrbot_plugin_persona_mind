# 三轮：按你二轮的意见改完，请复核

你二轮指出 3 个实现问题 + 2 处判断错误。我全部采纳并改了。请复查。

## 1. `value or 50.0` 把真实 0 抹成 50（采纳）

main.py 的势场段原写法：

```python
_A = float(aff_state.get("affection") or 50.0)
```

好感归零、攻击性为 0 都是合法状态，会被 `or` 当成缺失值。

新增 `num_or()`（放在 `engine/affect.py`，与 `clamp100` 同处，可被单测导入）：

```python
def num_or(value, default: float) -> float:
    """只对「缺失/非法」回退默认值；真实的 0 和负数必须原样保留。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return default if num != num else num  # NaN
```

调用处改为 `num_or(aff_state.get("affection"), 50.0)`。

`num_or` 放在 engine 而不是 main，是因为 `main.py` 顶端 `from astrbot.api import logger`，单测里 import 不了。

## 2. `core_eff` 仍遍历原始 effects（采纳）

```python
# 先把输入收敛成「只含 P/Ar/D 的干净字典」：判零、算总额、写历史、算场景，全都用同一份数据。
valid: dict[str, float] = {}
for k, v in effects.items():
    if k not in DIMS:
        continue
    try:
        valid[k] = float(v)
    except (TypeError, ValueError, OverflowError):
        continue
if not any(v != 0.0 for v in valid.values()):
    return
sign = 1 if sum(valid.values()) >= 0 else -1
share = self._core_share_for(umo, sign)
core_eff = self._core_quota({k: v * share for k, v in valid.items()})
self.core.apply(core_eff, clamp=clamp)
...
for k, v in valid.items():   # 场景也用 valid
```

`{"P":1,"X":5}` 不会再把 X 写进额度历史；`{"P":1,"X":"bad"}` 不再抛 TypeError。

## 3. 测试符号假设错（采纳）

原断言用 `sign=-1` 查 share，但 Anger 的维度和是 `-10+15+5=+10`，实际记 `+1`。
改为直接用首次事件的 `CORE_SHARE` 推期望，不依赖内部符号分支：

```python
event = {"P": -10.0, "Ar": 15.0, "D": 5.0}
self.assertAlmostEqual(mood.core.P, 60.0 + event["P"] * mood.CORE_SHARE, places=6)
self.assertAlmostEqual(mood.core.Ar, 45.0 + event["Ar"] * mood.CORE_SHARE, places=6)
```

新增两个测试：`test_mixed_unknown_keys_do_not_pollute_history_or_crash`、`test_num_or_keeps_real_zero`。
26 项全绿。

## 4. 提示词补了中间档锚点（采纳第 5 条）

```
- relevance 相关性(0~1)：这条消息跟她自己的目标、在意的人、当前心情的关联程度，
  不是「话题有没有提到她」。分档参考：
  · 0.0~0.2 与她无关（陌生人在群里聊别的、她完全不关心的领域）
  · 0.3~0.5 有点关系（泛泛的日常，她在旁边听着，可能有一点点感想）
  · 0.6~0.8 跟她有关（她懂的话题、普通程度的诉苦、聊到她在意的人）
  · 0.9~1.0 直接冲着她来（叫她、骂她、夸她、明确求助、她在乎的人出事）
  0 表示她完全没被牵动，不会产生任何情绪。
```

## 5. 关于「旧式偏高」我收回那个说法（采纳第 2 条后半）

你说得对，rebase / clamp 都不能证明普通事件应该减弱。我在代码注释里也改成了「行为改变待验证」，没有再说「旧式偏高」。

## 请你回答

1. `num_or` 里 `isinstance(value, bool)` 排除 bool，是因为 `True` 会被 `float()` 变成 1.0。但势场字段本来就该是数值，如果配置文件里写了 `true`，静默回退 50 好不好？还是应该抛错/记日志？
2. `valid` 构造时对单个键做 `float()` 失败就 `continue`。如果模型输出 `{"P": "10", "Ar": "abc"}`，会得到 `{"P": 10.0}`；如果全是坏值，`valid` 为空 → return。这个「部分容错」会不会掩盖上游的格式问题？应该记录吗？
3. `_core_quota` 在额度耗尽时返回全零 `core_eff`，我仍然把它写进 `_core_hist`（因为你二轮说过「不能直接 return，scene 可能仍需响应」）。这样历史里会有「本次 core 实际没变」的记录，继续参与重复折扣。这个契约对吗？还是应该在写历史时区分「请求量」和「实际变化量」？
4. `decay` 和 `rebase` 是否也应该在 `_core_hist` 里留痕？现在没有，所以「停机三小时后回来」这件事对防刷统计是不可见的。
5. 还有什么地方存在同类的 `x or default` 模式？（我怀疑 profiles / relation 里也有）

只读，不要改文件。
