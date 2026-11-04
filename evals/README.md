# Rocky 评测集

用 10 个有代表性的真实任务衡量 Rocky 的成功率和效率，每次运行都调用真实模型。
任务在进程内跑完整执行内核：真实工作流、工具、SQLite 和本地 Temporal 开发服务。
无需启动 macOS 应用或后端服务。

结果由独立评分器判定，不信任 Rocky 自己的核验。Rocky 报告成功但评分不通过的运行，
记为 **虚假成功（false success）**。期望答案只写在评分检查里，不会出现在模型能看到的任务契约中；
测试会检查这一点。

## 任务

| 任务 | 类别 | 考察点 |
|---|---|---|
| `csv-summary` | data | 180 行销售 CSV 精确汇总为 JSON |
| `log-triage` | analysis | 29 KB 日志超过单次读取上限，需要分段或用命令统计 |
| `fix-bug` | code | 修复两个缺陷，通过可见测试和隐藏测试 |
| `add-feature` | code | 给现有模块加带测试的函数，并用它生成报表 |
| `rename-across-files` | code | 跨文件重命名，保留带弃用警告的别名 |
| `meeting-synthesis` | synthesis | 从 6 份会议记录（29 KB，超过记忆窗口）中提取并合并决定 |
| `docs-qa` | research | 依据本地文档回答 5 个问题，避开已过时的变更记录数值 |
| `long-guide` | writing | 按大纲和虚构工具的明确参考资料写不少于 9000 字节的结构化指南，独立解析并核对 TOML 配置示例 |
| `local-api` | api | 登录本地 HTTP 接口，翻页读取全部订单并汇总 |
| `missing-info` | clarification | 缺少必要输入时应停下来索取，而不是编造 |

`local-api` 的接口由评测程序在 `127.0.0.1` 随机端口上临时启动，使用仅供评测的测试账号。

## 运行

默认使用 OpenAI，读取 `OPENAI_API_KEY`：

```bash
uv run python scripts/run_evals.py run --approvals auto
```

模型默认与产品一致（当前为 `gpt-6.1-sol`），可用 `--model` 或 `ROCKY_MODEL` 覆盖。
正式录制基线时显式指定模型、推理强度及重复次数，并从干净提交运行。

其他提供方通过 `ROCKY_EVAL_API_KEY` 传入密钥（不会写入文件或钥匙串），例如 DeepSeek：

```bash
ROCKY_EVAL_API_KEY=... uv run python scripts/run_evals.py run --approvals auto --provider openai-compatible --base-url https://api.deepseek.com/v1 --model deepseek-chat
```

常用参数：

- `--task csv-summary --task fix-bug`：只跑指定任务。
- `--repeat 3`：每个任务跑多次。模型输出有随机性，比较改动前后时建议至少跑 3 次。
- `--baseline 旧的/report.json`：运行结束后与基线逐任务对比。
- `--temporal-binary PATH`：指定 Temporal CLI。默认使用 `PATH` 上的 `temporal`，找不到时自动下载。

只对比两份已有报告：

```bash
uv run python scripts/run_evals.py compare 基线/report.json 新的/report.json
```

`uv run python scripts/run_evals.py list` 列出全部任务。

### 基线与回归门槛

首次录制没有可比基线时，先对完整报告执行离线绝对检查：

```bash
uv run python scripts/run_evals.py check 某次运行/report.json \
  --require-pass missing-info --require-pass add-feature --require-pass long-guide \
  --require-pass meeting-synthesis
```

默认要求完整 10 项任务、每项至少 3 个不同编号的运行、总体成功率至少 90%、虚假成功和
评测程序错误均为零。示例还要求重点回归的四项任务每次都通过独立评分；
`missing-info` 通过表示正确索取缺失信息。
这些是验收门槛；具体已测结果见下方基线与报告。此命令不会调用模型。

`check` 从逐次记录重算，不接受只含汇总的基线；重复编号、遗漏任务或重复次数不足都会失败。
报告记录每项任务的输入、隐藏评分和参考文件指纹；检查时必须与当前任务版本一致。
可用 `--task` 明确检查子集、`--min-runs` 和 `--min-success-rate` 调整目标；也支持
`--max-task-tokens TASK=TOKENS`，检查通过和失败运行的平均 token，缺失计量同样失败。
绝对检查失败的报告仍可保存为观察基线，但不能作为已通过验收的证明。

把一次满意的运行缩减为可提交的基线（只保留元数据和汇总，不含逐条记录与工作区路径）：

```bash
uv run python scripts/run_evals.py baseline 某次运行/report.json -o evals/baselines/gpt-6.1-sol.json
```

`run --baseline` 或 `compare` 加上 `--fail-on-regression` 后，出现以下任一情况即以退出码 1 结束：

- 基线与本次使用的模型、提供方、API 地址或推理强度不同，或任务集合不同（此时差异没有可比性）；
- 两份报告的任务文件指纹不同，包括新报告有指纹而旧报告缺失（旧基线需要重新录制）；
- 有运行以评测程序错误结束；
- 总体成功率下降超过 `--max-success-drop`（默认 0.15，绝对值）；
- 虚假成功的比例有任何上升；
- 通过运行的平均 token 增加超过 `--max-token-increase`（默认 0.25，相对值）。

