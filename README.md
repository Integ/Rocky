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
- 完整工具权限：文件、HTTP、持久登录会话和 Shell；严格校验参数，命令与非安全 HTTP 请求默认先审批。分级审批让沙箱内只读命令和所有者规则覆盖的操作直接执行，无人回应的请求超时后拒绝并留待补批，长期运行不会被单条审批卡住。
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
Temporal 本地引擎。首次打开应用会注册 Rocky 后台服务（launchd 用户级 LaunchAgent），
由后台服务自动准备 Python 依赖并常驻运行；日常使用无需终端、Docker 或浏览器。若 macOS 要求批准，
请在“系统设置 > 通用 > 登录项与扩展”中允许 Rocky 在后台运行。可以将 `Rocky.app` 拖入“应用程序”文件夹。

在应用左下角打开 **设置与模型**（`⌘,`），选择 DeepSeek、OpenRouter、Anthropic、Ollama，
或填写自定义 OpenAI 兼容接口的 API 地址、模型名称和 API key。macOS 将密钥保存到系统钥匙串，
设置不会返回密钥明文。Ollama 可以不填密钥，但需事先启动本机 Ollama 并下载模型。

`⌘N` 新建任务，`⌘↩` 提交；验收标准、产物路径和执行预算由 AI 根据目标及任务难度拟定，提交前先给你核对并提示缺少的资料或偏紧的预算，可以返回修改或调大预算和时限。默认完整权限，无需配置域名白名单。
需要审批时，任务详情展示完整操作，点击“批准此操作”继续同一操作，或取消任务。任务需要审批、等你回复或结束时，Rocky 发送系统通知（点通知打开该任务），Dock 图标显示等你处理的任务数，一直等你的任务每 3 小时再提醒一次；关闭窗口后默认留在菜单栏继续提醒，可在设置的“通知”中关闭。任务结束后可直接预览、导出文件。关闭窗口或退出应用不会停止后台服务，
任务继续执行，下次打开应用直接连接；后台服务崩溃后由 launchd 自动重启并恢复未结束任务。有任务运行时，后台服务会阻止 Mac 空闲睡眠，
但合盖或手动睡眠时任务暂停，唤醒后继续。设置中可重启或停止后台服务。已结束任务支持存档、恢复和删除；删除任务会同时删除执行记录和工作区文件（含交付物），只想移出列表请用存档。

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
`uv run rocky worker`。也可用 `uv run rocky serve` 在一个进程中托管 API 和 worker（设置
`ROCKY_TEMPORAL_BIN` 时同时托管 Temporal）；它使用 `data/api-token` 中的持久令牌并强制 token 模式，
任一子进程退出即整体以非零状态退出，供 launchd/systemd 重启。三个进程从同一仓库目录启动；本机默认无需设置
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
macOS 后台服务固定使用回环地址和保存在 `data/api-token`（权限 0600）的持久 API 令牌。

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

默认 `ROCKY_TOOL_ACCESS=full`。完整权限模式提供 `read_file`、`write_file`、`append_file`、`edit_file`、`list_files`、`fetch_url`、`http_request` 和 `run_command`；支持 HTTP/HTTPS 地址、私网、本机服务、重定向、POST 表单/JSON、认证请求及登录 Cookie 会话，下述 Rocky 控制端口除外。旧任务中保存的 `allowed_tools` / `allowed_domains` 不再限制执行。

Python 文件工具可使用绝对路径和隐藏文件，但包括 `full` 模式在内，一律禁止访问工作区根目录中的其他任务和私有 `.rocky/` 控制目录；路径遍历、符号链接和硬链接也不能绕过限制。读取、写入、追加、编辑、目录列表、HTTP 下载目标及验收文件读取都遵守此边界。提交任务时可以授权文件夹（合同的 `folders`，客户端在输入框点文件夹按钮或拖入文件夹）：任务可以像在自己目录里一样直接读写这些文件夹，Seatbelt 也允许沙箱命令写入。授权文件夹必须已存在，不能是根目录、家目录本身，也不能包含或位于 Rocky 自己的数据、代码或凭据存储中。在 `required` 审批模式下，写入、追加、编辑或下载到任务目录和授权文件夹之外的路径（包括经任务内符号链接指向外部的路径）、读取或列出凭据存储（`~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.netrc`、`~/.config/gh`、`~/Library/Keychains`、浏览器配置等），以及读取或列出家目录下任务目录和授权文件夹之外的个人文件，都需要所有者逐次批准完整操作，所有者规则和本任务同类授权都不能放行。家目录之外的系统和工具链文件可直接读取。产物相对路径用于结果核验和导出。

