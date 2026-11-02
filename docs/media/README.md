# README 介绍素材

本组素材对应 Rocky **v0.2.0**，使用项目现有图标和青绿色视觉。
画面是根据已实现功能绘制的**流程示意与示例数据，不是真实任务录屏，也不代表实际模型的完成质量**。
没有读取个人任务、工作区、凭据或真实服务状态。正式旁白在本机使用 F5-TTS 推理。
参考音色使用 Microsoft Edge 的通用新闻男声，以公开示例文字在线合成；没有上传用户录音或任务数据。

| 文件 | 用途 |
| --- | --- |
| [rocky-v0.2-overview.png](rocky-v0.2-overview.png) | README 概览图，1920 × 1080 |
| [rocky-intro.mp4](rocky-intro.mp4) | 60 秒介绍，1280 × 720、30 fps；H.264 / AAC，F5-TTS 沉稳中文播音男声、画面内字幕与可选字幕轨 |
| [rocky-voice-preview.mp3](rocky-voice-preview.mp3) | 新男声前两段的短试听 |
| [rocky-intro.gif](rocky-intro.gif) | 800 × 450 的无声循环预览；八个分镜按视频时长依次停留，减少 README 加载体积 |
| [rocky-intro.srt](rocky-intro.srt) | 独立 UTF-8 中文字幕 |
| [f5-reference.wav](f5-reference.wav) / [f5-reference.txt](f5-reference.txt) | 新闻播音男声的合成参考与逐字转录，24 kHz 单声道 |
| [voice-profile.json](voice-profile.json) | 音色来源、风格与参考音频校验值，以及语速和采样设置 |
| [voice-generation.json](voice-generation.json) | 本次模型、依赖版本、采样参数、种子和逐段音频校验值 |

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

需要 Apple Silicon Mac、Xcode Command Line Tools、Python 3.12 和 `ffmpeg` / `ffprobe`。
旁白使用 [F5-TTS](https://github.com/SWivid/F5-TTS) 的 [MLX 实现](https://github.com/lucasnewman/f5-tts-mlx)：
8-bit 权重、48 步 Euler 采样。配音依赖在单独环境中安装，不属于 Rocky 的应用依赖。

在仓库根目录运行：

```bash
uv venv /tmp/rocky-readme-tts --python 3.12
uv pip install --python /tmp/rocky-readme-tts/bin/python -r scripts/requirements-readme-media.txt
/tmp/rocky-readme-tts/bin/python scripts/build_readme_media.py
```

首次推理会从 Hugging Face 下载 F5-TTS 与 Vocos 权重，之后复用缓存。模型权重没有放入仓库；
上游权重许可见 [F5-TTS 模型卡](https://huggingface.co/SWivid/F5-TTS)，其中预训练权重为 CC-BY-NC-4.0。
`f5-reference.wav` 使用 `zh-CN-YunyangNeural`（Microsoft 音色列表标为新闻类、专业可靠的男声），
参考文本为“让复杂的任务，有条理地推进。让每一份成果，都清晰可见。”，语速为 `-5%`。
它只作为音色参考，最终八段旁白全部由 F5-TTS 生成，没有使用具体人物录音或第三方音乐。
每段时长根据参考音色的实际语速、文本长度和标点估算，不再统一拉成相同秒数；过长时要求缩短口播，
给分镜留出自然停顿。复用配音时还会核对音色方案，防止误用上一版音色。

脚本先生成临时分镜、旁白与编码文件，编码成功后更新媒体及生成记录。
产物包含轻微镜头推进和淡入转场；FFmpeg 写入适合网页播放的 MP4 faststart 信息。
模型、MLX、设备与编码器版本可能影响结果；记录种子与模型修订，不要求逐字节可复现。

只替换旁白、直接复制现有视频与字幕流：

```bash
/tmp/rocky-readme-tts/bin/python scripts/build_readme_media.py --audio-only
```

已有生成好的八段 F5-TTS 配音时，可直接用它们重做完整视频，不必重新推理。
目录中需有 `voice-0.wav` 至 `voice-7.wav` 及 `voice-generation.json`；脚本会核对文本与音频校验值：

```bash
python3 scripts/build_readme_media.py --narration-dir /tmp/rocky-readme-tts-clips
```

保留中间文件用于视觉与声音检查：

```bash
/tmp/rocky-readme-tts/bin/python scripts/build_readme_media.py --work-dir /tmp/rocky-readme-media
python3 scripts/build_readme_media.py --frames-only --work-dir /tmp/rocky-readme-frames
```

分镜时间、标题、字幕和旁白统一维护在 [readme_storyboard.json](../../scripts/readme_storyboard.json)，
播音方案维护在 [voice-profile.json](voice-profile.json)。
版式和示意面板在 [render_readme_media.swift](../../scripts/render_readme_media.swift)，
F5-TTS 合成在 [synthesize_readme_narration.py](../../scripts/synthesize_readme_narration.py)，
编码与字幕逻辑在 [build_readme_media.py](../../scripts/build_readme_media.py)。
也可通过 `--tts-python /path/to/python` 从普通 Python 环境调用独立的配音环境。
后续更新时先核对实现，再修改分镜；检查全部画面的文字、边界说明、旁白节奏和字幕轨，避免把示例状态写成实际运行结果。
