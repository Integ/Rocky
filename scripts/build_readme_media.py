"""Build the README's v0.2 overview, one-minute narrated tour and GIF on macOS.

Uses AppKit, local F5-TTS (MLX), and FFmpeg. No remote voice/model API calls.
Outputs are staged and replace published files only after encoding succeeds.
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "docs/media"
STORYBOARD = json.loads((ROOT / "scripts/readme_storyboard.json").read_text(encoding="utf-8"))
STARTS = [scene["start"] for scene in STORYBOARD]
ENDS = [scene["end"] for scene in STORYBOARD]
CAPTIONS = [scene["caption"] for scene in STORYBOARD]


def run(arguments, *, stream=False, **options):
    result = subprocess.run(arguments, text=True, capture_output=not stream, **options)
    if result.returncode:
        raise RuntimeError(
            f"{arguments[0]} failed:\n{(result.stderr or '')[-7000:]}\n{(result.stdout or '')[-2000:]}"
        )
    return result.stdout or ""


def probe(path):
    return json.loads(
        run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    )


def timestamp(seconds):
    millis = round(seconds * 1000)
    return f"{millis // 3600000:02}:{millis // 60000 % 60:02}:{millis // 1000 % 60:02},{millis % 1000:03}"


def build_preview(work):
    # Keep the GIF small: static storyboard holds, with motion in the MP4 only.
    sequence = work / "preview.ffconcat"
    sequence.write_text(
        "ffconcat version 1.0\n"
        + "".join(
            f"file scene-{i:02}.png\nduration {end - start}\n"
            for i, (start, end) in enumerate(zip(STARTS, ENDS, strict=True))
        )
        + "file scene-07.png\n"
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(sequence),
            "-filter_complex",
            "fps=1,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=3",
            "-t",
            "60",
            "-an",
            "-loop",
            "0",
            str(work / "rocky-intro.gif"),
        ]
    )


def synthesize_narration(work, tts_python):
    run(
        [
            str(tts_python),
            str(ROOT / "scripts/synthesize_readme_narration.py"),
            "--output-dir",
            str(work),
            "--reference-audio",
            str(MEDIA / "f5-reference.wav"),
            "--reference-text",
            str(MEDIA / "f5-reference.txt"),
        ],
        stream=True,
    )


def audio_filters(work, first_input):
    filters = []
    for i, (start, end) in enumerate(zip(STARTS, ENDS, strict=True)):
        duration = float(probe(work / f"voice-{i}.wav")["format"].get("duration", 0))
        if duration <= 0:
            raise ValueError(f"Missing or silent F5-TTS narration for scene {i + 1}")
        tempo = max(1, duration / (end - start - 1.0))
        if tempo > 1.5:
            raise ValueError(f"Narration {i} needs shortening (tempo={tempo:.2f})")
        filters.append(
            f"[{i + first_input}:a]aresample=48000,atempo={tempo:.5f},"
            f"adelay={round((start + 0.45) * 1000)}:all=1[a{i}]"
        )
        print(f"Scene {i + 1}: F5-TTS {duration:.1f}s, playback ×{tempo:.2f}", flush=True)
    filters.append(
        "".join(f"[a{i}]" for i in range(8))
        + "amix=inputs=8:duration=longest:normalize=0,apad,atrim=0:60,"
        "loudnorm=I=-18:TP=-2:LRA=7,aresample=48000,afade=t=out:st=59.6:d=0.4[audio]"
    )
    return filters


def verify_video(video):
    metadata = probe(video)
    assert abs(float(metadata["format"]["duration"]) - 60) < 0.1
    assert {stream["codec_type"] for stream in metadata["streams"]} == {
        "video",
        "audio",
        "subtitle",
    }


def replace_audio(work, tts_python):
    synthesize_narration(work, tts_python)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(MEDIA / "rocky-intro.mp4"),
    ]
    for i in range(8):
        command += ["-i", str(work / f"voice-{i}.wav")]
    video = work / "rocky-intro.mp4"
    command += [
        "-filter_complex",
        ";".join(audio_filters(work, 1)),
        "-map",
        "0:v:0",
        "-map",
        "[audio]",
        "-map",
        "0:s:0",
        "-t",
        "60",
        "-c:v",
        "copy",
        "-c:s",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-metadata",
        "comment=功能示意与示例数据；八段中文旁白均由本机 F5-TTS（MLX）生成；非真实任务录屏。",
        str(video),
    ]
    run(command)
    verify_video(video)
    shutil.copyfile(video, MEDIA / "rocky-intro.mp4")
    shutil.copyfile(work / "voice-generation.json", MEDIA / "voice-generation.json")
    print("Replaced the narration with F5-TTS; copied existing video/subtitle streams.")


def build(work, frames_only=False, audio_only=False, tts_python=sys.executable):
    if len(STORYBOARD) != 8 or STARTS != [0, *ENDS[:-1]] or ENDS[-1] != 60:
        raise ValueError("The tour needs eight contiguous scenes totaling 60 seconds")
    if audio_only:
        replace_audio(work, tts_python)
        return
    modules = work / "modules"
    modules.mkdir(exist_ok=True)
    environment = {**os.environ, "CLANG_MODULE_CACHE_PATH": str(modules)}
    run(
        [
            "swift",
            "-module-cache-path",
            str(modules),
            str(ROOT / "scripts/render_readme_media.swift"),
            str(work),
            str(ROOT),
        ],
        env=environment,
    )
    if frames_only:
        print(f"Storyboard PNGs: {work}")
        return
    srt = work / "rocky-intro.srt"
    srt.write_text(
        "\n\n".join(
            f"{i + 1}\n{timestamp(start + 0.4)} --> {timestamp(end - 0.15)}\n{caption}"
            for i, (start, end, caption) in enumerate(zip(STARTS, ENDS, CAPTIONS, strict=True))
        )
        + "\n",
        encoding="utf-8",
    )
    synthesize_narration(work, tts_python)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", "2"]
    for i in range(8):
        command += ["-i", str(work / f"scene-{i:02}.png")]
    for i in range(8):
        command += ["-i", str(work / f"voice-{i}.wav")]
    command += ["-i", str(srt)]
    filters = []
    for i, (start, end) in enumerate(zip(STARTS, ENDS, strict=True)):
        duration = end - start + (0.4 if i < 7 else 0)
        frames = math.ceil(duration * 30)
        filters.append(
            f"[{i}:v]zoompan=z='1.003+0.010*on/{frames - 1}':d={frames}:"
            f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=1280x720:fps=30,"
            f"format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[v{i}]"
        )
    previous = "v0"
    for i in range(1, 8):
        name = f"transition{i}"
        filters.append(
            f"[{previous}][v{i}]xfade=transition=fade:duration=0.4:offset={STARTS[i]}[{name}]"
        )
        previous = name
    filters.extend(audio_filters(work, 8))
    video = work / "rocky-intro.mp4"
    command += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        f"[{previous}]",
        "-map",
        "[audio]",
        "-map",
        "16:s:0",
        "-t",
        "60",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-c:s",
        "mov_text",
        "-metadata:s:s:0",
        "language=zho",
        "-movflags",
        "+faststart",
        "-metadata",
        "title=Rocky v0.2 — 从目标到交付",
        "-metadata",
        "comment=功能示意与示例数据；八段中文旁白均由本机 F5-TTS（MLX）生成；非真实任务录屏。",
        str(video),
    ]
    run(command)
    build_preview(work)
    verify_video(video)
    # Copy only finished products; intermediate images/audio stay outside the repository.
    MEDIA.mkdir(exist_ok=True)
    for name in ("rocky-intro.mp4", "rocky-intro.gif", "rocky-intro.srt", "voice-generation.json"):
        shutil.copyfile(work / name, MEDIA / name)
    shutil.copyfile(work / "scene-00.png", MEDIA / "rocky-v0.2-overview.png")
    print("Published local media files to docs/media.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir", type=Path, help="Keep storyboard frames and audio for visual QA"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--frames-only", action="store_true", help="Render PNGs without encoding media"
    )
    modes.add_argument(
        "--audio-only", action="store_true", help="Replace narration while copying video/subtitles"
    )
    parser.add_argument(
        "--tts-python",
        type=Path,
        default=Path(sys.executable),
        help="Python from the optional F5-TTS environment",
    )
    args = parser.parse_args()
    if args.frames_only and not args.work_dir:
        parser.error("--frames-only requires --work-dir")
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        build(args.work_dir.resolve(), args.frames_only, args.audio_only, args.tts_python)
    else:
        with tempfile.TemporaryDirectory(prefix="rocky-readme-") as directory:
            build(Path(directory), tts_python=args.tts_python, audio_only=args.audio_only)