macOS 上的 `run_command` 默认通过 `/usr/bin/sandbox-exec -p <按 goal 生成的 SBPL profile>` 执行。命令只能写当前任务目录，不能读写其他任务或 `.rocky/`；临时文件、Python/uv/pip/npm 缓存位于任务内的 `.rocky-sandbox/`，因此 `uv`、`git`、`python` 可正常创建项目、虚拟环境及缓存。子进程和后台作业继承同一 Seatbelt 限制。上述凭据存储在沙箱内不可读，需要它们的命令（如走 SSH 的 `git push`、`gh`、`aws`）须申请 `sandbox=false`；其他普通文件可读，网络访问继续开放；此机制提供文件及进程隔离，不提供网络隔离或资源配额。非 macOS、缺少 Seatbelt 或系统拒绝加载 profile 时，命令执行失败，不会自动降级为无沙箱执行。

后台服务的 Rocky/Temporal 及常见 API 凭据环境变量不会传给沙箱命令，Python/uv 也不会继承后台服务的虚拟环境配置。配置中的凭据文件、SQLite 数据库及其 WAL/SHM/回滚日志，以及 `rocky serve` 的 API 令牌、Temporal 数据库、`run/` 状态目录和日志目录（`ROCKY_API_TOKEN_FILE`、`ROCKY_TEMPORAL_DB`、`ROCKY_RUN_DIR`、`ROCKY_LOG_DIR`），文件工具和沙箱命令都不能读写；文件工具也不能改写正在运行的 Rocky 代码或 Python 环境。命令和 HTTP 工具（包括重定向）不能连接配置的 Rocky API、Temporal 目标端口，防止任务调用控制接口自行审批。这些端口在所有目标地址上保留，普通外部服务若使用相同端口也会被阻止。

`http_request` 的 `content` 为 JSON 配置，例如 `{"method":"POST","data":{"username":"…","password":"…"},"session":"platform"}`；后续请求使用相同 `session` 自动携带 Cookie。可用 `headers` / `json` / `body` / `auth` 配置其他认证方式，`path` 保存完整响应文件。会话按任务和名称隔离，私有持久存储，重启后可复用。`run_command` 的 `content` 为 Shell 命令，`path` 可指定工作目录。单次 HTTP 请求最多 30 秒。命令默认最多 30 秒，可用 `timeout_seconds` 申请最长 900 秒（构建、安装依赖、测试套件）；命令返回时它留下的子进程会被结束。更长的工作或需要一直运行的服务用 `background: true` 作为后台作业启动：调用立即返回作业 ID，输出写入任务目录的 `.rocky-jobs/<id>.log`，结束后在 `.rocky-jobs/<id>.exit` 写入退出码。每个任务最多同时 4 个后台作业，单个最长 6 小时，任务结束或删除时全部停止。`timeout_seconds` 和 `background` 属于审批内容，改动它们会使原审批失效。

`ROCKY_APPROVAL_MODE=required` 是默认值：每条 Shell 命令，以及 GET/HEAD/OPTIONS 以外的 HTTP 请求，都要先经过分级审批。审批绑定任务、操作 ID 和完整参数，普通唤醒消息或网页中的“已批准”不会授权。依次检查：

