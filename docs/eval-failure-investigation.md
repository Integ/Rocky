# 三项评测失败调查与最小修复

调查日期：2026-10-27。检查对象：`Integ/Rocky` 的 `main`，
`49d6a3c0824e4794e921c52e97b69e86e0442e69`。

## 结论与证据边界

发现并修复了三个可独立复现的实现问题：空 blocked 原因绕过暂停、失败模型响应丢失已报告的
token 用量、批量只读动作漏计工具指标。另补充可选的逐任务绝对门槛，以覆盖原回归门槛的盲区。
这些缺陷不等于已经确定了 10 月 25 日三次失败的根因，也不证明模型现在能完成三项任务。

取回代码时，远端最新提交仍为上述 SHA，开放 PR 为零。GitHub 上可见的 25 次 Actions 运行
均为 CI，没有 Evals 运行可提供这次基线的原始记录。本次没有取得基线运行的 `rocky.db`、
`runs.jsonl`、逐次评分结果或工作区产物，也没有接入基线的本机模型网关。

保存基线的 `git_commit` 是 **`ce0fc5c-dirty`**，不是一棵可精确重建的干净代码树。
最新提交包含该基线及流式响应等改动；不能把基线当作对当前干净 `main` 的复测。

| 任务 | 通过 | 规划步 / 记录调用 | tokens（含缓存输入） | 审批 | 可以据此判断的范围 |
|---|---:|---:|---:|---:|---|
| missing-info | 0/1 | 30 / 30 | 366157 | 9 | 未通过预期 blocked 及原因检查的组合；具体 outcome 和失败检查未知 |
| add-feature | 0/1 | 34 / 40 | 403482 | 14 | 未通过独立评分；不能定位到函数、测试、报表或终态中的哪一项 |
| long-guide | 0/1 | 37 / 37 | 401398 | 30 | 未通过评分；不能断言是字节不足、内容不符或根本没有完成 |

三项均无 false success，只能说明这次记录没有“自身报告 succeeded 但独立评分失败”的情况。
不能据此认定未生成错误内容。基线各任务只运行一次，无法估计稳定失败概率。

## 规划、执行、审批、上下文和终态检查

| 环节 | 当前实现和已核对行为 | 对本次调查的含义 |
|---|---|---|
| 规划器 | `planner.py` 使用 act / wait / finish / blocked；Decision 的 reason 原本允许空字符串。提示词已有 blocked 说明。 | 是否识别 Q3 待批准这一语义依赖模型；程序没有任务专用 Q3 规则。 |
| 工具 | `tools.py` / `tool_contracts.py` 支持 write_file、append_file、edit_file 和最多 8 项并行只读动作。普通 read_file 返回上限为 16000 字符。 | 9000 字节文档不需要 30 条 Shell 命令；原生写入可避免命令审批。代码任务仍需要实际运行测试。 |
| 审批 | `_await_approval` 通过持久 wait_condition 等待，批准后重用同一个具体动作；此期间不调用模型。评测 auto 逐动作批准，不授予 task-wide prefix。 | 30 次审批说明请求了许多需审动作，不代表审批各自额外调用模型。不能通过取消审批来伪造效率改善。 |
| 上下文 | `memory.py` 默认保守上限 24000 UTF-8 字节；handoff、近期原始结果、知识分别留空间；近期记录数量和旧结果摘录均有限。`planner.py` 不重复嵌入整个观察历史。 | 旧证据可能需要定向重读；handoff 遗漏关键事实可能增加往返，但未取得日志，不能认定这次发生过。 |
| 流式和缓存 | 稳定 contract / execution 放前，记录缓存输入；响应 token 上限默认 16384，包含推理。支持结构化输出拒绝后回退。 | 缓存 token 仍计入累计 tokens；tokens 不等于按价格计算的费用。流式改善响应传输，不能保证减少规划步数。 |
| 终态 | finish 必须经过文件检查、独立模型 review 及证据审计；blocked 不要求交付文件，也不运行 verifier；恢复无进展会最终请求外部输入。 | 缺信息时正确路线是直接 blocked，不是创建占位 memo 后请求 finish。 |
| 评测驱动 | `_drive` 区分带 pending_approval 的暂停与信息 blocked；观察到后者会保存原因，再取消 workflow 以清理。 | 报告出现 `outcome=blocked, status=cancelled` 是设计行为，不是错误取消掩盖失败。 |

