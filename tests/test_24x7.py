"""Quota responses, delayed highlights, capture selection and restart recovery."""
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import yt_dlp

work = tempfile.TemporaryDirectory(prefix="pc-24x7-")
os.environ.update(PEAKCUT_DATA=work.name, DATABASE_URL="", PEAKCUT_AI_PROVIDER="gemini", PEAKCUT_AI_MODELS="first,second",
                  PEAKCUT_AI_KEY="mock-quota-key", BUFFER_API_KEY="", PEAKCUT_LOW_RESOURCE="1")
sys.path.insert(0, ".")
from studio import ai, ai_limits, auth, brain, buffer, candidates, config, creators, ctx, db, jobs, live, load, media

failures = []


def check(name, value):
    print(("PASS " if value else "FAIL ") + name, flush=True)
    if not value:
        failures.append(name)


@dataclasses.dataclass
class Clock:
    now: float = 1763658000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def limited(metric, retry="0s"):
    return httpx.Response(429, headers={"Retry-After": "42"}, json={"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": metric}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
    ], "message": "sensitive mock payload"}})


def caught(fn):
    try:
        fn()
    except ai_limits.Deferred as exc:
        return exc
    return None


def test_limits():
    clock = Clock()
    check("Gemini TPM and RetryInfo are classified without exposing the body",
          ai_limits.classify(limited("InputTokensPerMinute", "60s"), clock.now) == ("provider_tokens", 60.0))
    header_date = "Thu, 20 Nov 2025 19:00:00 GMT"
    result = ai_limits.classify(httpx.Response(429, headers={"Retry-After": header_date}), clock.now)
    check("Retry-After supports HTTP-date as well as seconds", result[1] > 0)
    with patch.object(ai_limits, "time", clock), patch.object(ai_limits.random, "uniform", return_value=.5):
        gate = ai_limits.Limiter()
        def send(gate, model, fn):
            return gate.request("gemini", "mock", "key", model, 10, fn)

        with patch.object(config, "AI_RPD", 0):
            calls = []
            def quota():
                calls.append(1)
                return limited("GenerateRequestsPerDayPerProjectPerModel-FreeTier")
            first = caught(lambda: send(gate, "test", quota))
            after_restart = caught(lambda: send(ai_limits.Limiter(), "test", quota))
            check("daily exhaustion survives restart without repeating a request",
                  first and after_restart and first.reason == "daily_quota" and len(calls) == 1
                  and first.retry_at == after_restart.retry_at)
            check("a fallback model with a separate configured quota can answer",
                  send(gate, "other", lambda: httpx.Response(200)).status_code == 200)
            clock.now = first.retry_at + 1
            check("a daily quota can recover after the provider resets",
                  send(ai_limits.Limiter(), "test", lambda: httpx.Response(200)).status_code == 200)
        gate = ai_limits.Limiter()
        with patch.object(config, "AI_RPM", 1), patch.object(config, "AI_RPD", 0):
            send(gate, "paced", lambda: httpx.Response(200))
            held = caught(lambda: send(gate, "paced", lambda: httpx.Response(200)))
            check("a local rolling request cap leaves time to retry later", held and held.reason == "local_rate")
            clock.now += 61
            check("the rolling minute allows new requests automatically",
                  send(gate, "paced", lambda: httpx.Response(200)).status_code == 200)
        with patch.object(config, "AI_TPM", 15), patch.object(config, "AI_RPD", 0):
            gate = ai_limits.Limiter()
            send(gate, "tokens", lambda: httpx.Response(200))
            held = caught(lambda: send(gate, "tokens", lambda: httpx.Response(200)))
            check("estimated tokens use the same rolling admission budget", held and held.reason == "local_rate")
        with patch.object(config, "AI_RPD", 1):
            gate = ai_limits.Limiter()
            send(gate, "daily-local", lambda: httpx.Response(200))
            held = caught(lambda: send(ai_limits.Limiter(), "daily-local", lambda: httpx.Response(200)))
            check("a configured local request budget survives restart", held and held.reason == "local_daily_budget")
        gate = ai_limits.Limiter()
        responses = iter((httpx.Response(503), httpx.Response(200)))
        start = clock.now
        check("one short transient retry uses exponential delay plus jitter",
              send(gate, "transient", lambda: next(responses)).status_code == 200 and clock.now - start == 2.5)

    gate = ai_limits.Limiter()
    first, release, second = threading.Event(), threading.Event(), threading.Event()
    def hold():
        first.set()
        release.wait(3)
        return httpx.Response(200)
    a = threading.Thread(target=lambda: gate.request("gemini", "test", "key", "serial", 10, hold))
    b = threading.Thread(target=lambda: gate.request("gemini", "test", "key", "serial2", 10,
                                                    lambda: (second.set(), httpx.Response(200))[1]))
    a.start()
    first.wait(1)
    b.start()
    check("requests for different models share a single in-flight slot", not second.wait(.2))
    release.set()
    a.join(2)
    b.join(2)
    check("the waiting caller proceeds when the first model releases the slot",
          second.is_set() and gate.status().get("queue_wait_ms", 0) >= 100)