1. **只读命令**：在 Seatbelt 内运行的简单只读命令直接执行，包括 `ls`、`cat`、`head`、`tail`、`wc`、`grep`、`rg`、`find`、`stat`、`du`、`diff`、`jq` 等；会写文件或执行其他程序的参数（`find -exec/-delete`、`sort -o`、`tail -f`、`rg --pre` 等）、未加引号的通配符和 `~` 展开仍需批准。程序必须解析到任务工作区之外。参数或工作目录指向家目录下任务目录和授权文件夹之外的个人文件（或包含家目录的目录，如 `/Users`）时，只读命令也需要批准。`git` 不在其中，因为仓库配置（如 `core.fsmonitor`、diff 驱动）可以执行任意程序，而任务能写这些配置。`ROCKY_AUTO_APPROVE_READ_ONLY=false` 或客户端“审批”页可关闭。
2. **所有者规则**：命令前缀（如 `uv run pytest`），或某个精确来源上的 HTTP 方法（如 `POST https://api.example.com`，可限定路径前缀）。规则保存在 Rocky 数据库，命令和文件工具都无法改写；客户端审批卡片的“以后始终允许”、“审批”页手动输入或一键添加常用命令，或 `POST /approval-rules` 添加，`DELETE /approval-rules/{id}` 删除。`ROCKY_AUTO_APPROVE_COMMANDS=ls,git status,uv run pytest` 仍可配置环境变量白名单。
3. **本任务授权**：“本任务内批准同类”或 API 的 `{"fingerprint":"…","scope":"task"}` 让本任务后续同前缀的简单命令，或同方法、同来源的 HTTP 请求直接执行。
4. **等待所有者**：其余操作暂停等待批准。`POST /goals/{id}/approve` 接受 `scope` 为 `action`（默认）、`task`、`always`、`deny`。`GET /approvals` 列出所有未结束任务的待批与超时未批请求，`POST /approvals/batch` 一次处理多条。

无人回应的请求在 `ROCKY_APPROVAL_TIMEOUT_MINUTES`（默认 30，0 表示一直等待，客户端可改）后**只会被拒绝，绝不会自动批准**（选“一直等待”时会一直等到你处理或取消，期间不计时）：操作不执行，模型收到 `approval_timeout` 并改做不需要审批的工作，该请求进入任务的“超时未批”列表。所有者之后用 `scope` 为 `deferred` 批准它，任务会被唤醒，并可原样执行这一操作一次；`dismiss` 将其移除。连续 3 次超时后任务转为等待输入。含 `;`、`&&`、`|`、重定向、`$`、反引号或换行的命令永远不匹配前缀与只读规则，`sudo`/`env`/`xargs` 等包装命令和 `python -c` 这类内联代码不会提供同类批准。执行记录中的 `approved_by` 注明每个免审操作由哪一级放行（`read_only`、`allowlist`、`rule:<id>`、`task`、`deferred:…`、`owner`、`trusted`）。明确接受命令自行执行的单机所有者可配置 `ROCKY_APPROVAL_MODE=trusted`；完整工具权限本身不会关闭审批。此门禁按工具/方法分类，不能识别具有副作用的 GET 接口；带请求体或 URL 超过 2048 字符的 GET/HEAD/OPTIONS 按写入请求审批，以免把文件内容夹带出去。短 GET 仍可携带少量数据，这是保留的风险：个人文件读取需批准正是为此设置。所有者规则放行的命令（如 `uv run pytest`）在沙箱内运行任意项目代码，同样不受个人文件读取限制。任务目录和授权文件夹内的文件写入无需审批。

浏览器自动化或需要写工作区外部文件的特殊命令可设置 `{"tool":"run_command","content":"…","path":"…","sandbox":false}`。这一出口始终需要用户逐次批准完整操作，即使使用 `trusted`、全局前缀白名单或本任务同类授权也不能跳过；审批界面会明确提示以当前用户权限在 Seatbelt 外运行。无沙箱命令不再具有任务文件隔离保证。更改命令、工作目录或 `sandbox` 值都会使原审批失效。

需要复用旧版受限部署时，可显式设置 `ROCKY_TOOL_ACCESS=scoped`；仅在该模式下恢复原工具/域名白名单和工作目录约束。macOS 应用固定使用完整权限。

## 状态与边界

