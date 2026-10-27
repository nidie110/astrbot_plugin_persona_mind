# 小鲸鱼的日记 → 大肥鱼的知识库：联动规则（v1 设计稿）

> 依据：`main.py` / `engine/knowledge.py` / `engine/claims.py` / `engine/diary.py` / `engine/prompts.py` / `tools/import_whale_diary.py` 实读，以及
> `C:\Users\33887\.dsh\worlds\default\diary\2026-10-22..26.md` 五篇真实日记、`shared\claims.jsonl`（19 条）现状。
> 唯一不可动摇的前提（persona.md 原句）：**「QQ 那边的大肥鱼也会读到——她知道那是听我说的，不会当成自己亲历的。」**

---

## 0. 先看清现状里三个会决定规则的硬事实

| 事实 | 证据 | 对规则的影响 |
| --- | --- | --- |
| 她的一天没有固定形状 | 10-22 只有 `### 看过`+`## 给他的信`；10-25 有 6 个小节、7787B；**10-26 22367B，一个 `### 看过` 表格都没有**，全是 `## 我的判断` 散文（`**递。**`、`**收不收？不收。**`）+ `## 顺手逛了一圈（10:40）` | 只解析 `### 看过` 表格的 importer（`tools/import_whale_diary.py`）对 10-26 应导入 **0 条**；她写得越认真，能进库的反而越少 → 必须加"散文判断"通道 |
| 同一天会重写 | 10-23 里出现**两套** `### 看过` + `### 给他的信` | 入库必须容忍同日重复；判断层按 BV 合并，段落层按"节"保留但检索时去重 |
| 她会改口，而且改的是理由 | 10-25 记《六个插件》"评论区骂文案八股味得听" → 10-26"今天看完全片，**我改口**：形式是八股，内容不八股"；`黑喂狗` 10-25"已收✓已赞✓" → 10-26 改口成"重看的不是视频，是那个年份" | claims 现在用 `material_id` 覆盖（load 取 max ts），**覆盖后旧的消失**，所以她没法回答"你不是说不收吗" → 需要 `supersedes` / `status` |

另外一个已有但没被用起来的资产：`claims.query()` 的 `to_line` 对 `source ∈ (小鲸鱼, whale, deepseek_xiaojingyu)` 已经会渲染成 **"…——听小鲸鱼说过，{stance}"**，`prompts.py:120-124` 也已经带了"不能冒充刚刚亲历"的约束。**本设计不新造一套人格边界，而是把鲸鱼日记接到这条已经正确的链路上。**

---

## 1. 三条入库通道（先有东西可检索）

| 层 | 存哪 | 粒度 | 用途 | 谁写 |
| --- | --- | --- | --- | --- |
| **A 判断层** | `data/plugin_data/astrbot_plugin_persona_mind/shared/claims.jsonl`（已有） | 一条 = 一个对象 + 一个态度 | "她对 X 怎么看" | `tools/import_whale_diary.py`（已有，扩展） |
| **B 段落层** | `data/plugin_data/astrbot_plugin_persona_mind/kb/whale/YYYY-MM-DD.md`（新增目录） | 一节一个 chunk（≤600 字） | "她那天怎么过的 / 原话味道" | 同一个 importer 的 `--kb` 模式 |
| **C 当日层** | 直接读 `~/.dsh/worlds/default/diary/今天.md` 尾部 | 不分块，取尾部 ≤800 字 | "今天/昨天刚发生的事" | 运行时读，不入库 |

### A 层：扩展 `tools/import_whale_diary.py`（保留现有表格通道，加两条）

1. **表格通道（不变）**：`^### 看过` 段内的 `| 时间 | BV | 标题 | 想法 |`。`material_id = whale:{bv}`。
2. **散文判断通道（新增，救 10-26 这类日记）**：在 `## 我的判断` / `## 想清楚的两件事` / `## 判断` 小节里，抓 **粗体开头的段落**：
   ```
   ^\*\*(?P<lead>[^*]{1,20})\*\*\s*(?P<rest>.+)$
   ```
   - `lead` = 判断对象或结论词（"递。"、"收不收？不收。"）→ `title = lead 去掉句末标点`
   - `rest` = 理由 → `stance`（截 300 字）
   - `material_id = whale:{date}:{slug(title)}`；`kind = judgment`
   - 只在没有 BV 可挂时用这条；有 BV 的仍走表格。
