# 请复核一处改动：OCC 情绪冲量公式

你是一个严格的代码审查者。请审查下面这处改动，指出问题。不要客套，不要总结我做对的地方。

## 背景

这是一个 QQ 聊天机器人的人格引擎插件（Python，AstrBot 插件）。每条用户消息会先让一个小模型做 OCC 评价，输出四个维度：
- relevance 相关性 0~1（这条消息跟"我"有关吗）
- desirability 有利性 -1~1（对我有利还是不利）
- agency 责任归属 other/self/none
- control 可控性 0~1（我能改变吗）

评价结果决定她产生什么情绪（Anger/Joy/Guilt/Worry/Tender/Annoyed/None），每种情绪对应一个 PAD 冲量向量（如 Anger = P-10, Ar+15, D+5）。
冲量乘以一个 intensity 系数后，叠加到 PAD 情绪状态上。

## 改动前（main.py）

```python
rel = normalize_appraisal_number(thought.get("relevance"), 0.0, 1.0, 0.5)
des = normalize_appraisal_number(thought.get("desirability"), -1.0, 1.0, 0.0)
ctl = normalize_appraisal_number(thought.get("control"), 0.0, 1.0, 0.5)
intensity = 0.55 + 0.45 * min(1.0, abs(des) * rel * (1.0 - 0.5 * ctl))
self.mood.apply({k: v * intensity for k, v in effects.items()}, umo=umo)
```

## 改动后

```python
rel = normalize_appraisal_number(thought.get("relevance"), 0.0, 1.0, 0.5)
des = normalize_appraisal_number(thought.get("desirability"), -1.0, 1.0, 0.0)
ctl = normalize_appraisal_number(thought.get("control"), 0.0, 1.0, 0.5)
gate = 1.0 - 0.3 * ctl if des < 0 else 1.0
intensity = rel * (0.35 + 0.65 * abs(des)) * gate
self.mood.apply({k: v * intensity for k, v in effects.items()}, umo=umo)
```

## 改动理由

旧式有常数项 0.55，导致 relevance=0 时仍有 0.55 的背景冲量——语义上等于"任何消息都会撩到她一点"。
新式让 relevance 成为门槛：不相关 = 不产生情绪波动。

## 请你回答

1. 这个改动在语义上对不对？relevance=0 → intensity=0 是否过头（她会变得"麻木"）？
2. `gate = 1 - 0.3*ctl if des < 0 else 1.0`——只对负向事件做可控性折扣，这个选择合理吗？正向事件为什么不该折扣？
3. 边界情况：rel=0 但 des=±1（模型可能输出矛盾组合）、ctl=1 且负向、浮点精度、effects 全 0 的情况下会怎样？
4. `agency` 在这个公式里完全没参与，这算问题吗？如果算，最小改法是什么？
5. 同时我把一个测试断言从 `assertLess(P, 60.0)` 改成了 `assertEqual(P, 60.0)`（relevance=0 且 Anger 时，期望 P 完全不变）。这个断言改得对吗？它在测什么？
6. 你会怎么改？给出你自己的版本（可以是新公式，也可以是保留旧公式的理由）。

## 只读，不要改文件

请只输出审查意见，不要修改任何文件。