- `queued`：已保存，等待派发。
- `running`：规划、执行或核验。
- `waiting`：定时休眠，可提前唤醒。
- `blocked`：等待补充信息或具体操作审批；继续同一目标，沿用工作区和执行记录。
- `succeeded` / `failed` / `cancelled` / `budget_exhausted` / `deadline_exceeded`：终态，支持存档和删除。`budget_exhausted` 和 `deadline_exceeded` 可用 `POST /goals/{id}/extend`（请求体为新的 `limits`，须超过已用量）或客户端的“加倍预算继续”在原任务上继续：沿用工作区、进度和用量，运行时间重新计算。其余终态继续工作需提交新目标。`failed` 只表示执行引擎意外停止了任务，可用 `POST /goals/{id}/recover` 或客户端的“从保存的进度恢复”继续：沿用已有进度、用量和工作区，运行时间重新计算。数据库暂时不可写（磁盘满、Mac 睡眠）只会推迟任务，不会使其失败。

等待审批时发送消息会撤回这条审批请求，任务按消息继续。审批请求超时后才点批准，会自动按“补批”处理。GET/HEAD/OPTIONS 以外的请求不会跟随重定向到其他来源。

新任务默认最多 100 个规划步骤、150 次模型调用、500000 个累计输入/输出 token 和 3600 秒运行时间。通过契约的 `limits` 调整，单个字段设为 `null` 可关闭对应上限。旧版平铺的 `max_steps` / `max_model_calls` / `max_output_tokens` / `deadline_seconds` 仅用于读取已保存的任务记录，执行上限统一取 `limits`。累计用量在调用和行动前检查：一次进行中的请求可能越过 token 上限，提供方未返回用量时只能累计调用次数。等待补充信息或审批的时间不计入任务时限：时限只约束 Rocky 自己工作的时间，任务等你时不调用模型、不耗 token。连续无进展达到 `max_no_progress` 后，先携带交接摘要进行一轮策略恢复。重新读取已移出近期记忆的文件、目录或网页不计为停滞，每份证据限一次，紧接着重复读取仍计为停滞；恢复后再次持续停滞才等待补充信息。单次请求的超时和响应大小限制继续用于处理服务故障和模型上下文。取消阻止后续动作；已发生的写入不会自动撤回，正在进行的请求仍可能计费。

合同 API：`POST /goal-contract` 由模型根据目标拟定合同，返回 `{"contract": …, "warnings": […]}`，不会创建任务；`warnings` 里 `concern` 是模型指出的问题（无法核验的标准、拿不到的资料等），`model_calls_low` / `token_budget_low` / `time_limit_low` 是预算检查及建议值。确认后把 `contract` 原样提交到 `POST /goals`。`POST /goal-contract/check` 对修改过的合同只做预算检查，不调用模型。

任务管理 API：`GET /goals` 默认排除存档任务，`GET /goals?archived=true` 查看存档；`POST /goals/{id}/archive` 存档、`POST /goals/{id}/restore` 恢复、`DELETE /goals/{id}` 删除已结束任务及执行记录，并删除其工作区、回执和 Cookie 会话。进行中、休眠和等待输入的任务不能存档或删除。

## Harness 与记忆

模型每次返回一个结构化决策：行动、等待、完成或阻塞。单一 adaptive 循环校验决策、执行实际动作、将错误及可操作修正建议交回模型，并在每个检查点后落盘。默认每轮只有 `think` 和实际 `act` / `complete`，不会为六个阶段逐一派发 Activity。完成仍需文件实际状态检查和独立验收；六阶段名称仅作为观察和扩展的可选检查点，不再保留独立的六阶段或旧版执行循环。

扩展代码通过 `HarnessHooks.register(phase, handler)` 注册同步或异步处理器，再注入 `Activities(..., hooks=hooks)`。提交 `RunInput` 时还需设置 `hook_phases=hooks.phases`（或明确的阶段列表），让工作流启用相应检查点；只注册处理器不会派发。只有明确启用的阶段才调用处理器。处理器可返回 `PhaseResult` 更新反馈、交接摘要；仅决策检查点可替换决策。外部副作用应使用 `request.state.execution_step.step_id` 实现幂等。

