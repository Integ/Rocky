# Rocky 文档

## 使用与设计

| 文档 | 内容 |
| --- | --- |
| [../README.md](../README.md) | v0.3 功能、下载与上手、后端开发、工具与审批、状态与 API、运维 |
| [media/README.md](media/README.md) | v0.3 概览与功能示意图、一分钟介绍、字幕、分镜与素材生成方式 |
| [macos.md](macos.md) | macOS 应用：安装、模型配置、提交与继续任务、审批与通知、数据目录 |
| [architecture.md](architecture.md) | 执行架构（英文）：持久边界、执行循环、生命周期、验收与信任、权限边界 |
| [validation.md](validation.md) | 各阶段的验证记录（英文，按时间追加） |
| [../evals/README.md](../evals/README.md) | 评测集：任务、运行方法、基线与回归门槛 |
| [../CHANGELOG.md](../CHANGELOG.md) | 更新日志 |

## 报告

审查和分析报告按日期存放在 [reports/](reports/)。报告正文保留写作时的原样，行号对应当时的基准提交；
每份报告开头注明之后的处理状态。

| 日期 | 报告 | 基准提交 | 状态 |
| --- | --- | --- | --- |
| 2026-10-27 | [三项评测失败调查与最小修复](reports/2026-10-27-eval-failure-investigation.md) | `49d6a3c` | 补丁已随 PR #6 合并 |
| 2026-10-29 | [第一轮代码审查](reports/2026-10-29-code-review-1.md) | `947a352` | 已修复（`429398e`、`799116b` 等），钥匙串服务访问、事件保留期限和大文件拆分未处理 |
| 2026-10-29 | [后端常驻化方案](reports/2026-10-29-resident-backend-plan.md) | `af201db` | 第一、二阶段已实现（`0641840` 起），Linux 阶段不做 |
| 2026-10-29 | [影响任务完成的因素](reports/2026-10-29-task-completion-factors.md) | `799116b` | 主要障碍已有对应改动（续做、计时暂停、长命令、授权文件夹） |
| 2026-10-30 | [第二轮代码审查](reports/2026-10-30-code-review-2.md) | `77b8851` | 18 条全部修复（至 `dcb5450`），未真机验证 |
| 2026-11-03 | [真实使用验证与修复](reports/2026-11-03-real-usage-validation.md) | `b22fce9` | 验收、错误提示与窗口重开已修复；两条回复路径真实复测通过，Python 1338 / Swift 108 项通过 |
| 2026-11-03 | [执行能力增强与验证](reports/2026-11-03-execution-validation.md) | 本轮增强前快照 | 支持独立文件批次及逐文件恢复；三类真实评测通过，Python 1365 项通过，新构建原生验证通过 |
| 2026-11-03 | [文件查找与内容搜索工具验证](reports/2026-11-03-tool-expansion-validation.md) | 本轮工具扩展前工作区 | 新增两项只读工具；Python 1420 / Swift 109 项通过，真实模型和原生应用验证通过 |
| 2026-11-03 | [工具与执行衔接质量审查](reports/2026-11-03-quality-review.md) | 工具扩展后工作区 | 3 项 P2 已修复；Python 1450 项通过，120 文件分页核对通过，修复构建已启动 |
