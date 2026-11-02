"""Generate all README narration locally with F5-TTS, in an optional MLX environment."""

import argparse
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 24_000
HOP_LENGTH = 256
MODEL = "lucasnewman/f5-tts-mlx"


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def synthesize(output, reference_audio, reference_text):
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    from f5_tts_mlx.cfm import F5TTS
    from f5_tts_mlx.utils import convert_char_to_pinyin
    from huggingface_hub.constants import HF_HUB_CACHE

    reference, rate = sf.read(reference_audio, dtype="float32")
    if rate != SAMPLE_RATE or reference.ndim != 1 or not 4 <= len(reference) / rate <= 12:
        raise ValueError("F5 reference must be mono, 24 kHz, and 4–12 seconds long")
    transcript = reference_text.read_text(encoding="utf-8").strip()
    if not transcript:
        raise ValueError("Reference transcript is empty")
    rms = float(np.sqrt(np.mean(reference**2)))
    if rms < 0.001:
        raise ValueError("Reference audio is silent")
    if rms < 0.1:
        reference = reference * (0.1 / rms)
    condition = mx.array(reference)[None, :]
    model = F5TTS.from_pretrained(MODEL, quantization_bits=8)
    storyboard = json.loads((ROOT / "scripts/readme_storyboard.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for i, scene in enumerate(storyboard):
        narrative = scene.get("narration", scene["caption"])
        target_seconds = scene["end"] - scene["start"] - 1.4
        total_frames = round((len(reference) / rate + target_seconds) * rate / HOP_LENGTH)
        text = convert_char_to_pinyin([transcript + " " + narrative])
        print(f"F5-TTS: synthesizing scene {i + 1}/{len(storyboard)}", flush=True)
        wave, trajectory = model.sample(
            condition,
            text=text,
            duration=total_frames,
            steps=32,
            method="euler",
            cfg_strength=2.0,
            sway_sampling_coef=-1.0,
            seed=20261102 + i,
        )
        wave = wave[len(reference) :]
        mx.eval(wave)
        audio = np.asarray(wave, dtype=np.float32)
        if not np.isfinite(audio).all() or float(np.sqrt(np.mean(audio**2))) < 0.001:
            raise ValueError(f"F5-TTS returned invalid or silent audio for scene {i + 1}")
        # Reserve headroom without changing timing or introducing clipping in PCM.
        peak = float(np.max(np.abs(audio)))
        if peak > 0.89:
            audio = audio * (0.89 / peak)
        path = output / f"voice-{i}.wav"
        sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")
        records.append(
            {
                "scene": i + 1,
                "text": narrative,
                "seed": 20261102 + i,
                "duration_seconds": round(len(audio) / SAMPLE_RATE, 6),
                "sha256": digest(path),
            }
        )
        print(f"F5-TTS: scene {i + 1} complete ({len(audio) / rate:.2f}s)", flush=True)
        del wave, trajectory, audio

    revisions = {}
    for repository in (MODEL, "lucasnewman/vocos-mel-24khz"):
        ref = Path(HF_HUB_CACHE) / ("models--" + repository.replace("/", "--")) / "refs/main"
        if ref.is_file():
            revisions[repository] = ref.read_text().strip()
    manifest = {
        "engine": "F5-TTS",
        "implementation": "f5-tts-mlx",
        "model": MODEL,
        "model_revisions": revisions,
        "packages": {name: version(name) for name in ("f5-tts-mlx", "mlx", "vocos-mlx")},
        "quantization_bits": 8,
        "steps": 32,
        "method": "euler",
        "sample_rate": SAMPLE_RATE,
        "reference": {
            "file": reference_audio.name,
            "sha256": digest(reference_audio),
            "transcript": transcript,
            "origin": "Synthetic macOS Tingting narration from the previous README video; no human recording.",
        },
        "scenes": records,
    }
    (output / "voice-generation.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reference-audio", type=Path, required=True)
    parser.add_argument("--reference-text", type=Path, required=True)
    args = parser.parse_args()
    synthesize(args.output_dir, args.reference_audio, args.reference_text)
