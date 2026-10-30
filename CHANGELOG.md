# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。版本号只在
`src/rocky/__init__.py` 和 `desktop/Info.plist` 两处定义，测试会核对两者一致。
仓库目前还没有打过 tag，下面按提交整理。

## [0.2.0] - 未发布

当前 main 的版本。macOS 原生客户端取代网页控制台，后端改为常驻后台服务，
并补齐长期无人值守运行所需的审批、恢复、通知和运维能力。尚未在真机上完整验证。

### 新增

- **macOS 原生客户端**（[`517d785`](https://github.com/Integ/Rocky/commit/517d785)）：SwiftUI 窗口提供任务历史、对话式输入、执行记录、产物预览和模型设置，模型密钥存在钥匙串。后改为原生 `NavigationSplitView` 侧栏、统一工具栏、Inspector，macOS 26 上输入框使用 Liquid Glass（[`77b8851`](https://github.com/Integ/Rocky/commit/77b8851)）。
- **单一自适应执行循环**（`517d785`、[`19a1e6a`](https://github.com/Integ/Rocky/commit/19a1e6a)）：模型每轮选择行动、等待、完成或阻塞；去掉旧版循环和六阶段循环。
- **评测集**（[`be85746`](https://github.com/Integ/Rocky/commit/be85746)）：10 个代表性任务和独立评分器；审查需逐条引用证据，由程序核对引用。回归门槛见 [`49d6a3c`](https://github.com/Integ/Rocky/commit/49d6a3c)。
- **并行只读动作**（[`97e36b0`](https://github.com/Integ/Rocky/commit/97e36b0)）：一次决策最多 8 个独立只读操作并发执行；新增 `append_file`、`edit_file`。
- **Seatbelt 命令沙箱**（[`4b14fef`](https://github.com/Integ/Rocky/commit/4b14fef)）：`run_command` 只能写当前任务目录。
- **自动拟定合同与预算**（[`d1f22af`](https://github.com/Integ/Rocky/commit/d1f22af)）：根据目标生成验收标准、产物和预算。
- **常驻后台服务**（[`0641840`](https://github.com/Integ/Rocky/commit/0641840)）：`rocky serve` 托管 Temporal、API 和 worker，经 `SMAppService` 注册为 LaunchAgent，持久 API 令牌，关闭窗口任务继续执行。
- **分级审批**（[`262f9b4`](https://github.com/Integ/Rocky/commit/262f9b4)、[`7556678`](https://github.com/Integ/Rocky/commit/7556678)）：沙箱内只读命令直接执行、所有者规则、本任务授权、批量处理；无人回应 30 分钟后拒绝（不会自动批准），可补批。
- **提交前合同核对**（[`fdf77ed`](https://github.com/Integ/Rocky/commit/fdf77ed)）：模型指出问题并检查预算，可调到 2 倍或 4 倍、时限最多 8 小时或不限；`POST /goal-contract`、`/goal-contract/check`。审批页可手动添加命令规则。
- **系统通知**（[`ad16cf5`](https://github.com/Integ/Rocky/commit/ad16cf5)）：需要审批、等你回复或结束时通知，Dock 角标，默认关窗口后留在菜单栏，等你的任务每 3 小时再提醒。
- **长命令和后台作业**（[`5a86dec`](https://github.com/Integ/Rocky/commit/5a86dec)、[`d3fc78f`](https://github.com/Integ/Rocky/commit/d3fc78f)）：`timeout_seconds` 最长 900 秒；`background: true` 启动托管后台作业，任务结束时统一停止。
- **授权文件夹**（[`dc8cb47`](https://github.com/Integ/Rocky/commit/dc8cb47)、[`3767d89`](https://github.com/Integ/Rocky/commit/3767d89)）：合同 `folders` 内可直接读写；附加参考文件（合同 `files`，只读，[`dcb5450`](https://github.com/Integ/Rocky/commit/dcb5450)）。
- **在原任务上继续**：预算或时限用完后 `POST /goals/{id}/extend`（[`2f744ab`](https://github.com/Integ/Rocky/commit/2f744ab)）；已结束任务可追加要求 `POST /goals/{id}/follow-up`（[`2927660`](https://github.com/Integ/Rocky/commit/2927660)）；失败任务从保存的进度恢复 `POST /goals/{id}/recover`（[`799116b`](https://github.com/Integ/Rocky/commit/799116b)）。
- **记忆建议**（[`f60b056`](https://github.com/Integ/Rocky/commit/f60b056)）：任务完成或停下时模型最多提出三条，所有者确认后才写入；设置里可管理长期记忆。
- **离线启动**（[`5883210`](https://github.com/Integ/Rocky/commit/5883210)）：应用内置 Python 和锁定依赖，首次打开不需要网络。
- **浏览器工具 `browse`**（[`8d5881d`](https://github.com/Integ/Rocky/commit/8d5881d)）：用本机 Chrome 打开需要 JavaScript 的页面，只读打开与 GET 同级，交互步骤逐次批准。
- **长期目标与定时任务（议程）**：大目标由 AI 拆成步骤（`POST /plans/draft`），所有者核对后开始（`POST /plans`）；每一步作为独立任务依次执行，开始前自动拟定合同，前面步骤的交付文件复制到后面步骤的 `inputs/step-N/`；每完成一步，模型可调整剩余步骤或判断目标已达成。某步没完成时计划停下等所有者重试、跳过，或在原任务上继续后自动接上。定时任务（`/schedules`）按一次、间隔、每天、每周（本地时区）启动同一份合同；错过的多次只补一次，上次未结束则跳过。worker 内的议程循环每 10 秒推进，同时运行的计划步骤默认 1 个（`ROCKY_AGENDA_CONCURRENCY`）。客户端侧栏新增“议程”，任务工具栏新增“定时重复”。
- **运维**（`8d5881d`）：`GET /status`、`GET /metrics`（Prometheus）、每日备份与 `rocky restore`、日志按大小轮转、`rocky rotate-token`。

### 变更

- 模型调用改为流式，静默 300 秒或总计 900 秒才超时；长期记忆按 BM25 排序；SQLite 用 WAL；`GET /goals` 支持 ETag（`49d6a3c`）。
- 预算用完重试沿用原合同，每个有限上限翻倍（[`ce0fc5c`](https://github.com/Integ/Rocky/commit/ce0fc5c)），后改为在原任务上继续（`2f744ab`）。
- 等审批或补充信息时暂停任务计时，`blocked` 任务不再自行超时（`fdf77ed`）。
- 模型调用走独立的 `<queue>-model` 任务队列，不再挤占工具执行和状态保存的名额（[`2bc2693`](https://github.com/Integ/Rocky/commit/2bc2693)、[`3e0eac8`](https://github.com/Integ/Rocky/commit/3e0eac8)）。
- 默认模型改为 `gpt-6.1-sol`，预设更新到 Claude 5.5 一代（[`5bfab91`](https://github.com/Integ/Rocky/commit/5bfab91)）。
- 系统原因和结果总结以中文显示；核对页可直接编辑验收标准和交付文件并显示起草用量；侧栏列出所有未完成任务，搜索查全部任务（[`10f420f`](https://github.com/Integ/Rocky/commit/10f420f)）。
- 执行记录缓存并限制 1000 条，窗口不在前台时 15 秒轮询；macOS 26 CI 也跑 `swift test`（`dcb5450`）。
- 后端版本统一为 0.2.0（[`947a352`](https://github.com/Integ/Rocky/commit/947a352)）。

### 安全

- 完整权限下，写到任务目录外、读取凭据存储都需要逐次批准；审批卡片红标沙箱外命令（[`429398e`](https://github.com/Integ/Rocky/commit/429398e)）。
- 读取家目录下任务目录和授权文件夹之外的个人文件需要批准；带请求体或 URL 超过 2048 字符的 GET 按写入审批（`dc8cb47`）。
- 修改类请求不跟随跨源重定向；控制面文件（API 令牌、Temporal 库、`run/`、日志）对工具不可见（`799116b`）。

### 修复

- 空 blocked 原因造成的无效规划循环；失败模型调用的用量计入（[`da97713`](https://github.com/Integ/Rocky/commit/da97713)）。
- 数据库暂时不可写只暂停任务，不再让工作流永久失败；等审批时发消息会撤回请求；删除任务同时删除工作区、回执和 Cookie；数据库加入 schema 版本和迁移（`799116b`）。
- 删除确认框如实说明会删除工作区文件（[`a49c23f`](https://github.com/Integ/Rocky/commit/a49c23f)）。
- macOS CI 中历史测试的会话清理竞态（[`af201db`](https://github.com/Integ/Rocky/commit/af201db)）。

### 移除

- 网页控制台（Mission Control）：macOS 应用成为唯一客户端（`517d785`）。

## [0.1.0] - 2026-10-20

- 持久化目标执行内核（[`03eff6e`](https://github.com/Integ/Rocky/commit/03eff6e)）：Temporal 工作流、SQL outbox、单调版本检查点、工具幂等回执、文件和语义两级验收、Docker Compose 和 CI。
- 网页控制台 Mission Control，含模型与推理强度设置、免令牌本机访问（PR #2–#5）。

各阶段的验证记录见 [docs/validation.md](docs/validation.md)，审查和分析报告见 [docs/reports/](docs/README.md#报告)。