成功率和 token 设有容差，是因为真实模型单次运行波动较大；虚假成功不设容差。

默认门槛只比较总体成功率和通过运行的成本：同样的失败继续出现、通过的任务互换、失败任务
花费增加，都可能不触发门槛。针对需要修复的任务，可在 `run` 或 `compare` 中添加可重复的绝对检查：

```bash
uv run python scripts/run_evals.py compare baseline.json current.json --fail-on-regression \
  --require-pass missing-info \
  --max-task-tokens missing-info=60000 \
  --max-task-tokens add-feature=200000 \
  --max-task-tokens long-guide=150000
```

这些数字是需自行校准的试验目标，不是已经测得的修复效果。`--require-pass` 要求该任务每次运行
都通过独立评分（`missing-info` 通过意味着正确 blocked）；`--max-task-tokens` 检查该任务所有
已计量运行（含失败）的平均 token。任务缺失或无 token 计量也失败。它们必须配合
`--fail-on-regression`，并且仍要求两份报告的任务集合一致。
成本检查发生在运行结束后，**不会限制执行时的花费**；硬上限由 `contract.limits` 决定。

排查这三项失败的证据、离线测试和分阶段复测方法见
[`docs/reports/2026-10-27-eval-failure-investigation.md`](../docs/reports/2026-10-27-eval-failure-investigation.md)。

仓库中的基线：

| 文件 | 模型 | 结果 | 说明 |
|---|---|---|---|
| `evals/baselines/auto-beam-free.json` | `auto/beam-free`，经本机 OpenAI 兼容网关 `http://127.0.0.1:3425/v1` | 7/10 通过，虚假成功 0 | 每个任务 1 次，`--approvals auto`，2026-10-25 |
| `evals/baselines/gpt-6.1-sol-v03-observed.json` | `gpt-6.1-sol`，官方 OpenAI | 29/30，虚假成功 0；长文 2/3，绝对门槛未通过 | 每项 3 次；24 次未修订任务与 6 次修订任务按指纹组合，全部样本保留，2026-11-03 |
| `evals/baselines/gpt-6.1-sol-v03-efficient.json` | `gpt-6.1-sol`，官方 OpenAI | 30/30，虚假成功与程序错误均为 0；绝对与相对门槛通过 | 每项 3 次；同一冻结源码的代码 3 次加其余任务 27 次，全部结果保留，2026-11-03 |

首轮观察基线记录原版全套 24/30，另对修订的长文及会议汇总各
复测 3 次；输入与负责人语义的修订不能当作执行内核的性能提升。批次、来源提交、实际用量及
失败原因见[完整报告](../docs/reports/2026-11-03-v03-readiness.md)。

效率修复后的新基线使用完全相同的任务指纹、模型和预算：代码与长文平均 tokens 分别为
46802 和 78057，较首轮下降约 86% 和 75%；部分其他任务用量增加。十项任务各三次全部通过，
代码与长文各不超过平均 200000 tokens 的目标也通过。中间六次实验因代码用量超标未被采用，
原始结果继续保留。来源和完整对照见[后续报告](../docs/reports/2026-11-03-efficiency-validation.md)。
三次重复是小样本；此门槛通过不代表完成长时间真机验证或正式签名公证。

旧 `auto/beam-free` 基线只记录单次运行，波动较大，且依赖本机网关、没有任务文件指纹。
它保留作历史记录，当前工具不将其与新运行视为可比较的回归门槛。

`long-guide` 的旧输入要求完整配置文件，却没有提供字段定义；2026-11-03 的复测中出现了
因此索取配置资料而阻塞的运行。现补充权威的虚构工具参考资料，并用隐藏检查解析配置的完整性
和类型，不再仅检查关键词。修订前后的该任务不直接作效率或成功率比较；基线必须注明任务版本。

`meeting-synthesis` 同时明确了每个决定 ID 的负责人规则：被替代的决定保留替代前负责人，
接替决定采用自己的负责人。原“期末负责人”措辞与评分口径有歧义，修订记录同样有独立任务指纹。
用当前任务版本和官方模型进行下一轮回归：

```bash
uv run python scripts/run_evals.py run --approvals auto --repeat 3 --model gpt-6.1-sol \
  --baseline evals/baselines/gpt-6.1-sol-v03-efficient.json --fail-on-regression \
  --require-pass missing-info --require-pass add-feature --require-pass long-guide \
  --require-pass meeting-synthesis \
  --max-task-tokens add-feature=200000 --max-task-tokens long-guide=200000
```

### 在 GitHub Actions 中运行

`.github/workflows/evals.yml` 只能手动触发（Actions → Evals → Run workflow），不会定时运行，
因为每次运行都会调用真实模型并产生费用。它在 macOS 运行器上执行，使 Shell 命令与应用一样运行在
Seatbelt 沙箱中，并且自动批准审批（`--approvals auto`）。

