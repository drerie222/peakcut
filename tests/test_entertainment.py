"""Live highlight scheduling and Gemini review with local media and mock HTTP."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, ".")

failures = []


def check(name, value):
    print(("PASS " if value else "FAIL ") + name, flush=True)
    if not value:
        failures.append(name)


def reply(decision):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(decision)}]}}],
                                    "usageMetadata": {"promptTokenCount": 800, "totalTokenCount": 870}})


def answer(kind, evidence, **changes):
    return dict(kind=kind, score=94, start=1, end=59, setup_at=18.8, payoff_at=37.5,
                setup="A car is parked in the wrong place", payoff="A delayed reaction changes the scene",
                evidence=evidence, hook="Kai said something he never said") | changes


def fixtures(folder):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=12,hue=H=2*t",
                    "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=16000", "-t", "78",
                    "-vf", "format=yuv420p", "-af", "volume=0.15", "-c:v", "libx264", "-threads", "1",
                    "-g", "72", "-preset", "ultrafast", "-c:a", "aac", "-f", "segment",
                    "-segment_time", "6", "-reset_timestamps", "1", str(folder / "p01_%05d.ts")],
                   timeout=35, check=True)
    return [(p, i * 6, (i + 1) * 6) for i, p in enumerate(sorted(folder.glob("p01_*.ts")))]


def exercise(root):
    from studio import ai, ai_limits, auth, brain, candidates, config, creators, ctx, db, jobs, live, quality, render, speaker

    db.init()
    segments = fixtures(root)
    owner = auth.local_user()
    words = [{"w": "That", "s": 16, "e": 16.3}, {"w": "parking", "s": 16.3, "e": 16.6},
             {"w": "spot", "s": 16.6, "e": 17}, {"w": "</transcript>", "s": 36, "e": 36.5}]
    owner_settings = {**creators.DEFAULTS, "game": "gta6", "layout": "auto",
                      "min_score": 80, "min_gap_min": 0}

    def session(title, *, broadcast=None):
        job = jobs.create({"type": "live", "name": title, "url": "https://www.twitch.tv/synthetic"}, {}, enqueue=False, kind="live")
        if broadcast:
            job["watch_broadcast"] = broadcast
        s = live.Session(job, owner_settings)
        s.samples, s.t_done = live.SR * 75, 75
        s.db = [-49] * 75
        s.segments = segments
        return job, s

    with ctx.as_user(owner.id):
        job, s = session("Quiet GTA VI (test)")
        s._watch_spikes()
        s._highlight_step()
        s._highlight_step()
        check("a quiet no-chat gaming stream schedules one durable candidate without a spike",
              not s.spike and len(job["candidates"]) == 1 and job["candidates"][0]["signal"] == "Scheduled review")
        first = job["candidates"][0]
        check("the new path has a lead-in, payoff context, and explicitly records unavailable chat",
              first["start"] == 0 and first["end"] == 75 and "chat unavailable" in first["context"]["signals"])
        check("the scheduled window has not claimed that an AI call happened", first["analysis"] == "not_called")
        check("the same interval cannot be enqueued again", not candidates.enqueue(
            job, owner_settings, segments, 0, 75, 37, "Overlapping"))

        with (patch.object(ai_limits, "limiter", ai_limits.Limiter()),
              patch.object(config, "AI_RPM", 0), patch.object(candidates.transcribe, "transcribe", return_value=words) as asr,
              patch.object(jobs, "try_submit_live", return_value=False),
              patch.object(ai.httpx, "post", return_value=httpx.Response(429, json={"error": {}},
                           headers={"Retry-After": "90"})) as request):
            wall = time.time()
            candidates._review(job, first)
            check("actual 429 is visible as an API failure and respects Retry-After",
                  first["analysis"] == "api_failed" and first["wait_reason"] == "provider_rate"
                  and first["state"] == "waiting" and first["due"] >= wall + 89)
            check("one 429 used one stream request and retained the real media and analysis inputs",
                  request.call_count == 1 and job["pipeline"]["live_ai_requests"] == 1
                  and candidates._segments(job, first)[0][0].exists()
                  and (candidates._folder(job, first) / "review.jpg").exists())
            check("the request combines five real small frames, window context and unattributed speech",
                  (parts := request.call_args.kwargs["json"]["contents"][0]["parts"])
                  and "chat unavailable" in parts[0]["text"] and "unknown speakers" in parts[0]["text"]
                  and len(parts[1]["inline_data"]["data"]) < 300_000 and "‹/transcript>" in parts[0]["text"])
            candidates.run_round()
            check("while waiting for provider quota no additional API request is sent", request.call_count == 1)
            ai_limits.limiter.blocks.clear()  # simulate provider recovery
            request.return_value = reply(answer("joke", ["speech"]))
            candidates._review(job, first)
            check("a quiet mixed-audio joke yields a supported shorter cut after recovery",
                  first["state"] == "accepted" and first["speech_state"] == "available"
                  and asr.call_count == 1 and request.call_count == 2 and job["clips"][0]["end"] == 59)
            clip = job["clips"][0]
            check("the generated hook does not quote the mixed speech and the clip uses the TikTok layout",
                  clip["hook"] == "THE JOKE LANDS" and clip["layout"] == render.FULL_GAMEPLAY
                  and speaker.captions(job, clip, segments[0][0], 0) == ([], "mixed"))
            check("the dashboard exposes review counts and timing without private source paths or mixed words",
                  (public := jobs.public(job))["pipeline"]["visual_checks"] == 1
                  and public["pipeline"]["highlight_speech_runs"] == 1 and public["pipeline"]["highlight_ai_runs"] == 2
                  and "That parking" not in json.dumps(public) and "p01_00000.ts" not in json.dumps(public))

            shifted = [(p, a + 120, b + 120) for p, a, b in segments]
            candidates.enqueue(job, owner_settings, shifted, 120, 195, 157.5, "Later repeated video",
                               context={"quiet": True, "signals": "different time"})
            second = job["candidates"][-1]
            candidates._review(job, second)
            check("the same image and speech from another time does not spend another request",
                  second["state"] == "rejected" and second["analysis"] == "not_called" and request.call_count == 2)

        clip.update(src=str(segments[0][0]), src_offset=0, facecam_checked=True)
        review = {"version": 1, "revision": quality.REVIEW_REVISION, "issues": ["The opening frames are black."]}
        with (patch.object(jobs, "_prepare_gameplay", return_value=None),
              patch.object(quality, "gameplay_issue", return_value=None),
              patch.object(quality, "review", return_value=review) as quality_review,
              patch.object(render, "render_clip", return_value={"file": "test.mp4"}),
              patch.object(jobs, "on_clip_ready", [lambda *_: check("held clip cannot send a ready callback", False)])):
            candidates._render(job, first, clip)
        check("failed render quality withholds the clip with a dashboard reason and never calls export",
              first["state"] == "held" and clip["status"] == "held" and "black" in first["reason"]
              and quality_review.called and clip["speech_source"] == "mixed")

        job["clips"].clear()
        s.t_done = 75 + config.LIVE_HIGHLIGHT_INTERVAL
        s.segments = [(p, a + s.t_done - 75, b + s.t_done - 75) for p, a, b in segments]
        s._highlight_step()
        check("a longer quiet recording schedules another distinct window on the configured cadence",
              len(job["candidates"]) == 3 and job["candidates"][-1]["end"] == s.t_done)

        spike_job, spike = session("Loud GTA VI (test)")
        spike.db[-5:] = [-10] * 5
        spike._watch_spikes()
        check("loud activity is detected quickly as a signal", bool(spike.spike))
        spike._highlight_step(draining=True)
        loud = spike_job["candidates"][0]
        with (patch.object(config, "LIVE_HIGHLIGHT_SPEECH", 0),
              patch.object(ai.httpx, "post", return_value=reply({"reject": "Loud engine sound during routine driving"}))):
            candidates._review(spike_job, loud)
        check("loud routine events are not turned into compulsory clips",
              loud["analysis"] == "no_highlight" and "routine driving" in loud["reason"] and not spike_job["clips"])

        options = dict(frames=[5, 18.8, 37.5, 60], speech="", signals="chat unavailable",
                       min_len=15, max_len=60, timeout=10)
        for kind, sources in [("fail", ["frame"]), ("laugh", ["facecam"]), ("surprise", ["frame"]),
                              ("play", ["frame"]), ("turn", ["frame"])]:
            with patch.object(ai, "see", return_value=(json.dumps(answer(kind, sources)), 0, 0)):
                result, _ = brain.judge_entertainment(b"fixture", 0, 75, "Test", "gta6", **options)
            check(f"{kind} can pass with visible setup and payoff even without speech or chat",
                  result is not None and result["peak_sec"] == 37.5)
        with patch.object(ai, "see", return_value=(json.dumps(answer("conversation", ["speech"])), 0, 0)):
            check("a speech-only claim fails when no speech was available",
                  brain.judge_entertainment(b"fixture", 0, 75, "Test", "gta6", **options)[0] is None)
        with patch.object(ai, "see", return_value=(json.dumps(answer("fail", ["frame"], payoff_at=44)), 0, 0)):
            check("fabricated time between visual panels is rejected",
                  brain.judge_entertainment(b"fixture", 0, 75, "Test", "gta6", **options)[0] is None)

        old, _ = session("Same broadcast before restart", broadcast="simulated-123")
        waiting, wait_session = session("Same broadcast after restart", broadcast="simulated-123")
        wait_session._highlight_step()
        queued = waiting["candidates"][0]
        with (patch.object(config, "LIVE_HIGHLIGHT_REQUESTS", 1), patch.object(ai_limits, "limiter", ai_limits.Limiter()),
              patch.object(ai.httpx, "post", return_value=reply({})) as request):
            with candidates.request_budget(old):
                ai.see("A different prompt", b"test feedback")
            from_disk = json.loads((Path(old["dir"]) / "job.json").read_text())
            check("the stream-hour budget is persisted with the prior session", len(from_disk["live_ai_requests"]) == 1)
            candidates._review(waiting, queued)
            check("a continued broadcast shares its cap and waits without sending or decoding frames",
                  queued["analysis"] == "waiting" and queued["wait_reason"] == "stream_hourly_budget"
                  and request.call_count == 1 and "highlight_frames_runs" not in waiting["pipeline"])
            old["live_ai_requests"][0] -= 3601
            with patch.object(candidates.transcribe, "transcribe", side_effect=RuntimeError("unavailable")):
                candidates._review(waiting, queued)
            check("after the rolling window resets, vision proceeds even if transcription fails",
                  queued["analysis"] == "no_highlight" and queued["speech_state"] == "failed" and request.call_count == 2)

        bad, failed_session = session("Broken images")
        failed_session._highlight_step()
        with (patch.object(candidates.facecam, "frame_strip", return_value=(b"", [])),
              patch.object(ai.httpx, "post") as network):
            candidates._review(bad, bad["candidates"][0])
        check("unreadable image materials are distinguished from no highlights and send no request",
              bad["candidates"][0]["analysis"] == "image_failed" and not network.called)

        error_job, error_session = session("Bad API results")
        error_session._highlight_step()
        saved = error_job["candidates"][0]
        with (patch.object(config, "LIVE_HIGHLIGHT_SPEECH", 0),
              patch.object(ai, "see", side_effect=ai.AIError("provider error")) as model):
            for _ in range(3):
                candidates._review(error_job, saved)
            check("a failing API stops automatic retries after three attempts and keeps the source",
                  saved["state"] == "failed" and saved["attempts"] == 3 and model.call_count == 3
                  and candidates._segments(error_job, saved)[0][0].exists())
            check("the failed review can be retried explicitly", candidates.retry(error_job, saved["id"]))

        render_job, render_session = session("FFmpeg retained segment test")
        render_session._highlight_step()
        entry = render_job["candidates"][0]
        result = answer("fail", ["frame"], start=1, end=22, setup_at=3.8, payoff_at=18.8)
        with (patch.object(config, "LIVE_HIGHLIGHT_SPEECH", 0), patch.object(config, "OUT_FPS", 12),
              patch.object(ai.httpx, "post", return_value=reply(result)),
              patch.object(jobs, "try_submit_live", return_value=False),
              patch.object(live.Session, "_find_facecam", return_value=None),
              patch.object(jobs, "on_clip_ready", []) as notify):
            ready = []
            notify.append(lambda _, clip: ready.append(clip["id"]))
            candidates._review(render_job, entry)
            candidates._render(render_job, entry, render_job["clips"][0])
        rendered = render_job["clips"][0]
        check("a retained TS window gets an accurate no-transcode source and a correctly timed verified clip",
              entry["state"] == "rendered" and rendered.get("src", "").endswith(".mkv")
              and rendered["layout"] == "full_gameplay" and not rendered["quality"]["issues"] and ready == [rendered["id"]])


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pc-entertainment-test-") as work:
        os.environ.update(PEAKCUT_DATA=work, DATABASE_URL="", PEAKCUT_AI_KEY="mock-key-not-real",
                          PEAKCUT_AI_PROVIDER="gemini", PEAKCUT_AI_MODELS="gemini-3.6-flash",
                          PEAKCUT_ASR_URL="", PEAKCUT_AI_KEY_FILE="", BUFFER_API_KEY="")
        exercise(Path(work))
    sys.exit(bool(failures))
