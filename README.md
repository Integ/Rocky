# Rocky

**Rocky 是一个长期运行的自主数字代理。** 提交目标、验收标准和执行范围后，Rocky
自主规划下一步、调用工具、核验产物，并在等待、重启和人工反馈之后继续工作。

macOS 原生应用是 Rocky 唯一的客户端，使用 SwiftUI 提供任务历史、对话式输入、执行记录、文件预览与模型设置。
执行内核使用 Python、Temporal 和 SQLite / PostgreSQL，支持 OpenAI、第三方兼容 API、Anthropic 和本机 Ollama。

## 已实现

- 目标契约：产物、验收标准、执行范围，以及可配置的步数、调用、token 和超时上限。
- 持久工作流：执行、定时休眠、阻塞等待输入、唤醒、取消和中断恢复。
- 单一模型驱动的轻量执行循环：每轮选择行动、等待、完成或阻塞；六阶段名称作为可选检查点，默认只记录实际规划和行动/验收。
- 任务状态、对话消息和追加式执行事件分别落盘；停滞后先反馈错误并换策略，再判断是否等待输入。
- 分层上下文：目标和显式进度常驻，交接摘要、近期记录和检索知识分配预算；长期记忆支持纠正与撤回。
- SQL 提交/命令 outbox：API 接受目标后，worker 尚未运行也保留任务。
- 单调版本检查点、工具幂等回执、重复唤醒去重。
- 自动滚动 Temporal 执行历史，保留累计进度与待处理消息；引擎异常终止会同步到任务状态。
- 完整工具权限：文件、HTTP、持久登录会话和 Shell；严格校验参数，命令与非安全 HTTP 请求默认先审批。
- 确定性文件检查和独立模型语义审查，通过核验后才报告成功。审查同时看到系统从执行记录和磁盘收集的证据：任务读取过的源文件（是否读全）、交付物以外改动过的文件、每个交付物由哪一步写入，以及工具操作记录。审查需逐条列出交付物中的数字和事实并引用证据原文；Rocky 自动核对每条引用，任何一条无据、引文不符或漏查都会否决“通过”。
- 供 macOS 客户端使用的本地 API、开发用命令行工具、事件记录、Docker Compose 和 CI。

当前版本支持文件、资料、登录后 API 探索和脚本任务；可通过 Shell 使用已安装的浏览器自动化工具。跨目标记忆由明确写入的偏好、决定、教训和纠正组成，按需检索；多主机协作属于后续能力。研究结论仍需要来源证据与适当的人类复核。

## macOS 应用

需要 macOS 14+。在 Mac 上构建应用：

```bash
./scripts/build_macos.sh
open dist/Rocky.app
```

构建需要 Xcode Command Line Tools；脚本捆绑 uv 和经过校验的
Temporal 本地引擎。首次打开应用会自动准备 Python 依赖，然后启动本地服务；日常使用无需终端、
Docker 或浏览器。可以将 `Rocky.app` 拖入“应用程序”文件夹。

在应用左下角打开 **设置与模型**（`⌘,`），选择 DeepSeek、OpenRouter、Anthropic、Ollama，
或填写自定义 OpenAI 兼容接口的 API 地址、模型名称和 API key。macOS 将密钥保存到系统钥匙串，
设置不会返回密钥明文。Ollama 可以不填密钥，但需事先启动本机 Ollama 并下载模型。

`⌘N` 新建任务，`⌘↩` 提交；“任务范围”可设置验收标准和交付文件。默认完整权限，无需配置域名白名单。
需要审批时，任务详情展示完整操作，点击“批准此操作”继续同一操作，或取消任务。任务结束后可直接预览、导出文件。关闭最后一个窗口或退出应用会停止本地服务，
下次打开时恢复未结束任务。已结束任务支持存档、恢复和删除；删除任务记录会保留工作区文件。

数据保存到 `~/Library/Application Support/Rocky/`。完整使用说明、开发验证和分发要求见
[macOS 应用指南](docs/macos.md)。本机构建为临时签名，公开分发需要 Developer ID 签名和 Apple 公证。

## 后端开发