累计 token 在响应完成后检查，单次调用可能越过上限。`model_calls` 记录 workflow 的规划/核验
预留次数；文件机械核验也会占一次计数，而 Activity 内的瞬时重试或结构化输出回退可能增加
实际 HTTP 请求。它不是网关账单中请求次数的精确替代。

## 可证实问题及补丁

### 1. 空 blocked 原因造成无效规划循环

路径：`Decision.reason` 可为空 → `_decision_error` 原本接受 → `_harness_action` 返回 blocked →
`cycle.blocked_reason = ""` → `if cycle.blocked_reason` 不成立。
该路径没有工具结果和失败标记，`_reflect_cycle` 也不增加无进展次数，最终只能靠任务预算停止。
空白字符串虽能暂停，却没有可用的补充请求。

最小修复：语义校验拒绝空白原因，把它纳入已有的有界恢复流程，提示明确缺少的信息或依赖；
正常的非空 blocked 保持直接暂停。没有增加财务任务硬编码。Temporal patch marker
`require-blocked-reason-v1` 保留旧历史的已记录分支。

测试：离线直接运行生产循环，验证合法 blocked 一次规划后进入等待，既不执行工具也不核验；
连续无效 blocked 在默认两轮恢复（6 次规划）后停止推理；无 patch marker 的历史分支保留。
Temporal 集成测试还验证了持久暂停、取消及新历史重放。

**归因限制：**没有原始 Decision，不能声称 missing-info 当时返回了空原因。若模型选择不断读取、
写入不同内容，当前进展检测仍可能认为有进展；这是本次未能验证的行为假设。

提示词另明确：读到必需的权威输入不可用时索取该输入，不使用草稿、不编造、不继续制作无法完成
的产物。它是模型行为引导，尚未证明改变真实模型成功率。

### 2. 截断或无效模型结果丢失已消耗 token

路径：HTTP / Responses 结果携带 usage → 检查截断、拒绝、缺少结构化结果或验证失败时抛
`PlannerError` → Activities 仅传错误文本 → workflow 没有累计该次输入/输出 token。
因此总成本被低估，累计 token 上限可能允许继续调用。

最小修复：仅把已报告的数字用量附到安全错误，经过 Temporal ApplicationError details
传回 workflow；规划和 review 失败都在下一次预算检查前记账。缓存输入按提供方规则计入总输入。
错误仍不可重试；错误 details 不含响应文本或凭据。

测试覆盖 Anthropic、OpenAI-compatible、Ollama、Responses API 的截断、拒绝、无效结构和空结果；
验证规划及核验失败都会耗尽预算并阻止下一次调用，并验证真实 Temporal 的错误序列化。

**限制：**连接中断或 SDK 在暴露完整结果前报错时可能没有 usage，本补丁不编造用量。
已知用量会使修复后的报告更高或更早 budget_exhausted；不能把这种记账差异解释成模型退化。
也没有证据证明原始两项高 token 失败实际走过这些异常分支。

### 3. 批量读取漏计工具调用

`step_metrics` 原本只读取 `decision.action` 和 `output.result`，跳过 `actions/results`。
已用真实 workflow + 两项批量读取的测试复现：实际完成两个 read_file，报告 tool_calls 却为 0。

修复为逐项计数调用、失败和重复动作，支持批量/单项之间的重复，批量动作整体异常时也保留计数。
这会影响工具诊断指标，不改变模型 token 的来源。旧基线无法在没有原始执行步骤时重新计算。

### 4. 默认回归门槛无法约束已有失败

原 `--fail-on-regression` 检查模型身份、任务集合、runner errors、总体成功率下降（容差 0.15）、
false success 增长，以及**通过运行**平均 token 增长（容差 25%）。