当前目标、验收标准和 `task_progress`（已完成、待办、证据引用）常驻；模型可更新累计 `handoff_summary`，保留较早结论、失败方案和后续动作。`MemoryManager` 为摘要、近期原始记录和检索知识保留独立份额：三层都有内容时按 40% / 40% / 20% 分配，空层及未用空间交给后续层。完整记忆块连同 JSON 标签最多 24000 token；最新一次工具结果可占用近期记录的大部分份额，通常能完整进入下一步决策，较早结果优先截短。有交接摘要后，较早的成功工具结果改为带来源索引的首尾节选，最新一批结果及错误修复提示优先保留；已被摘要覆盖和仅因窗口滚动省略的结果会分别标记。原始记录仍保存在数据库中，不为压缩额外调用模型。需要源码细节时，`read_file` 可通过 `content` 中的 JSON（例如 `{"start_line":120,"end_line":180}`）按行读取，每次最多 400 行，避免重复读取整个模块。长摘要不会挤走全部近期错误。默认用 UTF-8 字节数作 token 保守上界，可注入模型 tokenizer。任务契约、显式状态、反馈和整个请求另有边界；目标过大时显式报错，验收标准不会被静默截断。

完整历史留在数据库，默认检索只读取近期记录、必要的用户补充和相关长期记忆。长期记忆须明确写入偏好、决定、教训或纠正，附带来源；执行流水账和模型摘要不会自动成为长期记忆。可使用 `GET /memories?query=关键词`、`PUT /memories/{key}` 和 `DELETE /memories/{key}?expected_revision=N` 管理。更新采用版本比较，旧结论可原位纠正或撤回，冲突返回 409。例如创建一条偏好可向 `PUT /memories/research-style` 提交 `{"kind":"preference","content":"研究报告注明原始来源和获取日期","keywords":"研究 research 来源","source":"用户明确要求","expected_revision":0}`；后续修改提交读取到的 `expected_revision`。

`GET /goals/{id}/conversation` 查看完整对话，`GET /goals/{id}/steps?after=0&limit=100` 查看阶段投影，`GET /goals/{id}/execution-events?after=0&limit=100` 查看追加式执行事件（输入、输出、时间和耗时）。`steps` 的进行中记录会更新；`execution-events` 保留每次状态转换，重复投递不会重复追加。任务知识仍通过 `GET /goals/{id}/knowledge`、`PUT /goals/{id}/knowledge` 管理。

`GET /goals/{id}/events?after=0` 返回界面时间线投影，每页最多 100 条；后续请求将 `after` 设为上一页最后的 `event_id`。桌面端在当前选择的任务内保留事件和游标，每 3 秒仅追加新事件，切换任务时重新加载。新阶段的时间线事件仅保存状态、用量、产物、阶段元数据和最新一步的观察摘要（并行批次保留该步所有观察），不重复保存阶段输入/输出或交接摘要；完整状态仍保存在目标记录中，完整工具结果仍在执行记录和消息表中。读取旧事件时也会生成相同的精简投影，但不会重写历史数据；旧版无阶段记录的事件仍保留原始观察供对话和记忆检索。

数据库记录 schema 版本，启动时按顺序执行升级步骤并保留已有数据；旧版 Rocky 拒绝打开被新版升级过的数据库。此 v0.1 版本不再支持回放改造前尚未完成的 Temporal 执行历史；升级前应结束旧 worker 中仍在运行、休眠或阻塞的任务，升级后按原目标重新提交。不提供旧执行历史的自动迁移，已有记录与工作区文件会保留。架构与参考资料见 [执行架构](docs/architecture.md)。

长期任务会在完整一轮之后自动滚动执行历史：最多每 8 轮一次，或历史达到 8 MB / 1500 个事件时提前滚动。任务 ID、累计用量、产物、取消/唤醒和去重状态会带入后续执行。工具完整输出保留在 SQL 中，工作流检查点只携带最近 6 条、每条最多 2000 字符的观察摘要；SQL 时间线投影进一步仅保留最新一步。worker 另每 30 秒核对引擎终态，避免任务已失败但界面仍显示“正在工作”。引擎已经强制终止的旧任务不会因升级而自动重启。

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