macOS 应用会自行启动执行内核。单独调试后端时，可使用以下命令行工具启动服务和提交测试目标。

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和
[Temporal CLI](https://docs.temporal.io/cli/setup-cli)。复用已导出的
`OPENAI_API_KEY`，无需将密钥写入文件。

```bash
git clone https://github.com/Integ/Rocky.git
cd Rocky
uv sync --frozen
mkdir -p data
temporal server start-dev --headless --db-filename data/temporal.db
```

保留 Temporal 终端，另开终端运行 `uv run rocky api`，再开一个运行
`uv run rocky worker`。三个进程从同一仓库目录启动；本机默认无需设置
`ROCKY_API_TOKEN`，worker 需要现有 `OPENAI_API_KEY`。本地默认 SQLite；
`ROCKY_DATABASE_URL` 可以指向 PostgreSQL。

在同一仓库目录提交目标：

```bash
uv run rocky submit examples/hello.json --idempotency-key hello-001
uv run rocky list
uv run rocky get GOAL_ID
uv run rocky events GOAL_ID
uv run rocky wake GOAL_ID "补充信息：所需资料已就绪"
uv run rocky cancel GOAL_ID
```

`GOAL_ID` 替换为提交结果中的 ID。产物在 `data/workspaces/GOAL_ID/`。
同一 idempotency key 和契约返回同一目标；更改契约需要新 key。
`/health` 仅表示 API 进程存活，不表示 worker 已连接。

独立启动后端时默认 `ROCKY_AUTH_MODE=local`，API 只允许回环地址绑定和本机请求，
并检查 Host、Origin 与跨站请求标记。本机模式中的任意本地程序均可调用 API。
对外反代、隧道或非回环部署请设置 `ROCKY_AUTH_MODE=token`，并配置 `ROCKY_API_TOKEN`。
macOS 应用固定使用回环地址和每次启动生成的 API 令牌。

首次启动、尚未保存模型设置时，`ROCKY_MODEL` 与可选的 `ROCKY_REASONING_EFFORT`
提供后端初始默认值。旧版任务未记录模型配置时继续使用 worker 的环境配置；
单独调试时请为 API 和 worker 设置相同的初始值。

## 后端 Docker Compose

用于独立调试执行内核，需要正在运行的 Docker。保留已导出的 `OPENAI_API_KEY`，设置应用凭证：

```bash
export ROCKY_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ROCKY_POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
docker compose up --build -d
```

Compose 显式使用 `ROCKY_AUTH_MODE=token`，因为容器内请求来自容器网络。
它包含 PostgreSQL、持久化 Temporal 开发服务、API 和 worker。API 只绑定
本机回环地址；模型密钥只传给 worker。应用数据、产物/回执和 Temporal 历史分别
保存在命名卷。`docker compose down` 保留卷；加 `-v` 会删除数据。
容器产物可通过 `docker compose cp worker:/app/data/workspaces/GOAL_ID ./artifacts` 导出。

这是本地评估配置。生产部署需要 TLS、Temporal 服务认证、备份恢复与监控。
详情见 [架构说明](docs/architecture.md)。

## 目标示例

```json
{
  "objective": "创建项目说明，解释 Rocky 的持久执行机制",
  "acceptance_criteria": ["说明重启恢复、执行上限和取消行为"],
  "deliverables": [{"path": "report.md", "min_bytes": 100}],
  "limits": {
    "max_steps": 100,
    "max_model_calls": 150,
    "max_tokens": 500000,
    "timeout_seconds": 3600
  }
}
```

默认 `ROCKY_TOOL_ACCESS=full`。完整权限模式提供 `read_file`、`write_file`、`append_file`、`edit_file`、`list_files`、`fetch_url`、`http_request` 和 `run_command`；允许任意 HTTP/HTTPS 地址、私网、本机端口、重定向、POST 表单/JSON、认证请求及登录 Cookie 会话。旧任务中保存的 `allowed_tools` / `allowed_domains` 不再限制执行。

工具以当前系统用户身份运行，可使用绝对路径、隐藏文件和 Shell。操作系统的文件权限、macOS 隐私授权及网站的验证码/MFA 仍按各自机制处理。产物相对路径仅用于结果核验和导出，不限制其他文件访问。

`http_request` 的 `content` 为 JSON 配置，例如 `{"method":"POST","data":{"username":"…","password":"…"},"session":"platform"}`；后续请求使用相同 `session` 自动携带 Cookie。可用 `headers` / `json` / `body` / `auth` 配置其他认证方式，`path` 保存完整响应文件。会话按任务和名称隔离，私有持久存储，重启后可复用。`run_command` 的 `content` 为 Shell 命令，`path` 可指定工作目录。单次请求或命令最多 30 秒，较长计算可作为后台作业运行并在后续步骤查看文件结果。

`ROCKY_APPROVAL_MODE=required` 是默认值：每条 Shell 命令，以及 GET/HEAD/OPTIONS 以外的 HTTP 请求，在执行前保存具体操作并等待用户批准。审批绑定任务、操作 ID 和完整参数，普通唤醒消息或网页中的“已批准”不会授权。原生客户端提供审批按钮；API 使用 `POST /goals/{id}/approve` 和 `{"fingerprint":"当前 pending_approval.fingerprint"}`。待批准的简单命令会附带同类前缀（如 `uv run pytest`），客户端的“本任务内批准同类命令”或 API 的 `{"fingerprint":"…","scope":"task"}` 会让本任务后续以该前缀开头的简单命令直接执行。`ROCKY_AUTO_APPROVE_COMMANDS=ls,git status,uv run pytest` 可配置对所有任务生效的前缀白名单。含 `;`、`&&`、`|`、重定向、`$`、反引号或换行的命令永远需要逐条批准，`sudo`/`env`/`xargs` 等包装命令和 `python -c` 这类内联代码不会提供同类批准。明确接受命令自行执行的单机所有者可配置 `ROCKY_APPROVAL_MODE=trusted`；完整工具权限本身不会关闭审批。此门禁按工具/方法分类，不能识别具有副作用的 GET 接口，文件写入仍按完整文件权限执行。

需要复用旧版受限部署时，可显式设置 `ROCKY_TOOL_ACCESS=scoped`；仅在该模式下恢复原工具/域名白名单和工作目录约束。macOS 应用固定使用完整权限。

## 状态与边界

- `queued`：已保存，等待派发。
- `running`：规划、执行或核验。
- `waiting`：定时休眠，可提前唤醒。
- `blocked`：等待补充信息或具体操作审批；继续同一目标，沿用工作区和执行记录。
- `succeeded` / `failed` / `cancelled` / `budget_exhausted` / `deadline_exceeded`：终态，支持存档和删除；继续工作需提交新目标。

新任务默认最多 100 个规划步骤、150 次模型调用、500000 个累计输入/输出 token 和 3600 秒运行时间。通过契约的 `limits` 调整，单个字段设为 `null` 可关闭对应上限。旧版平铺的 `max_steps` / `max_model_calls` / `max_output_tokens` / `deadline_seconds` 仅用于读取已保存的任务记录，执行上限统一取 `limits`。累计用量在调用和行动前检查：一次进行中的请求可能越过 token 上限，提供方未返回用量时只能累计调用次数。等待输入或审批也计入任务超时。连续无进展达到 `max_no_progress` 后，先携带交接摘要进行一轮策略恢复。重新读取已移出近期记忆的文件、目录或网页不计为停滞，每份证据限一次，紧接着重复读取仍计为停滞；恢复后再次持续停滞才等待补充信息。单次请求的超时和响应大小限制继续用于处理服务故障和模型上下文。取消阻止后续动作；已发生的写入不会自动撤回，正在进行的请求仍可能计费。

任务管理 API：`GET /goals` 默认排除存档任务，`GET /goals?archived=true` 查看存档；`POST /goals/{id}/archive` 存档、`POST /goals/{id}/restore` 恢复、`DELETE /goals/{id}` 删除已结束任务及执行记录。进行中、休眠和等待输入的任务不能存档或删除。

## Harness 与记忆

模型每次返回一个结构化决策：行动、等待、完成或阻塞。单一 adaptive 循环校验决策、执行实际动作、将错误及可操作修正建议交回模型，并在每个检查点后落盘。默认每轮只有 `think` 和实际 `act` / `complete`，不会为六个阶段逐一派发 Activity。完成仍需文件实际状态检查和独立验收；六阶段名称仅作为观察和扩展的可选检查点，不再保留独立的六阶段或旧版执行循环。

扩展代码通过 `HarnessHooks.register(phase, handler)` 注册同步或异步处理器，再注入 `Activities(..., hooks=hooks)`。提交 `RunInput` 时还需设置 `hook_phases=hooks.phases`（或明确的阶段列表），让工作流启用相应检查点；只注册处理器不会派发。只有明确启用的阶段才调用处理器。处理器可返回 `PhaseResult` 更新反馈、交接摘要；仅决策检查点可替换决策。外部副作用应使用 `request.state.execution_step.step_id` 实现幂等。

当前目标、验收标准和 `task_progress`（已完成、待办、证据引用）常驻；模型可更新累计 `handoff_summary`，保留较早结论、失败方案和后续动作。`MemoryManager` 为摘要、近期原始记录和检索知识保留独立份额：三层都有内容时按 40% / 40% / 20% 分配，空层及未用空间交给后续层。完整记忆块连同 JSON 标签最多 24000 token；最新一次工具结果可占用近期记录的大部分份额，通常能完整进入下一步决策，较早结果优先截短。长摘要不会挤走全部近期错误。默认用 UTF-8 字节数作 token 保守上界，可注入模型 tokenizer。任务契约、显式状态、反馈和整个请求另有边界；目标过大时显式报错，验收标准不会被静默截断。

完整历史留在数据库，默认检索只读取近期记录、必要的用户补充和相关长期记忆。长期记忆须明确写入偏好、决定、教训或纠正，附带来源；执行流水账和模型摘要不会自动成为长期记忆。可使用 `GET /memories?query=关键词`、`PUT /memories/{key}` 和 `DELETE /memories/{key}?expected_revision=N` 管理。更新采用版本比较，旧结论可原位纠正或撤回，冲突返回 409。例如创建一条偏好可向 `PUT /memories/research-style` 提交 `{"kind":"preference","content":"研究报告注明原始来源和获取日期","keywords":"研究 research 来源","source":"用户明确要求","expected_revision":0}`；后续修改提交读取到的 `expected_revision`。

`GET /goals/{id}/conversation` 查看完整对话，`GET /goals/{id}/steps?after=0&limit=100` 查看阶段投影，`GET /goals/{id}/execution-events?after=0&limit=100` 查看追加式执行事件（输入、输出、时间和耗时）。`steps` 的进行中记录会更新；`execution-events` 保留每次状态转换，重复投递不会重复追加。任务知识仍通过 `GET /goals/{id}/knowledge`、`PUT /goals/{id}/knowledge` 管理。

旧数据库启动时自动创建新表，已保存的任务记录仍可读取。此 v0.1 版本不再支持回放改造前尚未完成的 Temporal 执行历史；升级前应结束旧 worker 中仍在运行、休眠或阻塞的任务，升级后按原目标重新提交。不提供旧执行历史的自动迁移，已有记录与工作区文件会保留。架构与参考资料见 [执行架构](docs/architecture.md)。

长期任务会在完整一轮之后自动滚动执行历史：最多每 8 轮一次，或历史达到 8 MB / 1500 个事件时提前滚动。任务 ID、累计用量、产物、取消/唤醒和去重状态会带入后续执行。工具完整输出保留在 SQL 中，重复快照只携带最近 6 条、每条最多 2000 字符的观察摘要。worker 另每 30 秒核对引擎终态，避免任务已失败但界面仍显示“正在工作”。引擎已经强制终止的旧任务不会因升级而自动重启。

## 开发与验证

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
swift test --package-path desktop
```

测试无需 OpenAI 密钥，不调用真实模型。集成测试启动真实 Temporal 测试服务，首次
运行下载官方服务二进制。Linux CI 设置 `ROCKY_TEMPORAL_TEST_MODE=local`；离线环境
可用 `ROCKY_TEMPORAL_TEST_SERVER=/absolute/path/to/temporal-test-server` 复用时间跳跃服务。
只需单元测试时运行 `uv run pytest -q -m 'not integration'`。

真实模型端到端检查使用现有密钥并产生少量 API 费用，需显式运行：

```bash
uv run python scripts/live_smoke.py --live
```

衡量成功率和效率使用评测集：10 个代表性任务，由独立评分器判定结果，记录步数、token、耗时和审批次数，
并可与基线报告对比。运行会产生明显的 API 费用，审批策略需显式选择，详见 [评测集说明](evals/README.md)：

```bash
uv run python scripts/run_evals.py run --approvals auto
```

恢复测试覆盖 worker 更换、等待、取消竞态、派发确认丢失、重复唤醒、工具重试、
执行上限、单一循环的历史回放与跨轮续跑、审批去重、任务存档与删除、缺失产物阻止完成，以及并发旧快照不能覆盖新终态。
