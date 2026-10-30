# 后端常驻化方案

> 方案日期：2026-10-29 · 基准提交：[`af201db`](https://github.com/Integ/Rocky/commit/af201db) ·
> 原始文档：<https://claude.ai/artifact/DqDGeNbCzmRH6cBq6Ty2s9>

## 落地状态（截至 2026-10-30，main@8d5881d）

- **第一、二阶段已实现**：[`0641840`](https://github.com/Integ/Rocky/commit/0641840) 加入 `rocky serve` supervisor、持久 API 令牌和 `SMAppService` 注册的 LaunchAgent（`dev.rocky.agent`），关窗口后任务继续跑。之后 [`5883210`](https://github.com/Integ/Rocky/commit/5883210) 把 Python 和依赖打进应用，服务离线启动；[`ad16cf5`](https://github.com/Integ/Rocky/commit/ad16cf5) 加入系统通知、Dock 角标和菜单栏；[`8d5881d`](https://github.com/Integ/Rocky/commit/8d5881d) 加入每日备份、`/status`、`/metrics` 和 `rocky rotate-token`。
- **第三阶段（Linux 主机和 bubblewrap 沙箱）不做**：2026-10-29 确认 Rocky 只在 Mac 上运行；2026-10-30 确认不做多机和远程访问。
- 尚未在真机上验证常驻服务。

以下为方案原文。

---

## 结论与推荐

推荐两步走：先把后端做成 macOS 用户级 LaunchAgent（经 `SMAppService` 由应用注册），应用退化为纯客户端；再补一个 Linux 沙箱后端（bubblewrap 为主），让同一套服务能跑在 Linux 主机或 Docker 里实现 7×24 运行。

- 第一步改动最小：Seatbelt 沙箱、钥匙串凭据、回环 API 全部原样可用，关掉窗口任务照跑。主要工作是“启动/监控/停止”从 `RuntimeManager.swift` 挪到一个常驻 supervisor，以及把每次启动随机生成的 API 令牌改成持久令牌。
- 第二步是真正脱离 Mac 的前提：当前 `seatbelt.py` 在非 macOS 直接抛 `SandboxUnavailable`，Linux 上每条 Shell 命令都只能走“无沙箱 + 逐条审批”，自主性基本归零。所以 Linux 常驻的关键不是 systemd 或 Docker，而是沙箱。
- 不推荐：在 Mac 上用 Docker 跑后端（丢 Seatbelt，又依赖 Docker Desktop 常开），以及 root LaunchDaemon（拿不到登录钥匙串）。

依据：`main@af201db` 的代码阅读，未改代码，未在真机上验证。

## 现状：后端如何与 macOS 应用耦合

后端本身已经是独立进程（`rocky api`、`rocky worker` + Temporal dev server），耦合几乎全在应用的 `RuntimeManager.swift` 和两个 macOS 专属模块里。Temporal 的持久工作流本来就支持重启恢复，所以常驻化不需要改执行内核。

| 耦合点 | 位置 | 常驻化时的影响 |
| --- | --- | --- |
| 应用以子进程启动 temporal/api/worker，退出时 SIGTERM→SIGKILL | `RuntimeManager.start()/stop()`、`RockyDesktopApp.swift:8` | 生命周期需移交给 launchd/systemd |
| API 令牌每次启动由 `UUID()` 生成，只在内存和子进程环境变量里 | `RuntimeManager.apiToken` | 需要持久令牌，客户端从钥匙串或 0600 文件读取 |
| 启动时 `uv sync` 安装依赖，并把应用内 backend 拷到 Application Support | `stageBackend()` | 升级需显式“停服务→替换→启动”，不能再靠打开应用触发 |
| 固定端口 18741/18743，`desktop.lock` flock 防多实例 | `checkPorts()`、`prepareDirectoriesAndLock()` | 锁应由服务持有，应用改为探测连接 |
| 就绪文件 `ROCKY_WORKER_READY_FILE` + 2 秒健康检查，异常即全部停掉 | `startMonitoring()`、`service.py:232` | 重启策略交给 supervisor（KeepAlive / Restart=on-failure） |
| 模型密钥存登录钥匙串，非 macOS 自动改用 0600 文件 | `credentials.py` | LaunchAgent 可用；LaunchDaemon/Linux 走文件存储 |
| Shell 沙箱只有 Seatbelt，其他平台拒绝执行 | `seatbelt.py:prepare_sandbox`、`tools.py:432` | Linux/Docker 下无法自主跑命令，是最大阻碍 |
| “在 Finder 中打开工作区”直接用本地路径 | `AppModel.swift:287` | 远程部署时需隐藏或改走 API；产物预览/下载已经走 API |

顺带一个现存问题：任务时限把“应用关闭期间”也算进去（`docs/macos.md:55`），常驻后这个问题自然消失。

## 方案对比

LaunchAgent 成本最低、安全性不降；Linux 主机是唯一真正 7×24 且不占用日常电脑的选项，但必须先做 Linux 沙箱。

| 方案 | 常驻程度 | Shell 沙箱 | 凭据 | 客户端访问 | 改动量 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| A. macOS LaunchAgent（`SMAppService` 注册） | 用户登录期间常驻，崩溃自动拉起；合盖/睡眠会暂停 | Seatbelt 原样可用 | 登录钥匙串可用 | 回环 + 持久令牌 | 小：主要在 Swift 侧 | 首选，第一步 |
| B. macOS LaunchDaemon（root） | 开机即运行，无需登录 | Seatbelt 可用，但以 root 或专用用户运行，读不到用户文件 | 无登录钥匙串，需改文件 | 同 A | 中：权限、路径、安装器 | 不推荐 |
| C. Mac 上的 Docker（现有 compose） | 取决于 Docker Desktop 是否常开 | 容器内是 Linux，Seatbelt 不可用 | 文件/环境变量 | token 模式，已支持 | 小，但需 Linux 沙箱才能用 | 不推荐（兼得 A 和 D 的缺点） |
| D. Linux 主机（VPS/NAS/家用服务器），systemd 或 compose | 真正 7×24，不受 Mac 睡眠影响 | 需新实现（见下节） | 0600 文件，已支持；可接 systemd-creds | token 模式 + TLS；建议 Tailscale 等私网隧道，不暴露公网 | 中到大：沙箱 + 远程连接 + 审批通知 | 长期目标，第二步 |
| E. 云端托管 Temporal + 无状态 worker | 高可用 | 同 D | 同 D | 同 D | 大：多主机工作区、回执幂等要重做 | v0.1 是单主机设计（`docs/architecture.md`），暂不考虑 |

A 的睡眠问题可以缓解：worker 在有任务运行时持有 `IOPMAssertion`（或 `caffeinate -i -w <pid>`）阻止空闲睡眠，但合盖仍会睡。一台常开的 Mac mini 跑 A，实际效果接近 D。

## 非 macOS 沙箱方案

推荐 bubblewrap（bwrap）做文件隔离，再用独立网络命名空间或专用 uid + nftables 屏蔽控制端口。它是唯一能不要 root、又能表达“全盘可读但隐藏其他任务”的常见工具。

要复刻的 Seatbelt 保证（来自 `seatbelt.py`）：

1. 只能写当前任务目录和 `/dev/null`。
2. 读不到其他任务、`.rocky/`、凭据文件和 SQLite 库；其他文件可读。
3. 网络开放，但连不上 Rocky API 和 Temporal 端口（防止任务自己审批）。
4. 限制被子进程和脱离进程组的后台作业继承。
5. 环境变量清洗（已是跨平台代码，可直接复用）。

| 工具 | 文件隔离（1、2） | 端口屏蔽（3） | 需要的权限 | 评价 |
| --- | --- | --- | --- | --- |
| bubblewrap | `--ro-bind / /` + `--tmpfs` 覆盖工作区根 + `--bind` 当前任务 + 空文件遮蔽受保护路径，完整覆盖 | 自身只能全断网；配合 pasta/slirp4netns 提供出网，宿主回环不可达 | 非特权用户命名空间 | 主方案 |
| Landlock | 只有白名单语义，难以表达“读全盘但排除兄弟目录”；做“只写任务目录”很合适 | 内核 6.7+ 可限制 TCP connect，但也是白名单 | 无（5.13+ 内核） | 叠加在 bwrap 内做纵深防御 |
| 专用 uid + nftables `meta skuid` | 不负责 | 精确拒绝指定端口，语义与现在一致 | 一次性 root 配置 | Linux 主机上最简单的端口方案 |
| nsjail / firejail | 可以 | 可以 | firejail 是 setuid，攻击面大 | 可用但不如 bwrap 简单 |
| gVisor / Firecracker / 每条命令一个容器 | 强 | 强 | 挂 docker.sock 等于给 worker root；microVM 启动开销大 | 对单人 v0.1 过重 |

代码上建议把 `prepare_sandbox` 抽成后端接口（Seatbelt / Bubblewrap），按平台或 `ROCKY_SANDBOX` 选择，继续保持“不可用就拒绝，绝不静默降级”。当前 Linux CI 跳过沙箱测试，做完后 Linux 也能跑 `tests/test_command_isolation.py` 一类的隔离测试。

Docker 里用 bwrap 有个坑：默认 seccomp 不允许非特权创建用户命名空间，需要定制 seccomp profile；Ubuntu 24.04 宿主还会用 AppArmor 限制非特权 userns。这也是我建议 Linux 上优先用 systemd 裸跑、compose 作为次选的原因。

## 改动清单与分阶段计划

三个阶段各自可独立合并，第一阶段做完就能解决“关窗口任务就停”。

1. **抽出 supervisor（跨平台共用）**
   - 新增 `rocky serve`：在一个进程里按顺序拉起 Temporal → 等待可连 → api + worker，任一退出则整体退出，由 launchd/systemd 重启。launchd 没有服务依赖，单进程比三个 plist 简单。
   - 把 `RuntimeManager` 里的锁、端口检查、就绪文件、环境变量白名单移到 Python；SIGTERM 时优雅停止 worker。
   - API 令牌持久化：首次生成，存 0600 文件（macOS 可放钥匙串），支持轮换。
2. **macOS LaunchAgent**
   - 应用内嵌 `Contents/Library/LaunchAgents/com.rocky.agent.plist`（`KeepAlive`、`RunAtLoad`、日志路径），用 `SMAppService.agent(plistName:)` 注册，用户在“登录项”里可见可关。
   - `RuntimeManager` 改为：探测 `/health` → 未注册则注册 → 连接；退出应用不再 `stop()`。设置里加“后台运行”开关和“重启服务”。
   - 升级流程：版本号不一致时停服务 → `stageBackend` + `uv sync` → 启动。只在没有 running 任务时自动升级，否则提示。
   - 有任务运行时持有防睡眠断言。
3. **Linux 主机**
   - Bubblewrap 沙箱后端 + 端口屏蔽 + Linux CI 隔离测试。
   - 提供 `rocky.service` systemd unit（专用用户、`Restart=on-failure`、`ProtectSystem=strict`）；compose 改成一个容器跑 `rocky serve` 并附带 seccomp profile。
   - macOS 客户端支持“连接远程服务”：URL + 令牌，隐藏“在 Finder 中打开”。
   - 审批和 `blocked` 状态需要推送（本地通知、ntfy/Telegram/邮件 webhook 任选其一），否则常驻的任务会卡在等审批。
   - 备份：Temporal 库、Rocky 库、工作区三者一起备份（架构文档已要求）。

## 风险与待确认问题

- 常驻后任务在无人看着时也会调用模型和工具。现有预算上限和审批门禁仍生效，但建议加一个全局“暂停所有任务”开关。
- 本机 `local` 鉴权模式下任意本地程序都能调 API，常驻后暴露时间变长；应用启动的服务应继续固定用 `token` 模式。
- Temporal dev server（`start-dev`）无鉴权、单机 SQLite。Linux 主机上必须只绑回环，并依赖沙箱端口屏蔽。
- bwrap 的端口屏蔽细节（pasta 是否默认把网关映射到宿主回环、Docker seccomp 改动范围）是按文档推的，实现前需要在目标发行版上实测。

待你确认：

- [x] 已确认（2026-10-29）：只在 Mac 上使用。范围收窄为第一、二阶段，第三阶段和 Linux 沙箱暂不做。
- [ ] 如果要上 Linux，计划用 VPS、NAS 还是家里的服务器？影响远程访问和内核版本选择。
- [ ] 审批推送偏好哪个渠道？
