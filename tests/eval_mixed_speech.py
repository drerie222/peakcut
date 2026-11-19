"""Offline check of stereo subtraction and ASR on two synthetic, centre-panned voices."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, ".")


def read_voice(root, name, voice, text):
    wav = root / f"{name}.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"flite=voice={voice}:text={text}",
                    "-ar", "16000", "-ac", "1", str(wav)], check=True, timeout=15)
    result, _ = sf.read(wav, dtype="float32")
    return result


with tempfile.TemporaryDirectory(prefix="pc-mix-eval-") as work:
    os.environ.update(PEAKCUT_DATA=work, PEAKCUT_ASR_URL="", PEAKCUT_AI_KEY="", PEAKCUT_AI_KEY_FILE="")
    from studio import transcribe

    root = Path(work)
    streamer = read_voice(root, "streamer", "slt", "That jump was close. We made it across.")
    game = read_voice(root, "game", "rms", "Drive to the garage. The next checkpoint is ahead.")
    delay = int(1.4 * 16000)
    size = max(len(streamer), len(game) + delay)
    streamer = np.pad(streamer, (0, size - len(streamer)))
    game = np.pad(game, (delay, size - delay - len(game)))
    left = right = .5 * (streamer + game)
    print("Side channel peak:", abs(left - right).max())
    print("Swapping role labels leaves the mix equal:", np.array_equal(left, .5 * (game + streamer)))
    if transcribe.model_ready():
        pcm = np.rint(left * 32767).astype(np.int16)
        words = transcribe.transcribe(pcm)
        print("Mixed ASR:", " ".join(word["w"] for word in words))
