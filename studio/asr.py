"""Local inference, model selection and quantized fallback."""
import os
from pathlib import Path
import re
import site
from time import perf_counter

import numpy as np

MODELS = {
    "base.en": ("Systran/faster-whisper-base.en", "3d3d5dee26484f91867d81cb899cfcf72b96be6c"),
    "large-v3-turbo": ("mobiuslabsgmbh/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"),
    "distil-large-v3": ("Systran/faster-distil-whisper-large-v3", "c3058b475261292e64a0412df1d2681c06260fab"),
}
_DLLS = []


def prepare_cuda():
    if os.name == "nt":
        for site_packages in site.getsitepackages():
            for path in Path(site_packages, "nvidia").glob("*/bin"):
                if path.is_dir():
                    if str(path) not in _DLLS:
                        _DLLS.append(str(path))
                        _DLLS.append(os.add_dll_directory(str(path)))
                        os.environ["PATH"] = str(path) + os.pathsep + os.environ["PATH"]


def model_path(model, cache, existing=None, download=False):
    from huggingface_hub import snapshot_download
    repo, revision = MODELS[model]
    for root in (existing, cache):
        if not root:
            continue
        path = Path(root)
        candidate = path / ("models--" + repo.replace("/", "--")) / "snapshots" / revision
        if (candidate / "model.bin").is_file() and (candidate / "tokenizer.json").is_file():
            return str(candidate)
    return snapshot_download(repo, revision=revision, cache_dir=cache, local_files_only=not download, token=False,
                             allow_patterns=["*.json", "model.bin", "vocabulary.*"])


def repetitive(text):
    words = re.findall(r"[a-z']+", text.lower())
    return any(span * 4 <= len(words) and any(
        words[i:i + span] * 4 == words[i:i + span * 4] for i in range(len(words) - span * 4 + 1)
    ) for span in range(1, 7))


class Recognizer:
    def __init__(self, model, device, cache, existing=None, download=False):
        from faster_whisper import WhisperModel
        prepare_cuda()
        path = model_path(model, cache, existing, download)
        start = perf_counter()
        selected = ("cuda", "cpu") if device == "auto" else (device,)
        for placement in selected:
            try:
                engine = WhisperModel(path, device=placement,
                                      compute_type="int8_float16" if placement == "cuda" else "int8",
                                      cpu_threads=6, local_files_only=True)
                # Exercise GPU libraries at startup, including lazy loading in CTranslate2.
                mel_count = engine.feature_extractor.mel_filters.shape[0]
                engine.encode(np.zeros((mel_count, engine.feature_extractor.nb_max_frames), dtype=np.float32))
            except (RuntimeError, ValueError, OSError):
                if placement == selected[-1]:
                    raise
            else:
                self.engine, self.device, self.model = engine, placement, model
                break
        self.load_seconds = round(perf_counter() - start, 3)

    def decode(self, audio):
        segments, _ = self.engine.transcribe(
            audio, language="en", beam_size=1, vad_filter=True, word_timestamps=True,
            condition_on_previous_text=False, temperature=0, no_speech_threshold=0.6,
            log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
            vad_parameters={"min_speech_duration_ms": 100, "min_silence_duration_ms": 280,
                            "speech_pad_ms": 120},
        )
        result = []
        for segment in segments:
            words = [{"text": word.word.strip(), "start_ms": round(word.start * 1000),
                      "end_ms": round(word.end * 1000), "probability": round(word.probability, 3)}
                     for word in segment.words or [] if word.word.strip()]
            if not words:
                continue
            flags = []
            if repetitive(segment.text):
                flags.append("repetition to review")
            if (sum(word["probability"] for word in words) / len(words) < .52
                    or segment.avg_logprob < -1.0 or segment.no_speech_prob > .65):
                flags.append("low recognition confidence")
            result.append((segment.text.strip(), words, flags))
        return result