def test_gemini(owner):
    def reply(payload):
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": payload}]}}],
                                         "usageMetadata": {"promptTokenCount": 50, "totalTokenCount": 60}})
    gate = ai_limits.Limiter()
    with (ctx.as_user(owner.id), patch.object(ai_limits, "limiter", gate),
          patch.object(ai.httpx, "post", side_effect=[limited("RequestsPerDayPerModel"), reply("[]"), reply("{}")]) as http):
        answer, name, tokens = brain.ask_ai("test quota fallback")
        check("text falls back only to a configured model", answer == [] and name == "second" and tokens == 60)
        answer, name, tokens = brain.ask_ai("test quota fallback")
        check("success is reused without recording extra tokens or requests", answer == [] and name == "second"
              and tokens == 0 and http.call_count == 2)
        result, _, _ = ai.see("different prompt", b"jpeg")
        check("vision also respects the existing block and uses the configured fallback",
              result == "{}" and http.call_count == 3 and http.call_args.args[0].endswith("/second:generateContent"))
        check("service counters distinguish successes and 429 responses",
              gate.status().get("rate_errors") == 1 and gate.status().get("successes") == 2
              and "mock-quota-key" not in json.dumps(gate.status()))


def test_capture(root):
    actual = yt_dlp.YoutubeDL
    formats = [(480, 30), (720, 60), (1080, 30), (1080, 60), (1080, 120), (1440, 60)]
    class Extractor:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            data = {"id":"synthetic", "title":"Test", "webpage_url":url, "is_live":True, "extractor":"fixture",
                    "formats":[{"format_id": f"{h}-{fps}", "ext":"mp4", "url": f"https://example.test/{h}-{fps}",
                                "height":h, "width":round(h*16/9), "fps":fps, "vcodec":"avc1", "acodec":"aac"}
                               for h,fps in formats]}
            with actual({**self.opts, "simulate": True}) as ydl:
                return ydl.process_video_result(data, download=False)

    with patch.object(yt_dlp, "YoutubeDL", Extractor), patch.object(media, "youtube_paused", return_value=False):
        urls = ["https://www.twitch.tv/game", "https://www.youtube.com/@game/live", "https://kick.com/game"]
        check("Twitch, YouTube and Kick prefer 1080p60 over 1080p30 or higher capped formats",
              all(live.resolve(url)["urls"] == ["https://example.test/1080-60"] for url in urls))
        check("a failed connection can use the lower 720p ceiling",
              live.resolve(urls[0], 720)["urls"] == ["https://example.test/720-60"])
        formats[:] = [(480, 30)]
        check("a source with only a lower rendition still plays", live.resolve(urls[2])["urls"] == ["https://example.test/480-30"])

    path = root / "p01_00001.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=1920x1080:r=60", "-f", "lavfi", "-i",
                    "sine=frequency=400:sample_rate=48000", "-t", "0.6", "-threads", "1", "-c:v", "libx264", "-preset",
                    "ultrafast", "-c:a", "aac", str(path)], check=True, timeout=20)
    quality = media.recording_quality(path)
    check("ffprobe reports actual recorded size, FPS, bitrate and audio", quality["height"] == 1080 and quality["fps"] == 60
          and quality["bitrate"] > 0 and quality["audio_hz"] == 48000 and quality["audio_codec"] == "aac")
    return path


