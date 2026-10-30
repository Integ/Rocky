# 影响 Rocky 任务完成的因素

> 分析日期：2026-10-29 · 基准提交：[`799116b`](https://github.com/Integ/Rocky/commit/799116b) ·
> 依据：代码阅读和 [三项评测失败调查](2026-10-27-eval-failure-investigation.md) 里的旧评测记录，不是新测的数据。
> 原始文档：<https://claude.ai/artifact/4UAt6kkCxQHhc6WDiTuCnj>

## 之后的变化（截至 2026-10-30，main@8d5881d）

报告指出的几条主要障碍后来都有了对应改动：

- 预算或时限用完后可在原任务上“加倍预算继续”，不再从零开始（[`2f744ab`](https://github.com/Integ/Rocky/commit/2f744ab)）。`budget_exhausted` 和 `deadline_exceeded` 已不是死路。
- 等审批、等补充信息时暂停任务计时（[`fdf77ed`](https://github.com/Integ/Rocky/commit/fdf77ed)）。
- 提交前合同核对页给出 concerns 和预算检查，可调预算和时限（`fdf77ed`），也可直接改验收标准（[`10f420f`](https://github.com/Integ/Rocky/commit/10f420f)）。
- 命令可申请最长 900 秒，更长的工作走后台作业（[`5a86dec`](https://github.com/Integ/Rocky/commit/5a86dec)）。
- 授权文件夹内写入免逐条审批（[`dc8cb47`](https://github.com/Integ/Rocky/commit/dc8cb47)）；需要你时发系统通知（[`ad16cf5`](https://github.com/Integ/Rocky/commit/ad16cf5)）。
- 模型可提出记忆建议，经你确认后写入长期记忆（[`f60b056`](https://github.com/Integ/Rocky/commit/f60b056)）。

下文中的行号对应 `799116b`，部分已经移动。

---

## 一句话结论

一个任务能不能成功，取决于它能否在预算耗尽之前，让模型写出的产物通过“文件检查 + 独立模型审查 + 证据审计”三道闸门。最常见的失败不是闸门本身判错，而是**模型走了太多轮，预算先用光**；其次是**审批等待吃掉时间或连续超时转为 blocked**。

## 按影响排序

### 1. 模型能力和决策质量（影响最大）

整个执行只有一个循环：每一轮由模型选 act / wait / finish / blocked，程序只负责限额、权限和闸门（`src/rocky/workflow.py:918`）。什么时候该停下来要信息、什么时候该写产物、一次批量读多少文件，全靠模型和提示词（`src/rocky/planner.py:95` 起的 `_PLAN_INSTRUCTIONS`）。程序里没有针对具体任务的硬规则。

- missing-info 这种题，能否识别“Q3 目标未批准”并直接 blocked，完全是模型判断；提示词只是引导（`planner.py:135` 附近 blocked 说明）。
- 模型由 `ROCKY_MODEL` 和推理强度决定（`src/rocky/config.py:29`）。换网关后的模型，表现可能完全不同。

### 2. 预算：步数、模型调用、累计 token、墙钟时间

- 默认上限 100 步 / 150 次调用 / 50 万 token / 3600 秒（`src/rocky/models.py:54`）；从目标自动生成合同时，按每次调用 3.2 万 token + 6.4 万启动量估算（`planner.py:1347`）；评测用 40 / 60 / 40 万 / 1200 秒（`src/rocky/evaluation.py:55`）。
- 检查在 `workflow.py:517` 的 `_check_limits`。token 在响应回来后才检查，单次调用可能越线。
- 每一轮都重发合同、状态和记忆，单次请求上限 10 万 token（`planner.py:51`），缓存命中的 token 也计入累计。所以 token 消耗约等于“轮数 × 每轮上下文”，多走 10 轮就多烧几十万。
- 旧评测里 add-feature（403k）和 long-guide（401k）都是预算耗尽，missing-info 也用了 366k、30 步。
- **budget_exhausted 和 deadline_exceeded 是终态，不能恢复**：恢复接口只接受引擎层面的 `failed`（`src/rocky/store.py:604`）。预算不够只能重新提交。

### 3. 完成闸门（finish 时的三道检查）

顺序见 `src/rocky/activities.py:127`：

1. **文件检查**：每个声明的产物文件必须存在、达到 `min_bytes`、包含 `contains` 文本（`src/rocky/tools.py:1173`）。
2. **独立模型审查**：审查者只看真实产物和程序收集的证据，不信规划者自述（`planner.py:1482`，规则在 `_REVIEW_INSTRUCTIONS`）。数字必须能在源文件里原文找到，或来自任务实际跑过的命令输出；手写的汇总数会被拒。
3. **证据审计**：程序再逐条核对审查者引用的出处，有一条对不上就判失败（`src/rocky/evidence.py:485`）。通过后还会复查产物在审查期间没被改动。

影响：

- 审查失败会消耗一次模型调用，并计入“无进展”（`workflow.py:861`），反复修补很快吃预算。
- 审查者看到的产物文本有上限，6 万字符 / 3.6 万 token（`planner.py:89`）。很长的文档会被截断，而提示词规定“被截断的内容不算证据”，长产物更容易被判不通过。
- blocked 不经过这些闸门，所以“缺信息就停”反而便宜。

### 4. 目标合同的写法

- 验收标准最多 20 条、产物必须是工作区里的文件（`models.py:69`）。合同通常由模型先起草（`planner.py:1360`），用户确认后提交。
- 标准写得含糊，审查者既可能放过也可能卡死；标准里要求了拿不到的信息，任务就只能 blocked。
- 代码类任务要求有一份“验证摘要”产物，并且要真的跑测试，证据才过得了审计。

### 5. 审批和超时

- Shell 命令和非 GET 的 HTTP 请求默认逐条审批；简单只读命令自动放行（`src/rocky/approval_policy.py:181`），还可在 `ROCKY_AUTO_APPROVE_COMMANDS` 里加白名单前缀（`config.py:36`）。
- 没人回应 30 分钟后自动拒绝（`tools.py:520`、`config.py:42`）；**连续 3 次超时就转为 blocked**（`workflow.py:69`、`workflow.py:674`）。
- **审批等待期间墙钟照常走**：等待时间被任务截止时间封顶（`workflow.py:572`）。一个 1 小时的任务等两次审批就可能直接 deadline_exceeded。等待期间不调用模型、不耗 token。
- 旧评测的 long-guide 发起了 30 次审批，说明模型用了很多 Shell 命令去写文档，而原生 write/append 不需要审批。

### 6. 无进展检测

- 程序用“动作参数 + 结果指纹”判断是否重复（`workflow.py:797`）。连续 3 轮无进展（`models.py:95` 的 `max_no_progress`）先要求换策略，再无进展就转 blocked 等人（`workflow.py:864`）。
- 规划失败（包括网关不可达）也计入无进展，所以网关一断，约 6 轮后任务停在 blocked，而不是失败。
- 它只能防止原地打转；“每次写点不一样的东西但不收敛”的情况它看不出来，只能靠预算兜底。

### 7. 工具和沙箱限制

- 命令最长 30 秒（`tools.py:908`），更长的要放后台再轮询，会多出好几轮。
- 工具输出最多 16000 字符（`tools.py:61`），大输出要存文件再分段读。
- Seatbelt 只允许写当前任务工作区（`src/rocky/seatbelt.py:151`），网络放开（`seatbelt.py:131`）；读 `~/.ssh`、钥匙串等凭据要 `sandbox=false` 并逐条批准，工作区外的文件读写也要逐条批准（429398e）。
- 每次最多并行 8 个只读动作（`models.py:23`），用不用批量读对轮数影响很大。

### 8. 单次响应上限

每次规划响应最多 16384 token，包含推理 token，高推理强度翻倍（`planner.py:55`、`config.py:44`）。被截断视为不可重试的错误，这一轮白费。长文件必须用 append_file 分段写。

### 9. 上下文和记忆

- 规划时的记忆层上限 2.4 万 token，按交接摘要 / 近期记录 / 检索知识 40/40/20 分配（`src/rocky/memory.py:22`）；状态里只留最近 6 条观察（`workflow.py:256`）。
- 早期读过的内容滚出窗口后，要么靠模型写进交接摘要，要么重读。摘要写得差，会导致重复读取和多走轮数。
- 跨任务长期记忆只有用户显式写入的才会被检索，不会自动积累经验。

### 10. 持久化与恢复（现在基本不再是失败原因）

- 状态写入失败会无限重试，Mac 睡眠或磁盘满只会让任务暂停，不会失败（`workflow.py:59`）。
- 模型调用遇到 429 / 5xx / 断连会重试 3 次（`workflow.py:47`）；读超时和无效响应不重试，反馈给下一轮。
- 引擎层面失败的任务可以从保存的进度恢复，计数延续、墙钟重置（`store.py:604`）。
- 有活动任务时 supervisor 会阻止 Mac 休眠（`src/rocky/supervisor.py:306`）。

## 旧评测失败对应到哪些因素

| 任务 | 记录 | 最可能相关的因素 |
| --- | --- | --- |
| missing-info | 30 步、366k token、9 次审批，未通过 blocked+原因检查 | 1（没有及时识别缺信息）、6（不收敛时检测不到） |
| add-feature | 34 步 / 40 次调用、403k token、14 次审批 | 2（预算耗尽）、3（测试和审查的修补往返） |
| long-guide | 37 步、401k token、30 次审批 | 2、5（大量命令审批）、8（长文需分段） |

这些是候选解释：当时没有保留 rocky.db 和逐步记录，不能证明具体根因。

## 可调的杠杆（不改代码）

- 提交前检查合同：验收标准具体、可验证，不要求拿不到的信息；预算按任务规模给足。
- 把常用命令前缀加入自动批准白名单，减少审批次数；需要长时间无人值守时，把审批超时调长或把墙钟上限放宽。
- 选更强的模型或更高推理强度，通常比放宽闸门更有效。