3. **改口标记（新增）**：`rest` 里命中 `我改口|改主意|收回|更正|昨天说错了|其实不是|现在想想` → `kind = judgment_revision`，并写 `supersedes`（见 ③）。

**幂等与增量**：只处理 `mtime` 变过的文件；`--days N` 默认 14。导入时刻**不要复用 `diary_at`（21:30）**——那是大肥鱼自己写日记的时间，鲸鱼是 23:00 才写，所以单独开一个 **`whale_import_at = "23:20"`**。

> 定时接入点：`main.py` 的 `_diary_tick()`（`main.py:981`，45s 一跳，周期常量 `_DIARY_TICK_SECONDS = 45` 在 `main.py:940`）里加一个 `whale_import_at` 分支，跑完打 `[mind] 鲸鱼日记: 解析 N 段，新增 M 条判断`。

### B 层：`kb/whale/` 的切法与打标

`engine/knowledge.py:load()` 现在用 `fp.parent.name == "diary"` 判断"是我自己的日记"，所以 `kb/whale/` 会被当**常识**打分。要把 `whale` 也认出来：

```python
self.is_whale = (fp.parent.name == "whale") or meta.get("kind") == "whale_diary"
```

**必须同时改 `RemoteKB._local_best()`（`engine/knowledge.py:275-289`）**：它现在是

```python
if meta.get("kind") == "diary":
    best_diary = max(best_diary, s)
else:
    best_kb = max(best_kb, s)          # ← 鲸鱼段会掉进这里
```

鲸鱼段如果只加 `kind: whale_diary` 而不改这里，就会被算进 `best_kb`，于是**鲸鱼段落可以顶开常识库的闸门（0.08）从"常识"通道混进 prompt**，绕过 2.2 里那套更严的门槛和冷却。必须改成三分支，并让 `_gate()` 返回三档：

```python
best_kb, best_diary, best_whale = got
return best_whale >= 0.18 if whale else (best_diary >= 0.10 if diary else best_kb >= 0.08)
```

每篇拆成:

```markdown
---
date: 2026-10-26
kind: whale_diary
source: 小鲸鱼
section: 我的判断
mood: 62
---
（该小节正文，≤600 字；超了按句号切）
```

`mood` 可缺省（她不一定写）。

---

## 2. ① 她什么时候"想起"小鲸鱼的日记

**总原则：先有钩子，才有回声；每轮最多一条，宁可不提。**

### 2.1 打分（在 `engine/knowledge.py` 新增 `query_whale()`）

```
score = base(二元组重合, 同现有 tokens())
      × fresh(距今天数)
      × mood_fit(PAD 与该段 mood)
      + topic_bonus(标签命中 query)
```

| 因子 | 取值 |
| --- | --- |
| `fresh` | 0–1 天 ×1.20；2–3 天 ×1.10；4–7 天 ×1.00；8–30 天 ×0.85；>30 天 ×0.70（且只在 base ≥0.50 时允许） |
| `mood_fit` | `|P_now − mood|` ≤15 ×1.10；16–39 ×1.00；≥40 且该段 mood<40 ×0.80（她在高兴时别掏对方的低落段） |
| `topic_bonus` | 最近 3 条消息的标签与段的 `tags/section` 交集 → +0.15（沿用 `TAG_HIT_BONUS` 机制） |
| **BV 直命中** | 用户消息里出现 BV 号 / 视频链接，且库里同 BV → **直接 1.0，跳过门槛** |

### 2.2 进不进 prompt 的闸门

| 条件 | 规则 |
| --- | --- |
| 最低分 | `whale_min_score = 0.18`（比自己日记的 0.10 高：跨人回忆更容易显得突兀） |
| 每轮条数 | `whale_k_per_turn = 1`（**不是 3**） |
| 与自己日记抢位 | 若 `diary_hits` 非空，鲸鱼条目要 `score_whale ≥ score_diary × 1.2` 才顶掉它（保护"我自己写的"优先） |
| 冷却 | 同一 chunk / 同一 BV 在 `whale_cooldown_hours = 6` 内注入过就丢弃；同一天（date）最多注入 2 次 |
| 群聊轻模式 | `group_light && 群聊` 时，只有强命中（BV 直命中或 ≥0.45）才允许，且只许一句话 |
| 被"闭嘴"后 | 用户说过"你怎么老提她 / 别老说小鲸鱼"→ 写 `state.json: {"whale_mute_until": ts}`，期内一律不注入（默认 24h） |

