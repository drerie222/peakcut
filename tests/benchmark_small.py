"""Offline benchmark for the small-server profile, using an installed Parakeet model."""
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")


def main(root):
    from studio import config, ctx, db, jobs, media, transcribe

    speech = Path(sys.argv[1]) if len(sys.argv) > 1 else config.ASR_MODEL_DIR / "test_wavs" / "0.wav"
    if not transcribe.model_ready() or not speech.is_file():
        raise SystemExit("Provide the installed Parakeet model and a small local WAV speech sample.")
    started = time.monotonic()
    db.init()
    source = root / "synthetic-gaming.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=12",
                    "-stream_loop", "-1", "-i", str(speech), "-t", "66", "-c:v", "libx264", "-threads", "1",
                    "-preset", "ultrafast", "-c:a", "aac", str(source)], timeout=90, check=True)
    with ctx.as_user("benchmark"):
        job = jobs.create({"type": "file", "name": "Synthetic GTA VI fixture", "path": str(source)},
                          {"max_clips": 1, "min_len": 50, "max_len": 61, "layout": "gameplay"}, enqueue=False)
        jobs._run_job(job)
    clip = next((c for c in job["clips"] if c.get("status") == "done"), None)
    if clip is None:
        raise SystemExit("No completed clip.")
    output = media.probe(Path(job["dir"]) / "clips" / clip["file"])
    assert (output["width"], output["height"]) == (1080, 1920)
    assert job["word_count"] > 8 and output["duration"] >= 49
    assert not (Path(job["dir"]) / "audio.wav").exists()
    result = {"elapsed_s": round(time.monotonic() - started, 1), "input_s": 66, "output": output,
              "words": job["word_count"], "parent_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "child_peak_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}
    for name in ("memory.max", "memory.peak", "cpu.max"):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            result[name] = path.read_text().strip()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="peakcut-small-") as work:
        # Keep the entire run outside user data and off every remote service.
        os.environ.update(PEAKCUT_DATA=work, DATABASE_URL="", PEAKCUT_AI_KEY="", PEAKCUT_AI_KEY_FILE="",
                          PEAKCUT_AI_MODELS="", PEAKCUT_ASR_URL="", PEAKCUT_UPDATE_CHECK="0",
                          BUFFER_API_KEY="", DISCORD_CLIPS_WEBHOOK_URL="", DISCORD_LIVE_WEBHOOK_URL="",
                          DISCORD_STATUS_WEBHOOK_URL="", PEAKCUT_LOW_RESOURCE="1")
        main(Path(work))