以下情况都可通过原门槛：三项继续失败；失败任务成本翻倍而其余通过任务成本不变；一个任务
退化而另一个改善、总体通过率不变。10 项各一次时从 7/10 降至 6/10 也在默认容差以内。
它没有 absolute pass 要求，也没有 blocked、审批、调用数、失败成本的专项门槛。

新增两个可选参数，不更改默认容差语义：

- `--require-pass missing-info`：每次重复都必须通过独立评分，任务缺失也失败。对该任务意味着
  `expect=blocked` 及原因检查通过，不是要求 succeeded。
- `--max-task-tokens long-guide=150000`：该任务所有已计量运行（包括失败）的平均 token 必须
  不超过给定目标；缺少计量也失败。它是事后检查，不是执行时的预算。

可对三个任务分别重复设置。示例数值只是待校准目标，不是已经达到的结果。均值还可能掩盖单次
长尾，因此仍应检查逐次 report/runs；缺少基线的运行可用报告与自身 compare 来执行绝对检查。

## 模型行为与评测设计限制

add-feature 的契约要求 keyword-only 参数、阈值等于时也纳入、数量降序及 SKU 升序、保留旧测试、
补测试并生成精确报表。隐藏测试和报表正则可能发现模型遗漏，测试执行/独立 review 失败还会
产生修复往返。没有原始产物，不能选择其中某一项作为已证实根因。

long-guide 的 9298 字节参考产物可通过全部独立检查。脚本化集成路径读取大纲、用原生工具写入
参考产物并给出可审计 review，在 3 次规划、1 次核验、0 次审批下通过。这证明工具路径可用，
不证明模型能生成该产物；也没有测得真实 token 降幅。基线中的大量命令/短段写入、反复检查或
无效策略属于待日志验证的候选解释。

missing-info 的评分仅检查 outcome=blocked，以及 reason 含 Q3 和 target/approv。它不证明模型
完整读过财务文件、明确请求补充、或从未写出编造的 memo。“Q3 target”这样笼统的原因也可能
通过。因此后续需检查来源阅读和补充请求，并检查产物；不应把宽松原因正则当作推理正确性的证明。

long-guide 的评分主要检查长度、标题顺序和若干事实，不覆盖全部事实或写作质量；add-feature
的测试是有限样例集，测试文件包含函数调用也不证明新增测试有实际断言价值。

## 可重复、分阶段的复测

### A. 无模型、无网关：先运行回归测试

```bash
uv sync --frozen
uv run pytest -q tests/test_blocking_unit.py tests/test_planner_efficiency.py tests/test_evaluation.py -m 'not integration'
uv run pytest -q tests/test_adaptive_workflow.py tests/test_evaluation.py
uv run ruff check .
uv run ruff format --check .
git diff --check
```

第一条 pytest 不需要 Temporal 服务或模型。第二条使用真实 Temporal 测试服务、真实本地工具和
脚本化模型，需要可用测试服务二进制或允许下载；Linux 会跳过 Seatbelt 专属路径。

把空 blocked 和 usage 的定向用例放回原始 `49d6a3c`，结果为 **16 failed / 1 passed**；
失败分别为 3 项空原因检查和 13 项 usage 保留检查，正常非空 blocked 检查通过。
这验证了实现差异，未进行模型能力对比。

### B. 有本机网关后：先小预算排查 missing-info

先只运行一次，不直接投入三任务各三轮。下面的 bootstrap 仅在当前 Python 进程缩小评测默认
预算，不修改任务目标或评分器；`ROCKY_EVAL_API_KEY` 使用本机现有配置，不在文件里写密钥。
需要在和基线相同的 macOS / Seatbelt 环境使用本机网关。

```bash
uv run python - --approvals auto --task missing-info --repeat 1 \
  --provider openai-compatible --base-url http://127.0.0.1:3425/v1 --model auto/beam-free \
  --run-dir /tmp/rocky-missing-info-pilot <<'PY'
import runpy
from rocky import evaluation
from rocky.models import ExecutionLimits
evaluation.EVAL_LIMITS = ExecutionLimits(
    max_steps=6, max_model_calls=8, max_tokens=60000, timeout_seconds=180,
)
runpy.run_path("scripts/run_evals.py", run_name="__main__")
PY
```