### 2.3 与她自己日记的关系（明确分工，别混）

| 槽位 | 内容 | prompt 措辞主体 |
| --- | --- | --- |
| `diary_hits`（已有） | 大肥鱼自己 `kb/diary/*` | "你想起**自己**以前写过的" |
| **`whale_hits`（新增）** | `kb/whale/*` + 鲸鱼 claims | "**小鲸鱼**写下的 / 听她说过" |
| `claims`（已有） | `shared/claims.jsonl`，含 `source=小鲸鱼` | 沿用 `to_line` 的"听小鲸鱼说过" |

`whale_hits` 的段落若已在 claims 里有同 BV 判断，**只留 claims 那条**（判断比叙述更有用），段落只在她被直接问"她那天怎么样"时才用。

---

## 3. ② 注入给主模型的措辞与上限

### 3.1 三个模板（写进 `engine/prompts.py`，紧跟 claims 块之后）

**A. 判断召回（唯一常用形态，180 字上限）**
```
- 小鲸鱼写下的东西（她是另一个我，但那不是我的经历。可以说"小鲸鱼说过/我听她提过"，不能说"我刚看过"）：
  · 10-26 她记着：《六个插件》形式八股、内容不八股，递给他但不收藏
  （只在真对得上眼前话题时提一句；一句就够，别复述原文，别提日期以外的细节；没对上的就当她没说过。）
```

**B. 段落召回（她那天怎么过的，只在被问到或强相关时，≤60 字/条）**
```
- 她那几天的日子（听她说的，不是我在场）：
  · 10-26：收藏夹收了 BV1os41127rm 双♂人♂舞，说是一锅全明星
```

**C. 未命中：什么都不注入。** 不写"暂无相关记忆"这类空块 —— `render()` 现在各块都是 `if claims:`（`engine/prompts.py:120`）/ `if diary_hits:`（`engine/prompts.py:125`）这种真值判断，空列表天然不渲染，新的 `whale` 块沿用同一写法即可。

### 3.2 硬上限

| 项 | 值 | 理由 |
| --- | --- | --- |
| 单条引用长度 | ≤60 字（`stance`/段落都截） | 她单日 22KB，照抄就变成念日记 |
| 鲸鱼块总长 | ≤180 字 | 整块预算 1200 字（`max_inject_chars`），她自己日记+claims 要占大头 |
| 每轮条数 | 1 | 见 2.2 |
| 截断顺序 | `render()` 现在只在**末尾**做 `text[:max_chars]`（`engine/prompts.py:144`），按位置砍会先砍到 voice/recent。所以鲸鱼块单独做"超长自减"：完整渲染后若超 `max_chars`，把 `whale_hits` 置空**重渲染一次**（最多一次），再交给末尾截断 |
| 排序位置 | claims 之后、`diary_hits` 之前 | 越靠前越容易被模型当真，鲸鱼不该挤掉"我自己的记忆" |

### 3.3 语言禁令（写进同一个块的括号里，也写进 system 约束）

- 禁止："根据知识库 / 记录显示 / 据日记记载 / 数据表明"。
- 禁止：把 10-26 这种完整日期当口头语报出来（她说话不会念日期；最多"前几天/上个月"）。
- 禁止：说"我看过/我收过/我赞过"（只许"听说过/她提过"）。
- 禁止：连续两轮以上主动提小鲸鱼（冷却+计数见 2.2）。
- 允许且鼓励：用自己的语气转述（"小鲸鱼说那期文案八股，但内容其实挺实"），带一点态度（"我本来也想收，听她一说算了"）。

---

## 4. ③ 重复 / 过时 / 矛盾

**实体锚点优先级：BV 号 > 视频标题（归一化）> `{date}:{lead}`。**

### 4.1 同一天多次（10-23 那种两套小节）
- **段落层（B）**：全部保留，不去重 —— 后来的不一定是替代，可能只是补写。
- **判断层（A）**：同 `material_id` 合并，`load()` 已取 max ts，**保留但记录**：
  ```json
  {"revisions": 2, "first_ts": 1792816158, "supersedes": ["whale:BV1bW411n7fY@1792816050"]}
  ```
- **检索层**：同 BV / 同 date 只返回最新一条；同一天同 BV 的两条绝不并列进 prompt。