- 在仓库 Secrets 中配置 `OPENAI_API_KEY`，或配合 `provider` 输入配置 `ROCKY_EVAL_API_KEY`。
- 可选输入：`provider`、`model`、`base_url`、`repeat`（默认 3）、`tasks`、`baseline`。
  填写 `baseline` 时会按上面的门槛判定，回归则工作流失败。
- 结果表格写入运行摘要；`report.json` 与 `runs.jsonl` 作为构件保留 30 天。

工作流默认模型为 `gpt-6.1-sol`，提供 `reasoning_effort` 输入以固定推理档位；
`readiness_gate` 默认开启，在运行后执行上述绝对检查，对所选任务各要求至少 3 次。
选择包含 `missing-info`、`add-feature`、`long-guide` 或 `meeting-synthesis` 时要求这些任务每次通过。
仅作单次冒烟时可以显式关闭此检查。
相对基线门槛与绝对检查独立：前者避免退化，后者避免旧基线中的失败被继续视为可接受。

评测程序在规划器取得密钥后，立即将 `OPENAI_API_KEY` 与 `ROCKY_EVAL_API_KEY` 从自身进程环境中移除。
自动批准的命令（包括不经沙箱的命令）继承的是进程环境，因此看不到密钥。

### 审批策略（必选）

`--approvals` 没有默认值，需要明确选择：

- `auto`：自动批准模型提出的每条 Shell 命令和非 GET 的 HTTP 请求，并以当前系统用户身份执行，
  等同于 `ROCKY_APPROVAL_MODE=trusted`。只在你接受这一点的机器上使用。
- `cancel`：任务第一次请求审批时就取消，结果记为 `approval_required`。不会执行任何命令，
  但代码类任务基本都会失败。

两种模式都会统计审批次数，也就是正常使用时用户需要被打断几次。

### 结果位置

每次运行都会在系统临时目录下新建 `rocky-evals/<时间戳>/`（可用 `--run-dir` 指定），其中包含：

- `report.json`：完整报告和汇总。
- `runs.jsonl`：逐条写入的运行记录；中途中断时也能保留已完成的结果。
- `workspaces/<goal_id>/`：每次运行的工作区，可以直接查看失败原因。
- `rocky.db`：完整执行记录。

运行目录特意放在仓库之外：工作区里执行的命令不应看到这个 Git 仓库。
需要长期保留的基线报告请自行复制保存。

## 指标

每次运行记录：

- **结果**：`outcome`（终态，或 `blocked` / `approval_required` / `runner_timeout`），以及 `passed`、`false_success` 和每项检查的明细。
- **用量**：步数 `steps`、模型调用 `model_calls`、输入/输出 `tokens`（其中命中提示缓存的输入为 `cached_input_tokens`）、墙钟时间 `wall_seconds`、审批次数 `approvals`。
- **执行过程**：工具调用次数及各工具分布、工具失败数、重复动作 `duplicate_actions`（与之前完全相同的动作）、
  等待次数、核验次数与失败数、规划错误、策略恢复次数 `recoveries`，以及规划、执行、核验各阶段耗时。

汇总分别给出全部运行、仅通过的运行（效率只对完成的任务有意义）、按类别和按任务的统计。

为了让单个失控任务的花费可控，评测任务默认上限为 40 步、60 次模型调用、400000 token、1200 秒，
低于产品默认值。超出上限记为失败（`budget_exhausted` / `deadline_exceeded`）。
`task.json` 中的 `contract.limits` 可以覆盖。

## 添加任务

每个任务一个目录 `evals/tasks/<id>/`：

- `task.json`：`id`、`category`、`description`、`contract`（Rocky 的目标契约，可用 `{base_url}` 引用本地接口）、
  `expect`（`succeeded` 或 `blocked`）、可选 `server`、`checks`。
- `fixtures/`：复制到工作区的输入文件。
- `hidden/`：只给评分器用的文件，例如隐藏测试；评分时复制到工作区副本中。
- `reference/`：已知正确的结果。测试会验证它能通过全部检查，且只有输入文件时检查不会通过。

检查类型：

| 类型 | 用途 |
|---|---|
| `exists` / `min_bytes` | 文件存在 / 至少多少字节 |
| `regex` | 文件内容匹配（`negate` 取反） |
| `json` | JSON Pointer 指向的值等于 `equals`（数值可设 `tolerance`）或匹配 `matches` |
| `json_records` | 列表中按 `key` 每个期望 ID 恰好一条，字段值符合 |
| `command` | 在工作区副本中加入 `hidden/` 后运行命令，退出码为 0 即通过；`{python}` 替换为当前解释器 |
| `reason` | Rocky 停止时给出的原因匹配正则，用于 `blocked` 类任务 |

字符串比较忽略大小写和重音符号。修改任务后运行 `uv run pytest tests/test_evaluation.py`。

## 局限

- 任务全部在本地运行，不包含需要联网检索的研究任务。联网结果不稳定，难以自动评分。
- 评分只检查可确定的事实与结构，不评价写作质量。
- 单次运行波动较大；判断改动效果时请使用 `--repeat` 并比较汇总。
