"""A speech transcript identifies words, not who spoke them. Trust only a declared isolated track."""
import re
import tempfile
from pathlib import Path

from . import media, transcribe

_QUOTE = re.compile(r"""["“”«»]|(?:^|\s)['‘][^\n]+['’](?:\s|$)|\b(?:I|ME|MY|MINE|WE|OUR)\b""", re.I)


def protected(job, clip):
    return bool(job.get("watch_broadcast") or job.get("game") or clip.get("gameplay_source")
                or clip.get("layout") == "gameplay" or job.get("options", {}).get("layout") == "gameplay")


def declared_track(value):
    return value if type(value) is int and 2 <= value <= 8 else None


def captions(job, clip, src, offset):
    """Read the declared mic stream from this exact cut. FFmpeg fails if that stream is missing."""
    track = (clip.get("mic_track") if job.get("kind") == "live" else
             job.get("game_mic_track") if job.get("source", {}).get("type") == "file" else None)
    if not declared_track(track):
        return [], "mixed"
    start, end = clip["start"] - offset, clip["end"] - offset
    try:
        with tempfile.TemporaryDirectory(prefix="pc-mic-") as tmp:
            wav = Path(tmp) / "mic.wav"
            media.extract_audio(src, wav, track=track, start=start, seconds=end - start)
            words = transcribe.transcribe(media.load_audio(wav))
    except Exception:  # audio unavailable: never fall back to audio stream 1 or platform subtitles
        return [], "mic_unavailable"
    words = [dict(w, s=w["s"] + clip["start"], e=w["e"] + clip["start"])
             for w in words if 0 <= w["s"] < end - start]
    return words, "isolated_mic"


def hook(clip):
    """An action heading may describe a scene. It must not purport to quote an unverified speaker."""
    text = (clip.get("hook") or "").strip()
    if _QUOTE.search(text) or text.upper() in ("WAIT FOR IT", "YOU WON'T BELIEVE THIS"):
        return ""
    return " ".join(text.split()[:5])