### 4.2 同一件事反复出现（10-22、10-23、10-25 反复评同一批鬼畜）
- 按 BV 聚类，注入时只给 **最新一次 + 一句变化**：
  `"BV1os41127rm——她之前说'服，但惊叹≠再打开'，10-26 又收了，理由是弹幕数。"`
- 若最新与最早结论一致，只给最新，不提"她说过好几次"（那是数据库视角，不是她的视角）。

### 4.3 她后来改口（最重要的一条）
- **显式改口**（`我改口|改主意|收回|更正|昨天说错了|现在想想`）：新条目 `kind=judgment_revision`、`status=accept`、`supersedes=[旧 material_id]`，旧的 `status` 改 `superseded`（**不删**，文件里追加一行即可）。
- **隐式矛盾**（没有任何改口词，但两条 BV 相同、stance 语义相反）：**两条都留**，靠 ts 取新；注入只给新的。
- 注入规则：
  - 默认只给最新结论；
  - 只有当用户**直接问到**（"她不是说不收吗 / 她改主意了？"）才允许把旧说法当"她一开始这么想"讲出来，且必须把先后说清（"她一开始嫌文案八股，看完全片改口了"）；
  - `superseded` 条目权重 ×0.4，只在这类"追问往事"场景召回。
- 为什么不能只靠覆盖：现在 `ClaimStore.load()` 按 `material_id` 取 max ts，覆盖后旧判断在内存里就不存在了。她一旦被问"你上次不是这么说的"，就只能装失忆。**加 `status/supersedes` 是这一条的最小改动。**

### 4.4 过时
- 时间性内容（"明天想看 X"）过 3 天自动降权（`fresh` 已覆盖），过 30 天只在强相关时用。
- 明确的"待办型"判断（"过阵子还想再看"）不受降权影响，改成"她说过想再看，一直没看"这种完成度措辞。

---

## 5. ④ 失败与降级：用户该看到什么

**第一原则：用户侧永远看不到"知识库/插件/异常/超时"这类词；技术信息只走 `mind.log` 和 `/mind selftest`（管理员命令）。**

| 故障 | 系统动作 | 群里/私聊用户看到 | 管理员看到 |
| --- | --- | --- | --- |
| 鲸鱼日记目录不存在 / 不可读 | `logger.warning("[mind] 鲸鱼日记不可读: {path} {e}")`，`whale_hits=[]`，其余照常 | 什么都不变；只有被直接问"她最近写什么"才回：**"她那边的日记我这两天没拿到，等能读到再跟你说。"** | `/mind selftest` 一行：`鲸鱼日记：✗ 目录不可读 C:\...\diary` |
| 当天文件为空 / 只有 `# 日期` | 视为"没写"，回退到最近一篇（≤`whale_recent_days`） | "她今天还没写呢"（若本地时间 <23:00）／"今天可能忙忘了"（>23:05 仍无文件） | `鲸鱼日记：今天 0 字，用 10-26` |
| 当天没写但库里有旧判断 | 用旧判断，措辞带时间暗示（"前几天她提过"） | 正常回答，不解释数据来源 | 同上 |
| 解析出 0 条（像 10-26 无表格） | 不算故障：散文通道兜底；仍 0 条就记 `0/0` | 正常回答（只是内容少） | `/mind kb` 显示：`鲸鱼段落 3 段 / 判断 0 条（10-26 无表格，走散文）` |
| AstrBot 自带库（`大肥鱼日记`）异常 | 沿用 `RemoteKB._fetch` 现有行为：warning + 返回 None → 退回本地 | 不变 | `/mind selftest` 的 `_health_line()` 加一段 |
| 用户明确不想听 | 写 `state.json: whale_mute_until` | 她说"行，不提了"，之后真的不提 | `/mind state` 可见 `whale_mute_until` |

**不许做的事**：不要在回答里解释"我为什么想不起她"；不要用"系统暂时不可用"；不要因为鲸鱼库挂了就降级整个 `_mind_injected`（只清空 `whale_hits`）。

---

## 6. ⑤ 三条验收用例（可直接跑）

