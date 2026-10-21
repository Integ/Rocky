# Rocky

**Rocky 是一个长期运行的自主数字代理。** 提交目标、验收标准和执行范围后，Rocky
自主规划下一步、调用工具、核验产物，并在等待、重启和人工反馈之后继续工作。

v0.1 是单用户、单主机执行内核，使用 Python、Temporal、PostgreSQL 和 OpenAI
Responses API。模型通过 `ROCKY_MODEL` 配置，默认 `gpt-4.1-mini`。

## 已实现

- 目标契约：产物、验收标准、工具/域名权限、步骤/模型调用预算和截止时间。
- 持久工作流：执行、定时休眠、阻塞等待输入、唤醒、取消和中断恢复。
- SQL 提交/命令 outbox：API 接受目标后，worker 尚未运行也保留任务。
- 单调版本检查点、工具幂等回执、重复唤醒去重。
- 文件读写、目录查看、指定域名的公开网页文本读取。
- 确定性文件检查和独立模型语义审查，通过核验后才报告成功。
- Bearer 认证 API、命令行客户端、事件记录、Docker Compose 和 CI。

当前版本支持文件与资料任务。浏览器操作、任意脚本执行、跨目标长期记忆、多主机
协作属于后续能力。研究结论仍需要来源证据与适当的人类复核。

## 本地运行

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和
[Temporal CLI](https://docs.temporal.io/cli/setup-cli)。复用已导出的
`OPENAI_API_KEY`，无需将密钥写入文件。

```bash
git clone https://github.com/Integ/Rocky.git
cd Rocky
uv sync --frozen
export ROCKY_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
mkdir -p data
temporal server start-dev --db-filename data/temporal.db
```

保留 Temporal 终端，另开终端运行 `uv run rocky api`，再开一个运行
`uv run rocky worker`。三个进程从同一仓库目录启动；API 和客户端需要相同的
`ROCKY_API_TOKEN`，worker 需要现有 `OPENAI_API_KEY`。本地默认 SQLite；
`ROCKY_DATABASE_URL` 可以指向 PostgreSQL。

在具有相同 API token 的终端提交目标：

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
API 文档在 `http://127.0.0.1:8000/docs`，点击 Authorize 输入 API token。
`/health` 仅表示 API 进程存活，不表示 worker 已连接。

## Mission Control 控制台

API 启动后，在浏览器打开 [Rocky Mission Control](http://127.0.0.1:8000/)。
未连接时显示明确标注 **DEMO** 的本地示例任务；这些示例不表示实际执行记录。
连接后，控制台从同一地址的认证 API 读取真实任务，每三秒刷新一次。

在连接界面输入应用凭证 `ROCKY_API_TOKEN`。这是上文为 Rocky API 设置的 token，
**不要输入 `OPENAI_API_KEY`**；模型密钥仅供 worker 使用。控制台只在当前页面内存中
保留应用 token，不写入 localStorage、sessionStorage 或 URL；刷新页面后需要重新连接。
页面和静态资源不包含服务端凭证或真实任务数据，任务接口仍要求 Bearer 认证。

控制台支持创建目标、查看目标列表与详情、查看事件，以及补充信息唤醒或取消目标。
创建目标时仍需指定产物、验收标准、工具范围和预算；界面操作使用现有 API 的相同
持久化与取消规则。已显示的模型名称来自 API 配置，不能据此判断 worker 是否在线。
在 Docker 或分开配置进程时，请为 API 和 worker 设置相同的 `ROCKY_MODEL`。

界面遵循系统的“减少动态效果”偏好（`prefers-reduced-motion`）。所有脚本、样式和
字体均来自本机或系统，不依赖外部 CDN。公开页面用于加载控制台，认证任务 JSON 和
入口页面使用 `Cache-Control: no-store`，避免保留在浏览器缓存中。

## Docker Compose

需要正在运行的 Docker。保留已导出的 `OPENAI_API_KEY`，设置应用凭证：

```bash
export ROCKY_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ROCKY_POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
docker compose up --build -d
```

Compose 包含 PostgreSQL、持久化 Temporal 开发服务、API 和 worker。API/UI 只绑定
本机回环地址；模型密钥只传给 worker。应用数据、产物/回执和 Temporal 历史分别
保存在命名卷。`docker compose down` 保留卷；加 `-v` 会删除数据。
容器产物可通过 `docker compose cp worker:/app/data/workspaces/GOAL_ID ./artifacts` 导出。

这是本地评估配置。生产部署需要 TLS、Temporal 服务认证、备份恢复与监控。
详情见 [架构说明](docs/architecture.md)。

## 目标示例

```json
{
  "objective": "创建项目说明，解释 Rocky 的持久执行机制",
  "acceptance_criteria": ["说明重启恢复、预算和取消行为"],
  "deliverables": [{"path": "report.md", "min_bytes": 100}],
  "allowed_tools": ["read_file", "write_file", "list_files"],
  "max_steps": 12,
  "max_model_calls": 20,
  "max_output_tokens": 4096,
  "deadline_seconds": 86400
}
```

网络读取必须同时添加 `fetch_url` 和 `allowed_domains` 精确域名。
见 [研究示例](examples/research.json)。重定向不自动跟随，私网地址不可访问。

## 状态与边界

- `queued`：已保存，等待派发。
- `running`：规划、执行或核验。
- `waiting`：定时休眠，可提前唤醒。
- `blocked`：等待补充信息；唤醒继续同一目标，预算不会重置。
- `succeeded` / `failed` / `cancelled` / `budget_exhausted` /
  `deadline_exceeded`：终态，继续工作需提交新目标。

每轮规划消耗一个步骤和一个模型调用预留；完成核验另预留一个模型调用。
失败或结果未知的调用保守计入预算。休眠不调用模型。预算限制调用次数和每次输出
规模，并非精确金额上限。取消阻止后续动作；已发生的写入不会自动撤回，正在进行
的请求仍可能计费。

## 开发与验证

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

测试无需 OpenAI 密钥，不调用真实模型。集成测试启动真实 Temporal 测试服务，首次
运行下载官方服务二进制。Linux CI 设置 `ROCKY_TEMPORAL_TEST_MODE=local`；离线环境
可用 `ROCKY_TEMPORAL_TEST_SERVER=/absolute/path/to/temporal-test-server` 复用时间跳跃服务。
只需单元测试时运行 `uv run pytest -q -m 'not integration'`。

真实模型端到端检查使用现有密钥并产生少量 API 费用，需显式运行：

```bash
uv run python scripts/live_smoke.py --live
```

恢复测试覆盖 worker 更换、等待、取消竞态、派发确认丢失、重复唤醒、工具重试、
预算耗尽、缺失产物阻止完成，以及并发旧快照不能覆盖新终态。
