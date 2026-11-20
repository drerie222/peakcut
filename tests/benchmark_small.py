"""Offline benchmark for the small-server profile, using an installed Parakeet model."""
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")


def live_benchmark(root, source):
    import httpx

    from studio import ai, auth, candidates, config, creators, ctx, jobs, live, media

    capture = root / "capture"
    capture.mkdir()
    media.run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-filter_complex_threads", "1",
               "-filter_complex", "[0:a:0][0:a:1]amix=inputs=2:weights='0.15 1'[mix]",
               "-map", "0:v", "-map", "[mix]", "-c:v", "copy", "-c:a", "aac", "-f", "segment",
               "-segment_time", "6", "-reset_timestamps", "1", "-segment_list", "p01.csv",
               "-segment_list_type", "csv", "p01_%05d.ts"], cwd=capture, timeout=30)
    segments = [(capture / name, float(a), float(b))
                for line in (capture / "p01.csv").read_text().splitlines() for name, a, b in [line.split(",")]]
    owner = auth.local_user()
    decision = dict(kind="conversation", evidence=["speech", "frame"], score=94, start=2, end=61,
                    setup_at=5, payoff_at=46, setup="A conversation starts during play",
                    payoff="Its final remark resolves the conversation")
    response = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(decision)}]}}],
                                        "usageMetadata": {"promptTokenCount": 850, "totalTokenCount": 980}})
    with (ctx.as_user(owner.id), patch.dict(os.environ, PEAKCUT_AI_KEY="local-mock-only"),
          patch.object(config, "AI_PROVIDER", "gemini"), patch.object(config, "AI_MODELS", ["gemini-3.6-flash"]),
          patch.object(config, "LIVE_HIGHLIGHT_REQUESTS", 1), patch.object(jobs, "try_submit_live", return_value=False),
          patch.object(ai.httpx, "post", return_value=response) as gateway):
        job = jobs.create({"type": "live", "name": "Synthetic quiet GTA VI fixture"}, {}, enqueue=False, kind="live")
        session = live.Session(job, {**creators.DEFAULTS, "game": "gta6"})
        session.segments, session.t_done = segments, 66
        session.db = [-36] * 66
        session._watch_spikes()
        session._highlight_step(draining=True)
        assert session.spike is None
        entry = job["candidates"][0]
        retained = candidates._segments(job, entry)
        assert retained[0][0].stat().st_ino == segments[0][0].stat().st_ino
        candidates._review(job, entry)
        assert entry["state"] == "accepted" and entry["speech_state"] == "available", entry.get("reason")
        inputs = (candidates._folder(job, entry) / "review.jpg").stat().st_size
        clip = job["clips"][0]
        candidates._render(job, entry, clip)
        assert entry["state"] == "rendered" and not clip["quality"]["issues"], entry.get("reason")
        output = media.probe(Path(job["dir"]) / "clips" / clip["file"])
        assert clip["speech_source"] == "mixed" and clip["layout"] == "full_gameplay"
        assert (output["width"], output["height"]) == (1080, 1920)
        assert gateway.call_count == job["pipeline"]["live_ai_requests"] == 1
        return dict(pipeline=job["pipeline"], source_quality=media.recording_quality(source), output=output,
                    gameplay_region=clip["gameplay_region"], source_segment_bytes=sum(p.stat().st_size for p, _, _ in segments),
                    frame_sheet_bytes=inputs, mocked_http_attempts=gateway.call_count, mixed_captions=clip["speech_source"])


def main(root):
    from studio import config, db, transcribe

    speech = Path(sys.argv[1]) if len(sys.argv) > 1 else config.ASR_MODEL_DIR / "test_wavs" / "0.wav"
    if not transcribe.model_ready() or not speech.is_file():
        raise SystemExit("Provide the installed Parakeet model and a small local WAV speech sample.")
    started = time.monotonic()
    db.init()
    source = root / "synthetic-gaming.mp4"
    hd = os.environ.get("PEAKCUT_BENCH_1080P60") == "1"
    input_video = "testsrc2=s=1920x1080:r=60" if hd else "testsrc2=s=640x360:r=12"
    simulate_live = os.environ.get("PEAKCUT_BENCH_LIVE_HIGHLIGHT") == "1"
    if simulate_live:
        input_video += ",hue=H=2*t"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", input_video,
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-stream_loop", "-1", "-i", str(speech),
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "66", "-c:v", "libx264", "-threads", "1",
                    "-preset", "ultrafast", "-c:a", "aac", str(source)], timeout=180, check=True)
    if simulate_live:
        result = live_benchmark(root, source)
    else:
        result = video_benchmark(source, hd)
    result.update(elapsed_s=round(time.monotonic() - started, 1), input_s=66,
                  process_address_space_bytes=resource.getrlimit(resource.RLIMIT_AS)[0],
                  parent_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  child_peak_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    for name in ("memory.max", "memory.peak", "cpu.max"):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            result[name] = path.read_text().strip()
    print(json.dumps(result, indent=2))


def video_benchmark(source, hd):
    from studio import ctx, jobs, media

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
    return {"output": output, "words": job["word_count"], "source_quality": media.recording_quality(source),
            "gameplay_region": clip["gameplay_region"], "render_seconds": job.get("pipeline", {}).get("render_seconds"),
            "draft_hold_reasons": clip["quality"]["issues"]}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="peakcut-small-") as work:
        # Keep the entire run outside user data and off every remote service.
        os.environ.update(PEAKCUT_DATA=work, DATABASE_URL="", PEAKCUT_AI_KEY="", PEAKCUT_AI_KEY_FILE="",
                          PEAKCUT_AI_MODELS="", PEAKCUT_ASR_URL="", PEAKCUT_UPDATE_CHECK="0",
                          BUFFER_API_KEY="", DISCORD_CLIPS_WEBHOOK_URL="", DISCORD_LIVE_WEBHOOK_URL="",
                          DISCORD_STATUS_WEBHOOK_URL="", PEAKCUT_LOW_RESOURCE="1")
        main(Path(work))