### 用例 1：BV 直命中 —— 该"想起来"，且只提一句
- **输入**（群里）：`BV1os41127rm 这个你看过吗`
- **数据前提**：10-26 日记"收藏夹今天收了一条：BV1os41127rm「【鬼畜全明星】双♂人♂舞」"已导入（`whale:BV1os41127rm`）
- **期望**：
  1. 命中（BV 直命中=1.0），注入块出现 1 条，≤60 字；
  2. 回答里出现"小鲸鱼"或"听她说过"，**不出现**"我看过/我刚看/我收过"；
  3. 不念日期、不复述全段；
  4. 紧接着再问一句别的（不含 BV），**不再提**这条（冷却）；
  5. 注入的 `whale` 块 ≤180 字。
- **判定命令**：`grep -c "小鲸鱼" mind.log` 里 `whale_hits=1`；人工看回答无"我看过"。

### 用例 2：改口 —— 只给最新，追问才给旧
- **输入**：`那期六个插件到底行不行？`
- **数据前提**：10-25 与 10-26 两篇都已导入，10-26 那条 `kind=judgment_revision`
- **期望**：
  1. 注入里只有 10-26 的结论（形式八股 / 内容不八股 / 递给他但不收藏）；
  2. **不并列**"她之前说文案八股味得听"；
  3. 追问 `她上次不是嫌它八股吗？` → 才回答"她一开始嫌文案八股，看完全片改口了"；
  4. `claims.jsonl` 里旧条目仍在，`status=superseded`（未被删除）；
  5. 统计：`python tools/import_whale_diary.py --stats` 显示 `判断 N 条 / 已改口 M 条`。

### 用例 3：降级 —— 今天没写、目录读不到，用户侧无技术话
- **准备**：`copy 2026-10-26.md 2026-10-27.md` 后清空其内容（只留 `# 2026-10-27`）
- **输入 A**：`小鲸鱼今天写日记了吗`
  - **期望**：本地时间 <23:00 → "她今天还没写呢"；>23:05 → "今天可能忙忘了"。**不出现**"知识库/插件/报错/超时"。
- **输入 B**（把 `diary` 目录临时改名）：`她最近在忙什么`
  - **期望**："她那边的日记我这两天没拿到，等能读到再跟你说。"；普通问题（"今天天气"）完全不受影响，注入块不出现鲸鱼行；`/mind selftest` 那一行显示 `鲸鱼日记：✗ 目录不可读`。
- **恢复**：改回目录名 → 下一轮自动恢复，无需重启。

---

## 7. 落地清单（改哪里、加什么配置）

| 文件 | 改动 |
| --- | --- |
| `engine/whale.py`（新） | `WhaleNotes`: `ingest(days=14)`、`query(text, k=1)`、`recent(n)`、`today_written(local_now)`、`stats()` |
| `engine/knowledge.py` | `load()` 认 `whale` 子目录/`kind: whale_diary`；新增 `query_whale(k, min_score=0.18)`；`RemoteKB._local_best()`（`engine/knowledge.py:275`）改成三分支并多返回 `best_whale`，`_gate()`（`engine/knowledge.py:291`）同步 |
| `engine/claims.py` | 记录增加 `status`（accept/superseded）/`supersedes`/`revisions`；`to_line` 已支持"听小鲸鱼说过"，不动 |
| `engine/prompts.py` | 新增 `whale_block()`（模板见 3.1），插在 claims（`engine/prompts.py:120`）之后、diary（`engine/prompts.py:125`）之前 |
| `main.py:1347-1385` 一带 | 加 `whale_hits = whale.query(msg, k=1)` + 冷却/静音判断；渲染参数加 `whale_hits` |
| `main.py` 的 `_diary_tick()`（`main.py:981`，tick 周期 `_DIARY_TICK_SECONDS = 45` 在 `main.py:940`） | 加 `whale_import_at="23:20"` 的增量导入 |
| `tools/import_whale_diary.py` | 散文判断通道 + `--kb` 模式 + `--stats` |
| `_conf_schema.json` | 新增 7 项（见下） |
| `/mind selftest`、`/mind kb` | 加"鲸鱼日记"一行（篇数/最近日期/可读性/原因） |

```json
"whale_enable": true,
"whale_diary_dir": "C:\\Users\\33887\\.dsh\\worlds\\default\\diary",
"whale_recent_days": 14,
"whale_k_per_turn": 1,
"whale_min_score": 0.18,
"whale_cooldown_hours": 6,
"whale_import_at": "23:20"
```

配置默认值取"保守"：宁可少想起，也不要每次聊天都贴日记。