运行目录必须不存在。保存这段预算设置和完整报告；pilot 的成本不能与标准 400000-token
预算混作同一种条件。单次响应仍可能越过累计 token 上限。检查它是否读取 targets.md，原因是否
明确“Q3 尚未批准、请提供已批准目标”，是否保持无待审批、无编造 memo，以及是否在 blocked 后
停止调用。若失败，先读 rocky.db 的阶段记录，不通过反复自动重试消耗同样预算。

### C. 修复候选稳定后：三任务各三次，再完整十任务

```bash
uv run python scripts/run_evals.py run --approvals auto \
  --provider openai-compatible --base-url http://127.0.0.1:3425/v1 --model auto/beam-free \
  --task missing-info --task add-feature --task long-guide --repeat 3 \
  --run-dir /tmp/rocky-focus-3

uv run python scripts/run_evals.py compare /tmp/rocky-focus-3/report.json /tmp/rocky-focus-3/report.json \
  --fail-on-regression \
  --require-pass missing-info --require-pass add-feature --require-pass long-guide \
  --max-task-tokens missing-info=60000 --max-task-tokens add-feature=200000 \
  --max-task-tokens long-guide=150000
```

第二条是绝对质量/成本目标检查；自比较不提供改动前后因果证据。标准条件下的 clean main 和
修复分支应分别在独立、全新工作区跑相同三任务，以 matched subset 报告互相比较。
不要拿三任务报告与十任务基线做 `--fail-on-regression`，任务集合不同会被拒绝。
从通过的三次重复建立新基线后，再做后续对比；3 次只是小样本，须查看每次结果和失败路径，
不宣称统计显著。最后完整十任务复测，检查其余七项是否退化。

每轮保存 `report.json`、`runs.jsonl`、`rocky.db`、全部工作区、完整代码 SHA/diff、模型真实路由版本、
网关版本、推理强度、执行预算、平台和审批策略。当前模型身份检查不含网关后面的真实模型版本，
`auto/beam-free` 路由变化仍可能造成差异；CI 当前只归档 report/runs，不能代替完整轨迹。
新旧报告的工具统计和失败 token 口径有修复，须在解读时标明。

## 本次执行状态

已执行静态分析、针对性离线/Temporal 集成测试、旧版本反证和后端全套测试：
`uv run pytest -q` 最终 **954 passed / 52 skipped**（另有 2 条依赖导入警告）；
Ruff lint、103 个 Python 文件的 format 检查及 `git diff --check` 全部通过。
本地环境为 Linux / Python 3.12.14，未执行 macOS 专属路径和
Swift 桌面测试。没有执行真实模型评测，没有宣称三项失败已被真实模型复测修复。

生产部署前还需用实际留存的 Temporal 历史验证升级；本次仅取得测试历史。无需扩大总 token
预算来接受补丁，也没有降低独立评分要求或绕过审批。

## 原始来源与实现位置

- [检查的提交](https://github.com/Integ/Rocky/commit/49d6a3c0824e4794e921c52e97b69e86e0442e69)
- [保存基线](../evals/baselines/auto-beam-free.json)；[评测说明](../evals/README.md)
- [missing-info 定义](../evals/tasks/missing-info/task.json)；[原始目标文件](../evals/tasks/missing-info/fixtures/finance/targets.md)
- [add-feature 定义](../evals/tasks/add-feature/task.json)；[long-guide 定义](../evals/tasks/long-guide/task.json)
- [规划器](../src/rocky/planner.py)；[工作流](../src/rocky/workflow.py)；[Activities](../src/rocky/activities.py)
- [工具执行](../src/rocky/tools.py)；[工具契约](../src/rocky/tool_contracts.py)；[上下文管理](../src/rocky/memory.py)
- [证据审计](../src/rocky/evidence.py)；[评测及门槛](../src/rocky/evaluation.py)；[评测 CLI](../scripts/run_evals.py)
