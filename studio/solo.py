"""A local speaker check and known speech input for the offline recognizer."""
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import wave
import winsound

import numpy as np

SENTENCE = "Hello. This is a NexGen Live Studio sound check."


def reference(root, recognizer):
    with TemporaryDirectory(prefix="nexgen-sound-check-") as temporary:
        path = Path(temporary) / "reference.wav"
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(root / "scripts" / "reference.ps1"), str(path), SENTENCE],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        with wave.open(str(path), "rb") as speech:
            audio = np.frombuffer(speech.readframes(speech.getnframes()), dtype="<i2").astype(np.float32) / 32768
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        return " ".join(text for text, _, _ in recognizer.decode(audio))
