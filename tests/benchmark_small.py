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
    hd = os.environ.get("PEAKCUT_BENCH_1080P60") == "1"
    input_video = "testsrc2=s=1920x1080:r=60" if hd else "testsrc2=s=640x360:r=12"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", input_video,
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-stream_loop", "-1", "-i", str(speech),
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "66", "-c:v", "libx264", "-threads", "1",
                    "-preset", "ultrafast", "-c:a", "aac", str(source)], timeout=180, check=True)
    with ctx.as_user("benchmark"):
        job = jobs.create({"type": "file", "name": "Synthetic GTA VI fixture", "path": str(source)},
                          {"max_clips": 1, "min_len": 50, "max_len": 61}, enqueue=False)
        job["game_mic_track"] = 2
        jobs._run_job(job)
    clip = next((c for c in job["clips"] if c.get("status") == "done"), None)
    if clip is None:
        raise SystemExit("No completed clip.")
    output = media.probe(Path(job["dir"]) / "clips" / clip["file"])
    assert (output["width"], output["height"]) == (1080, 1920)
    assert job["word_count"] > 8 and output["duration"] >= 49
    assert clip["speech_source"] == "isolated_mic"
    assert clip["layout"] == "full_gameplay" and clip["gameplay_region"][2:] == ((1080, 608) if hd else (640, 360))
    assert not (Path(job["dir"]) / "audio.wav").exists()
    result = {"elapsed_s": round(time.monotonic() - started, 1), "input_s": 66, "output": output,
              "words": job["word_count"], "source_quality": media.recording_quality(source),
              "gameplay_region": clip["gameplay_region"], "render_seconds": job.get("pipeline", {}).get("render_seconds"),
              "draft_hold_reasons": clip["quality"]["issues"],
              "process_address_space_bytes": resource.getrlimit(resource.RLIMIT_AS)[0],
              "parent_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
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
