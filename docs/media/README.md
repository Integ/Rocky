# README 介绍素材

本组素材对应 Rocky **v0.2.0**，使用项目现有图标和青绿色视觉。
画面是根据已实现功能绘制的**流程示意与示例数据，不是真实任务录屏，也不代表实际模型的完成质量**。
没有读取个人任务、工作区、凭据或真实服务状态；制作过程不调用模型 API。

| 文件 | 用途 |
| --- | --- |
| [rocky-v0.2-overview.png](rocky-v0.2-overview.png) | README 概览图，1920 × 1080 |
| [rocky-intro.mp4](rocky-intro.mp4) | 60 秒介绍，1280 × 720、30 fps；H.264 / AAC，中文合成旁白、画面内字幕与可选字幕轨 |
| [rocky-intro.gif](rocky-intro.gif) | 800 × 450 的无声循环预览；八个分镜按视频时长依次停留，减少 README 加载体积 |
| [rocky-intro.srt](rocky-intro.srt) | 独立 UTF-8 中文字幕 |

## 分镜

| 时间 | 内容 | 对应功能 |
| --- | --- | --- |
| 00:00–00:06 | 从目标到交付 | macOS 客户端、持续执行、人工审阅 |
| 00:06–00:14 | 确认任务要求 | 验收标准、产物、范围、执行预算 |
| 00:14–00:22 | 后台执行与恢复 | 常驻服务、检查点、工具与独立验收；明确睡眠暂停边界 |
| 00:22–00:30 | 待你处理 | 具体操作批准、问题卡片、补充消息回执 |
| 00:30–00:38 | 议程 | 长期目标、定时任务、默认仅观察的事件任务与每日额度 |
| 00:38–00:46 | 交付审阅与历史 | 接受、要求修改、核验文件快照、预览与导出 |
| 00:46–00:54 | 透明执行与运维 | 模型调度、预算、9 个提示词模块、空间占用、脱敏诊断和备份 |
| 00:54–01:00 | 开始使用 | v0.2.0 下载、macOS 14+、多模型配置 |

“100 步 / 50 万 tokens / 1 小时”等任务预算和任务名称均为示例，不是每项任务固定的设置。
“4 / 1”表示默认模型总并发上限 4、为人工请求预留 1 个名额，实际值可配置。
本机持久化不等于模型计算都在本机；使用云端模型时，请求仍发送到所配置的服务商。

## 重新生成

需要 macOS、Xcode Command Line Tools、Python 3.12+、安装好的 `ffmpeg` / `ffprobe`，
以及系统中文语音 **Tingting**（`say -v '?'` 可查看）。旁白由 macOS 系统语音合成，没有使用真人录音或第三方音乐。

在仓库根目录运行：

```bash
python3 scripts/build_readme_media.py
```

脚本先生成临时分镜、旁白与编码文件，编码成功后更新上表中的四个文件。
产物包含轻微镜头推进和淡入转场；FFmpeg 写入适合网页播放的 MP4 faststart 信息。
系统语音和编码器版本可能影响声音及二进制结果，不要求逐字节可复现。

保留中间文件用于视觉与声音检查：

```bash
python3 scripts/build_readme_media.py --work-dir /tmp/rocky-readme-media
python3 scripts/build_readme_media.py --frames-only --work-dir /tmp/rocky-readme-frames
```

分镜时间、标题、字幕和旁白统一维护在 [readme_storyboard.json](../../scripts/readme_storyboard.json)。
版式和示意面板在 [render_readme_media.swift](../../scripts/render_readme_media.swift)，
编码与字幕逻辑在 [build_readme_media.py](../../scripts/build_readme_media.py)。
后续更新时先核对实现，再修改分镜；检查全部画面的文字、边界说明、旁白节奏和字幕轨，避免把示例状态写成实际运行结果。
