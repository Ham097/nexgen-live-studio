"""Benchmark reproducible synthetic fixtures separately from real learner accuracy."""
from argparse import ArgumentParser
import json
from pathlib import Path
import platform
from statistics import median
from time import perf_counter
import wave

import numpy as np
import soxr
from studio.analysis import tokens
from studio.asr import Recognizer


def read_audio(path):
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise ValueError("Use a 16-bit PCM WAV file.")
        raw = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        pcm = raw.reshape(-1, audio.getnchannels()).mean(axis=1, dtype=np.float32) / 32768
        return soxr.resample(pcm, audio.getframerate(), 16000)


def distance(reference, prediction):
    previous = list(range(len(prediction) + 1))
    for index, expected in enumerate(reference, 1):
        current = [index]
        for column, actual in enumerate(prediction, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (actual != expected)))
        previous = current
    return previous[-1]


def main():
    arguments = ArgumentParser()
    arguments.add_argument("fixtures", type=Path)
    arguments.add_argument("--cache", type=Path, default=Path("private/models"))
    arguments.add_argument("--existing", type=Path)
    arguments.add_argument("--models", nargs="+", default=["base.en", "large-v3-turbo", "distil-large-v3"])
    arguments.add_argument("--output", type=Path)
    args = arguments.parse_args()
    fixtures = [(file.stem, read_audio(file), file.with_suffix(".txt").read_text(encoding="utf-8-sig"))
                for file in sorted(args.fixtures.glob("*.wav"))]
    if not fixtures:
        arguments.error("No WAV fixtures found.")
    fixtures.append(("silence", np.zeros(48000, dtype=np.float32), ""))
    results = []
    for model in args.models:
        place = "cpu" if model == "base.en" else "auto"
        engine = Recognizer(model, place, str(args.cache), args.existing, download=model == "base.en")
        engine.decode(fixtures[0][1])
        cases = []
        for name, audio, reference in fixtures:
            elapsed, text = [], ""
            for _ in range(3):
                start = perf_counter()
                output = engine.decode(audio)
                elapsed.append(perf_counter() - start)
                text = " ".join(segment[0] for segment in output)
            errors = distance(tokens(reference), tokens(text))
            cases.append({"fixture": name, "audio_seconds": round(len(audio) / 16000, 3),
                          "median_seconds": round(median(elapsed), 3),
                          "word_errors": errors, "reference_words": len(tokens(reference)),
                          "recognized_words": len(tokens(text)), "text": text})
        results.append({"model": model, "device": engine.device, "load_seconds": engine.load_seconds, "cases": cases})
    result = json.dumps({"audio": "Windows SAPI synthetic speech and digital silence",
                         "system": platform.system(), "processor": platform.processor(), "runs": results}, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + "\n", encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
