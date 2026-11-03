# README 介绍素材

当前素材对应 **Rocky v0.3.0**，采用与新版应用一致的黑白灰配色。
画面是根据已实现功能绘制的流程示意，全部任务内容为合成数据。
旁白沿用中文播音男声音色，由本机 F5-TTS 生成；没有使用用户录音、个人任务或凭据。

| 文件 | 内容 |
| --- | --- |
| [rocky-v0.3-overview.png](rocky-v0.3-overview.png) | 1920 × 1080 项目概览与视频封面 |
| [rocky-v0.3-tools.png](rocky-v0.3-tools.png) | 文件查找、内容搜索、路径与行号示意 |
| [rocky-v0.3-execution.png](rocky-v0.3-execution.png) | 独立文件批次、逐文件结果和恢复回执示意 |
| [rocky-intro-broadcast-male.mp4](rocky-intro-broadcast-male.mp4) | 60 秒介绍，1280 × 720 / 30 fps，H.264、AAC；中文男声、画面字幕和可选字幕轨 |
| [rocky-intro.gif](rocky-intro.gif) | 800 × 450 无声预览，按实际分镜时长依次停留 |
| [rocky-intro.srt](rocky-intro.srt) | 独立 UTF-8 中文字幕 |
| [rocky-voice-preview.mp3](rocky-voice-preview.mp3) | 前两段旁白试听 |
| [voice-generation.json](voice-generation.json) | 模型修订、依赖版本、采样设置和逐段音频摘要 |
| [voice-profile.json](voice-profile.json) | 音色来源、参考音频摘要及语速设置 |
| [media-validation.json](media-validation.json) | 视频规格、字幕核对、音量、独立本地转写比对与媒体摘要 |
| [f5-reference.wav](f5-reference.wav) / [f5-reference.txt](f5-reference.txt) | 24 kHz 单声道合成参考音色及逐字转录 |

## 分镜

| 时间 | 内容 |
| --- | --- |
| 00:00–00:06 | Rocky：从目标到交付 |
| 00:06–00:14 | 确认验收标准、产物、范围和预算 |
| 00:14–00:22 | 查找文件、搜索内容，保留路径、行号和上下文 |
| 00:22–00:30 | 一轮规划修改多个独立文件，逐文件保存结果 |
| 00:30–00:38 | 审批、问题与补充消息的消费回执 |
| 00:38–00:46 | 独立核验、文件快照、接受结果与要求修改 |
| 00:46–00:54 | 后台执行、议程、诊断和预算 |
| 00:54–01:00 | 下载 Rocky v0.3.0，开始使用 |

图中的任务、人物、文件状态及预算均为示例；文件名、行号和原文来自合成验证资料。
任务工作区内的文件批次上限为 8 个，依次执行，部分失败保留已完成文件；批次不提供事务回滚。
新消息、审批要求或不确定结果会停止后续修改。搜索的部分结果不能证明某项内容不存在。
Mac 合盖或手动睡眠时任务暂停，唤醒后继续；云端模型请求会发送给用户配置的服务商。

## 重新生成

需要 Apple Silicon Mac、Xcode Command Line Tools、Python 3.12 和 FFmpeg / FFprobe。
配音在独立环境中使用 [F5-TTS 的 MLX 实现](https://github.com/lucasnewman/f5-tts-mlx)，
采用 8-bit 权重、48 步 Euler 采样。该环境不属于 Rocky 的应用依赖。

```bash
uv venv /tmp/rocky-readme-tts --python 3.12
uv pip install --python /tmp/rocky-readme-tts/bin/python -r scripts/requirements-readme-media.txt
python3 scripts/build_readme_media.py --tts-python /tmp/rocky-readme-tts/bin/python --work-dir /tmp/rocky-readme-media
```

首次推理需要下载公开的 F5-TTS 和 Vocos 模型；后续可复用本机缓存。模型权重未放入仓库。
F5-TTS 预训练权重许可为 CC-BY-NC-4.0，详见 [模型卡](https://huggingface.co/SWivid/F5-TTS)。
参考音色来自 Microsoft Edge 通用新闻男声 `zh-CN-YunyangNeural`，参考文本为
“让复杂的任务，有条理地推进。让每一份成果，都清晰可见。”。最终旁白由 F5-TTS 生成，
没有使用具体人物录音，也没有第三方背景音乐。

只生成八个分镜 PNG：

```bash
python3 scripts/build_readme_media.py --frames-only --work-dir /tmp/rocky-readme-frames
```

复用已经生成的 `voice-0.wav` 至 `voice-7.wav` 和同目录的 `voice-generation.json`：

```bash
python3 scripts/build_readme_media.py --narration-dir /tmp/rocky-readme-media --work-dir /tmp/rocky-readme-encode
```

脚本会核对旁白文字、音色配置、参考音频和每段 WAV 的摘要，编码成功后再更新 README 的媒体文件。
更新分镜时先核对实现，再检查全部画面、字幕、旁白节奏和视频音轨。

分镜与口播维护在 [readme_storyboard.json](../../scripts/readme_storyboard.json)；
布局在 [render_readme_media.swift](../../scripts/render_readme_media.swift)；
配音在 [synthesize_readme_narration.py](../../scripts/synthesize_readme_narration.py)；
编码和字幕在 [build_readme_media.py](../../scripts/build_readme_media.py)。
旧版 [v0.2 概览图](rocky-v0.2-overview.png) 保留用于历史参考。
