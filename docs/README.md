# Rocky 文档

## 使用与设计

| 文档 | 内容 |
| --- | --- |
| [../README.md](../README.md) | 项目概览、后端开发、工具与审批、状态与 API、运维 |
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
