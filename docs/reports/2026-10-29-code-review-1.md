# 第一轮代码审查（main@947a352）

> 审查日期：2026-10-29 · 基准提交：[`947a352`](https://github.com/Integ/Rocky/commit/947a352) ·
> 范围：`src/rocky` 全部后端、`desktop/` macOS 客户端、LaunchAgent 启动脚本、构建脚本、tests 与 CI。
> 原始文档：<https://claude.ai/artifact/Q9Ntc4A884Ux8kTiEDr6Ag>

## 处理状态（截至 2026-10-30，main@8d5881d）

| # | 问题 | 状态 |
| --- | --- | --- |
| 1 | 文件工具绕过沙箱与审批，可写自启动位置 | 已修复：[`429398e`](https://github.com/Integ/Rocky/commit/429398e)（任务目录外写入逐条批准），[`dc8cb47`](https://github.com/Integ/Rocky/commit/dc8cb47) 增加授权文件夹 |
| 2 | 无审批的数据外传链路 | 已修复：`429398e`（读凭据逐条批准），`dc8cb47`（读家目录个人文件需批准、带请求体或超长 URL 的 GET 按写入审批）；短 GET 携带少量数据仍是已知风险 |
| 3 | 审批卡片不显示沙箱外运行 | 已修复：`429398e`（红标和单独按钮文案） |
| 4 | 持久化失败让长期任务永久失败 | 已修复：[`799116b`](https://github.com/Integ/Rocky/commit/799116b)（`rocky.persist` 无限重试，只暂停不失败） |
| 5 | 失败任务没有恢复路径 | 已修复：`799116b`（`POST /goals/{id}/recover`，客户端“从保存的进度恢复”） |
| 6 | 控制面文件只保护了一部分 | 已修复：API 令牌、Temporal 库、`run/`、日志目录对文件工具和沙箱命令不可见 |
| 7 | 等待审批时用户消息被忽略 | 已修复：`799116b`（发消息撤回审批请求） |
| 8 | 已批准的 POST 跟随跨源重定向 | 已修复：`799116b`（非安全方法不跨源跟随） |
| 9 | execute Activity 没有心跳 | 已修复：工具执行包在 `_with_heartbeat` 中 |
| 10 | 等待中的任务允许 Mac 空闲睡眠 | 已修复：supervisor 把 `waiting` 也算作活动任务 |
| 11 | 存储无限增长 | 部分修复：删除任务会删工作区、回执和 Cookie（`799116b`）；日志按大小轮转（[`8d5881d`](https://github.com/Integ/Rocky/commit/8d5881d)）。事件表仍无保留期限 |
| 12 | 没有数据库迁移机制 | 已修复：`799116b`（`schema_version` 与 `store.MIGRATIONS`） |
| P3 | 沙箱允许 `com.apple.SecurityServer` | 未处理：关掉前需在真机确认不影响 TLS |
| P3 | 沙箱外命令继承 worker 环境 | 已修复：Rocky/Temporal 和常见 API 凭据环境变量不传给命令 |
| P3 | 迟到的批准被丢弃 | 已修复：超时后批准自动按“补批”处理 |
| P3 | 大文件和兼容代码 | 未处理 |

以下为报告原文。

---

## 结论

工程质量整体很高：持久化边界（outbox、revision 栅栏、调度标记、回执幂等）设计扎实，路径处理普遍用 dir_fd + O_NOFOLLOW，API 本地边界和凭据处理都很谨慎，本地非集成测试 955 个全部通过。

但有一个结构性问题压过其他所有发现：**Seatbelt 沙箱和分级审批只约束了 run_command，完整权限模式下的文件工具和 GET 请求既不进沙箱也不需要审批**，可以写入任何用户可写位置、读取任何用户可读文件并发往任意网站。我在云端用脚本实测了“无审批写入任意绝对路径”。在这个问题修好之前，#8 的审批分级在安全意义上可以被绕开。

其次是长期运行可靠性：数据库持久化 Activity 只重试 3 次、总时长 45 秒，失败会让工作流永久失败，而且目前没有恢复失败任务的途径。

下面按严重度排序。P0 = 应在 1.0 前修，P1 = 强烈建议 1.0 前修，P2 = 可排期，P3 = 改进项。

## P0：安全边界

### 1. 文件工具绕过沙箱与审批，可写入自启动位置

- 位置：src/rocky/tool_contracts.py:230-235（requires_approval 只看 run_command 和非安全 HTTP 方法）；src/rocky/tools.py:636-653（_check_path 只拦截其他任务目录、.rocky、凭据与 rocky.db，以及 Rocky 自身代码/Python 运行时）；desktop/LaunchAgent/rocky-agent:51 固定 ROCKY_TOOL_ACCESS=full。
- 问题：write_file / edit_file / append_file 可写任意绝对路径且无需审批；fetch_url / http_request 带 path 的 GET 下载同样可落到任意路径。
- 实测：在云端用 WorkspaceTools 以完整权限执行 write_file 写入工作区外的 ~/.zshrc，结果 success=True、approved_by 为空，文件内容被写入。
- 可被利用的落点：~/.zshrc / ~/.zshenv（之后每条命令都会执行，包括沙箱外命令）、~/Library/LaunchAgents/*.plist（登录即以用户权限运行，持久化）、Rocky.app/Contents/Resources/runtime/bin/rocky-agent（launchd 下次重启时以不受沙箱限制的身份执行，而 dist/Rocky.app 在用户可写目录中）、Application Support/Rocky/data/temporal.db 与 api-token（可直接破坏引擎）、任意 git 仓库的 .git/hooks 和 .git/config。
- 触发条件：模型被网页、文件或命令输出中的提示注入引导即可，不需要任何审批。
- 建议：工作区外的写入进入审批（与 run_command 同一套分级，可加 “本任务内允许写入某目录” 的 task 级授权）；另外对自启动和控制面路径做硬拒绝（~/Library/LaunchAgents、shell rc 文件、~/.ssh、Rocky.app、Application Support/Rocky 整个目录）。文档里“完整权限不是沙箱”的说明与审批分级的承诺需要统一口径。

### 2. 无审批的数据外传链路

- 位置：tools.py:286-323、tool_contracts.py:230-235、models.py 中 SAFE_HTTP_METHODS。
- 问题：read_file 可读任意绝对路径（~/.ssh/id_ed25519、~/.aws/credentials、~/.config/gh/hosts.yml、浏览器配置等），GET 到任意源也不需要审批。两步组合就能把密钥放进 URL 查询串发出去，全程无人工确认。沙箱内的只读命令（cat、grep 等，seatbelt.py:223 允许全局 file-read*）同样能读到这些文件。
- 建议（选一种或组合）：对敏感路径做读取拒绝列表；工作区外读取需要审批或 task 级授权；对未出现过的源发出带查询参数的 GET 需要审批，或者至少在一次任务读取过工作区外文件后，对新源的网络请求升级为需审批。

## P1：安全与可靠性

### 3. 审批卡片不显示“是否在沙箱外运行”

- 位置：desktop/Sources/RockyDesktop/Models.swift:215-220（ProposedAction 没有 sandbox 字段）、Models.swift:168-170（summary 只拼 tool/path/url/content）、WorkspaceView.swift:226-251。
- 问题：后端对 sandbox=false 的命令要求逐条批准，但 owner 在卡片上看到的内容与沙箱内命令完全一样，无法判断自己批准的是以当前用户全部权限执行的命令。另外 ApprovalsView.swift:45 用 lineLimit(4) 截断了待批内容，批量批准时长命令后半段看不到。
- 建议：解码 sandbox 字段，沙箱外命令用醒目的红色标识和不同的按钮文案；审批列表不要截断，或提供展开。

### 4. 持久化失败会让长期任务永久失败

- 位置：src/rocky/workflow.py:285-293（rocky.persist：maximum_attempts=3、schedule_to_close 45 秒）；workflow.py:939、972 等处的 _end_phase 在 try 之外调用 _persist。
- 问题：ActivityError 属于 Temporal 的 FailureError，从工作流代码抛出会直接让工作流执行失败（不是重试工作流任务）。只要数据库连续约 45 秒不可写（磁盘满、SQLite 被长事务占用），或者 Mac 在 persist 执行期间睡眠（caffeinate -i 只防空闲睡眠，合盖仍会睡），醒来后超时，任务就会被判失败，之后由对账逻辑标记为 failed。
- 建议：persist 是 revision 栅栏保护的幂等写，可以无限重试、去掉 schedule_to_close，只保留较短的 start_to_close；_complete 与 run() 开头的 persist 同理。补一个“persist 前几次失败后恢复”的工作流测试。

### 5. 失败任务没有恢复路径

- 位置：src/rocky/service.py:29-48。
- 问题：错误文案让用户“从保存的状态恢复任务”，但 API 和客户端都没有这个功能，只能重新提交新任务。对于“长期自主运行”这是最关键的一块缺口，和第 4 条叠加后影响更大。
- 实现时注意：工具回执按 operation_id（step-NNNN）存放在 .rocky/<goal_id>/，如果恢复时从 steps=0 重新开始，会与旧回执冲突（报 “operation identifier cannot be reused”，或者重放旧结果）。恢复应从最后的投影状态继续计数，或使用新的 operation_id 命名空间。

### 6. 控制面文件只保护了一部分

- 位置：src/rocky/tools.py:305-316。
- 问题：保护列表只有 credentials.json 和 rocky.db（含 -wal/-shm）。data/api-token、data/temporal.db、logs/、run/ 都不在列表中。后果：沙箱内的 grep -a 读取 temporal.db 属于只读命令，自动放行，可以读到其他任务的完整历史（工具输出、文件内容），违背 seatbelt.py:81-86 “其他任务不可访问”的承诺；api-token 可被读取后进入模型上下文；文件工具还能改写这些文件（见第 1 条）。
- 建议：直接把 Application Support/Rocky 下除当前任务工作区以外的部分整体加入 protected_paths（读写都拒绝）。

## P2：行为与运维

### 7. 等待审批期间，用户发来的消息被忽略

- 位置：workflow.py:562-569。
- 问题：等待条件只看取消、批准和拒绝，不看 wake_generation。用户在卡片出现后发消息说“别这样做，改成 X”，任务要等批准、拒绝或 30 分钟超时之后才会读到。
- 建议：收到 wake 时把当前请求视为拒绝，并把消息作为反馈交给下一轮规划。

### 8. 已批准的 POST 可被重定向带到其他源

- 位置：http_tools.py:188（follow_redirects 默认 true）、http_tools.py:316-331（跨源时只剥离认证头，307/308 会保留方法和请求体）、approval_policy.py:123-140（规则只匹配初始 URL）。
- 问题：按源授权的 POST（task 或 always 规则）遇到 307 后，请求体会发往未经批准的源。
- 建议：非安全方法禁止跨源重定向（遇到时返回结果让模型重新提请求），或者非安全方法默认不跟随重定向。

### 9. execute Activity 没有心跳，超时后线程仍持有锁

- 位置：workflow.py:349-376（start_to_close 45 秒、3 次重试）、tools.py:804-836（命令 30 秒，前后各遍历一次目录，最多 5000 个文件）、activities.py:116（to_thread 无法取消）。
- 问题：完整权限下 cwd 可以是大目录，两次遍历加上 30 秒命令可能超过 45 秒。超时后原线程仍持有 goal 的排他 flock，重试会在锁上阻塞直到再次超时，最后得到 outcome_unknown。
- 建议：execute 加心跳；遍历设置时间预算；超时尝试先检查回执状态再抢锁。

### 10. 等待中的任务允许 Mac 空闲睡眠

- 位置：supervisor.py:27（ACTIVE_STATUSES 只有 queued/running）、supervisor.py:293-302（用 /goals 默认 50 条判断）。
- 问题：模型主动 wait 的任务不会阻止睡眠，所以定时器会推迟到下次唤醒。如果这是有意为之（省电），应在界面上说明；否则把 waiting 也算作活跃状态。另外任务超过 50 个时，较早的活跃任务可能漏判。

### 11. 存储无限增长

- 每次 revision 都向 events、execution_events、goal_messages 追加记录，没有保留策略。
- delete_goal（store.py:749-774）只清理数据库，不删除 workspaces/<goal_id> 和 .rocky/<goal_id> 下的回执、cookie。
- 日志只在重启时轮转一次（supervisor.py:71-73），uvicorn 访问日志会把客户端每 2 秒一次的健康检查都记录下来。
- 建议：删除任务时一并删除文件；关闭访问日志或改用按大小轮轮；为已结束任务的 events 设置压缩或保留期限。

### 12. 没有数据库迁移机制（已知）

- store.py:256-257 只调用 create_all，新增列不会作用到已有数据库。目前靠“新增表而不改旧表”绕开，可以撑一段时间，但 1.0 前应引入 schema_version 表加顺序迁移脚本（不一定要用 alembic）。

## P3：改进项

- 沙箱允许 mach-lookup com.apple.SecurityServer（seatbelt.py:216）。模型密钥存在登录钥匙串中，由 .venv 里的 Python 创建，没有设置访问控制；被批准过的 uv run / python3 脚本前缀在沙箱内可能静默读取密钥。如果去掉这一项不影响 TLS（trustd 是单独列出的），建议删除。
- 沙箱外命令使用 env=None（tools.py:790），会继承 worker 的完整环境，包括 ROCKY_API_TOKEN。权限上已经是完全信任，但令牌可能随命令输出进入模型上下文，建议同样使用过滤后的环境。
- build_macos.sh:60-63 的 uv 校验和与二进制从同一来源下载，起不到完整性保护作用。建议像 Temporal 一样固定哈希。
- 完整权限下产物下载不限大小（tools.py:334），API 会把整个文件读入内存。
- 迟到的批准（超时后几秒才点）会被静默丢弃（workflow.py:236-242 一带），界面上只是该项变成“超时未批”。建议在 API 层把这种情况自动转为 deferred 处理。
- 代码层面：planner.py（1527 行）、store.py（1434 行）、tools.py（1119 行）偏大；为 v0.1 历史保留的兼容代码（scoped 模式、旧预算字段、workflow.patched 分支、getattr(settings, …) 防御）在单用户场景下可以在有了迁移机制后清理；终止状态列表在 service.py:178-184 与 store.TERMINAL_STATUSES 重复；JSON 去重键解析在 tool_contracts.py 里写了两遍。

## 测试缺口

- 没有测试断言“工作区外写入/敏感路径读取需要审批”。现有 full-access 测试把这种能力当作功能来验证，修第 1、2 条时需要同步调整。
- 没有覆盖 persist 连续失败、Activity 超时后恢复、Mac 睡眠后定时器行为的工作流测试。
- Swift 侧没有测试审批卡片对沙箱外命令的展示。
- 集成测试依赖在线下载 Temporal 测试服务器，离线环境跑不了（CI 上正常）。本次在云端只跑了非集成部分：955 passed、46 skipped。
- LaunchAgent 与审批在真机上仍未验证（已知）。

## 做得好的地方

- Outbox 加 REJECT_DUPLICATE、revision 条件更新、调度标记加 outcome_unknown，构成了一套完整的“至多一次外部副作用”设计。
- API 本地边界（Host/Origin/Sec-Fetch-Site 校验、关闭 proxy_headers）、服务模式强制令牌、422 不回显密钥，都很到位。
- 只读命令目录的过滤很细（GNU 长选项缩写、未加引号的通配符、zsh 的 = 展开、PATH 中的相对目录），并且有意排除了 git。
- 沙箱按端口拦截控制服务，覆盖了 IPv4 映射的 IPv6 回环地址；HTTP 工具在每次重定向时也做了检查。
- 证据核对由应用来判断审查结论，而不是信任模型的“通过”。

## 建议的修复顺序

1. 第 1、2、6 条一起做：工作区外文件访问进入审批，控制面与自启动路径硬拒绝，敏感路径读取拒绝。
2. 第 3 条：审批卡片显示沙箱状态并取消截断（改动小，收益大）。
3. 第 4、5 条：persist 改为无限重试，再做从投影状态恢复失败任务的入口。
4. 其余按需排期。