def test_candidates(owner, source):
    with ctx.as_user(owner.id), patch.object(load, "disk_full", return_value=False), patch.object(load, "render_disk_full", return_value=False):
        settings = dict(creators.DEFAULTS, layout="gameplay", min_gap_min=0)
        job = jobs.create({"type":"live", "name":"Fixture", "url":"https://www.twitch.tv/test"}, {}, kind="live", enqueue=False)
        job.update(status="live", watch_broadcast="test-broadcast")
        segments = [(source, 7100, 7250)]
        with patch.object(brain, "judge_gameplay") as judge:
            first = candidates.enqueue(job, settings, segments, 7110, 7160, 7140, "Jump: loudness")
            overlap = candidates.enqueue(job, settings, segments, 7112, 7162, 7142, "Jump: chat")
            second = candidates.enqueue(job, settings, segments, 7180, 7230, 7220, "Jump: chat")
            check("a long recording retains distinct moments and rejects a close duplicate without calling AI",
                  first and not overlap and second and not judge.called)
        path = Path(job["dir"]) / "job.json"
        first, second = job["candidates"]
        saved_file = candidates._segments(job, first)[0][0]
        check("saved candidate footage uses a hard link", saved_file.stat().st_ino == source.stat().st_ino)
        source.unlink()
        jobs._load_file(path, owner.id)
        reloaded = jobs.get(job["id"])
        first, second = reloaded["candidates"]
        check("the candidate remains playable and its source timing survives reload and rolling cleanup",
              candidates._segments(reloaded, first)[0][0].is_file() and first["start"] == 7110)
        now = time.time()
        with (patch.object(config, "ai_ready", return_value=True),
              patch.object(facecam := candidates.facecam, "frame_strip", return_value=b"mock frames"),
              patch.object(brain, "judge_gameplay", side_effect=ai_limits.Deferred("daily_quota", now + 3600)) as judge):
            candidates.run_round()
            check("a 429 defers the candidate until the reported reset without consuming failure retries",
                  first["state"] == "waiting" and first["due"] >= now + 3600 and first["attempts"] == 0 and judge.call_count == 1)
        with (patch.object(config, "ai_ready", return_value=True), patch.object(facecam, "frame_strip", return_value=b"mock frames"),
              patch.object(brain, "judge_gameplay", return_value=(brain._tidy({"score":95}, 7180, 7230), 100)),
              patch.object(jobs, "try_submit_live", return_value=False) as submit):
            candidates.run_round()
            check("another candidate is accepted while the first waits and a full render queue retains the accepted clip",
                  second["state"] == "accepted" and reloaded["clips"][0]["status"] == "queued" and submit.called)
        jobs._load_file(path, owner.id)
        reloaded = jobs.get(job["id"])
        second = reloaded["candidates"][1]
        check("an accepted clip remains queued across restart without repeating review",
              second["state"] == "accepted" and reloaded["clips"][0]["status"] == "queued")
        second["state"] = "failed"
        reloaded["clips"][0]["status"] = "error"
        check("a targeted retry requeues only the failed render",
              candidates.retry(reloaded, second["id"]) and second["state"] == "accepted" and reloaded["clips"][0]["status"] == "queued")
        check("dashboard output excludes filenames and filesystem locations",
              "p01_00001.ts" not in json.dumps(jobs.public(reloaded)) and "segments" not in jobs.public(reloaded)["candidates"][0])
        first = reloaded["candidates"][0]
        first["created"] -= (config.LIVE_CANDIDATE_HOURS + 1) * 3600
        with patch.object(jobs, "try_submit_live", return_value=False), patch.object(config, "ai_ready", return_value=False):
            candidates.run_round()
        check("expiry only removes the selected temporary links", first["state"] == "expired"
              and not candidates._folder(reloaded, first).exists() and candidates._folder(reloaded, second).exists())


def test_buffer():
    response = httpx.Response(200, json={"data":{"channel":{"id":"test", "service":"tiktok", "isDisconnected":False, "isLocked":False}}})
    with patch.dict(os.environ, BUFFER_API_KEY="mock-buffer"), patch.object(config, "BUFFER_TIKTOK_CHANNEL_ID", "test"):
        with patch.object(buffer.httpx, "post", return_value=response) as req:
            check("Buffer connection test uses a read-only TikTok channel query", buffer.check_channel()["ready"]
                  and "mutation" not in req.call_args.kwargs["json"]["query"])
        with patch.object(buffer.httpx, "post", return_value=httpx.Response(503)):
            try:
                buffer.check_channel()
            except buffer.Error as exc:
                check("temporarily unavailable Buffer reads are explicitly safe to retry", exc.status == 503 and not exc.uncertain)
            else:
                check("temporarily unavailable Buffer reads are explicitly safe to retry", False)


def main():
    db.init()
    owner, _ = auth._create("stability@example.test", "Test")
    test_limits()
    test_gemini(owner)
    source = test_capture(Path(work.name))
    test_candidates(owner, source)
    test_buffer()
    return bool(failures)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        db.engine.dispose()
        work.cleanup()
