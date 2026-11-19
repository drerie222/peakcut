"""Real FFmpeg fixtures with local, deterministic substitutes for ASR and external APIs."""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np
import sqlalchemy as sa

work = tempfile.TemporaryDirectory(prefix="pc-clip-quality-")
BASE = "https://peakcut.example.test"
os.environ.update(PEAKCUT_DATA=work.name, DATABASE_URL="", PEAKCUT_ACCOUNTS="1", PEAKCUT_PUBLIC_URL=BASE,
                  BUFFER_API_KEY="", BUFFER_TIKTOK_CHANNEL_ID="quality-tiktok", PEAKCUT_AI_KEY="",
                  PEAKCUT_LOW_RESOURCE="1", PEAKCUT_OUT_FPS="12", OPENBLAS_NUM_THREADS="1")
sys.path.insert(0, ".")
from studio import accounts, auth, brain, buffer, creators, ctx, db, jobs, live, media, quality, render, speaker, transcribe, watchlist

fails = []


def check(name, condition):
    print(("PASS " if condition else "FAIL ") + name, flush=True)
    if not condition:
        fails.append(name)


def fixture(path):
    filters = ("drawbox=x=0:y=0:w=150:h=94:color=0xb96a48:t=fill,"
               "drawbox=x=44:y=13:w=63:h=64:color=0xffca92:t=fill,"
               "drawbox=x=54:y=34:w=9:h=8:color=black:t=fill,"
               "drawbox=x=86:y=34:w=9:h=8:color=black:t=fill,"
               "drawbox=x=65:y=59:w=21:h=5:color=0x591b32:t=fill,"
               "drawbox=x=535:y=270:w=94:h=65:color=0x48dc80:t=fill,"
               "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,1.25)'")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=12",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-vf", filters, "-filter_threads", "1",
                    "-t", "8", "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)],
                   check=True, timeout=45)


def job_for(path, mic=0):
    job = jobs.create({"type": "file", "name": "Synthetic gaming replay", "path": str(path)},
                      {"layout": "gameplay"}, enqueue=False)
    job.update(source_path=str(path), width=640, height=360, duration=8, game_mic_track=mic)
    return job


def make_clip(job, cid, **opts):
    clip = dict(id=cid, status="queued", start=0, end=7.5, score=91, title="A racing finish", description="",
                hashtags=[], hook='"I GOT YOU"', layout="gameplay", captions=True, style="clean",
                evidence="visual", peak_sec=4, facecam=[8, 3, 139, 87])
    clip.update(opts)
    job["clips"].append(clip)
    return clip


def example(path, at, output):
    media.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-threads", "1", "-i", str(path), "-frames:v", "1",
               "-filter_threads", "1", "-vf", "scale=360:640", "-q:v", "4", str(output)], timeout=25)


def test_render(path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    failed_opening = quality.review(path, {"start": 0, "end": 8}, 1)
    check("preflight flags a rendered asset whose first frames are still black",
          any("opening frames" in issue for issue in failed_opening["issues"]))
    mixed = job_for(path)
    mixed_clip = make_clip(mixed, "mixed")
    legacy_words = [{"s": 2.1, "e": 2.7, "w": "Getaway"}, {"s": 2.7, "e": 3.4, "w": "driver"}]
    mixed_clip.update(fixed_words=legacy_words, fixed_for=[0, 7.5, "en2"])
    with patch.object(transcribe, "transcribe") as asr, patch.object(brain, "english_captions") as subtitles:
        jobs.render_one(mixed, mixed_clip, legacy_words)
    mixed_output = Path(mixed["dir"]) / "clips" / "mixed.mp4"
    ass = mixed_output.with_suffix(".ass").read_text(encoding="utf-8") if mixed_output.exists() else ""
    check("a black lead-in is trimmed and the actual rendered opening passes", mixed_clip["status"] == "done"
          and 1.1 < mixed_clip.get("trimmed_opening", 0) < 1.7 and not mixed_clip["quality"]["issues"]
          and quality.opening(mixed_output, 0, 3) == 0)
    check("mixed voices and stale cached captions are suppressed before translation", not asr.called and not subtitles.called
          and mixed_clip.get("speech_source") == "mixed" and "Getaway" not in ass and "Dialogue:" not in ass
          and "fixed_words" not in mixed_clip)
    check("the on-screen hook cannot turn an unverified quote into a streamer quote", mixed_clip["hook"] == "")
    check("rendering leaves the source bytes untouched", hashlib.sha256(path.read_bytes()).hexdigest() == digest)

    def isolated_asr(audio):
        # The real FFmpeg decoder must hand ASR track 2. The test recognizer emits known words for its 880 Hz signal.
        signal = audio[16000:32000].astype(np.float32)
        peak = int(np.argmax(np.abs(np.fft.rfft(signal))))
        if peak != 880:
            raise ValueError("The recognizer received programme audio instead of the mic.")
        return [{"s": 1.2, "e": 1.55, "w": "Streamer"}, {"s": 1.55, "e": 1.9, "w": "reaction"}]

    mic = job_for(path, mic=2)
    clip = make_clip(mic, "isolated", hook="NARROW ESCAPE ON THE TRACK")
    with (patch.object(transcribe, "transcribe", side_effect=isolated_asr) as asr,
          patch.object(brain, "english_captions", side_effect=lambda w, *a, **kw: w) as subtitles):
        jobs.render_one(mic, clip, legacy_words)
    output = Path(mic["dir"]) / "clips" / "isolated.mp4"
    ass = output.with_suffix(".ass").read_text(encoding="utf-8") if output.exists() else ""
    check("explicit mic track is decoded instead of the programme mix", asr.call_count == 1
          and clip.get("speech_source") == "isolated_mic" and "Streamer" in ass and "Getaway" not in ass
          and subtitles.call_args.kwargs["youtube_english"] is None)
    check("captions and short action hook stay on separate lanes", "0:00:03.00,Hook" in ass
          and clip.get("hook") == "NARROW ESCAPE ON THE TRACK")
    check("real output retains the enlarged face and the right-edge HUD", _check_frame(output))
    missing = job_for(path, mic=4)
    with patch.object(transcribe, "transcribe") as asr:
        voice, mode = speaker.captions(missing, clip, path, 0)
    check("an absent declared track never falls back to the game mix", mode == "mic_unavailable" and not asr.called and voice == [])
    if os.environ.get("PEAKCUT_QUALITY_EXAMPLES") and output.exists():
        example(output, 0, Path(os.environ["PEAKCUT_QUALITY_EXAMPLES"]) / "after-start.jpg")
        example(output, 1.4, Path(os.environ["PEAKCUT_QUALITY_EXAMPLES"]) / "after-caption.jpg")
    return mic, clip


def _check_frame(path):
    if not path.exists():
        return False
    from PIL import Image
    frame = Path(work.name) / "frame.jpg"
    example(path, 0.5, frame)
    with Image.open(frame) as image:
        arr = np.asarray(image.convert("RGB"), dtype=np.int16)
    # The pale centre of the fake camera is now in the top-middle, and a green HUD card survives at the far right.
    centre = arr[151:180, 161:190].mean(axis=(0, 1))
    hud = arr[405:432, 307:333].mean(axis=(0, 1))
    return centre[0] > 195 and centre[1] > 145 and hud[1] - hud[0] > 55 and hud[1] - hud[2] > 35


def test_selection_and_export(job, clip):
    levels = np.full(62, -30.0)
    check("steady programme audio yields no highlight", brain.gameplay_candidates(levels, 62, 3, 10, 35) == [])
    levels[26:28] = [-9, -10]
    picks = brain.gameplay_candidates(levels, 62, 3, 10, 35)
    check("related audio peaks produce one review candidate without a quote or scene claim", len(picks) == 1
          and picks[0]["evidence"] == "audio_only" and not picks[0]["hook"])
    with patch.object(brain.ai, "see", return_value=('{"title":"Maybe a stunt","score":100}', 0, 0)):
        check("an unsupported score is not visual evidence", brain.judge_gameplay(b"fixture", 0, 10, "GTA", "gta6")[0] is None)

    # Unscoped older games never went through the mic gate. They cannot export an old rendered version.
    legacy = dict(clip)
    legacy.pop("quality", None)
    legacy.update(id="legacy", version=2)
    bad = jobs.create({"type": "file", "name": "invalid.mp4"}, {}, enqueue=False)
    (Path(bad["dir"]) / "clips").mkdir()
    (Path(bad["dir"]) / "clips" / "corrupt.mp4").write_bytes(b"no playable track")
    corrupt = dict(id="corrupt", file="corrupt.mp4", status="done", version=1, start=0, end=7.5)
    weak = dict(clip)
    weak.update(id="weak", quality={**clip["quality"], "issues": ["Only a sound spike was detected."]})
    with patch.dict(os.environ, BUFFER_API_KEY="test-key"), patch.object(buffer.httpx, "post") as http:
        outcomes = []
        for j, c in ((job, legacy), (bad, corrupt), (job, weak)):
            try:
                buffer.create(j, c)
            except buffer.Error as exc:
                outcomes.append((exc.status, str(exc)))
        check("legacy gameplay, a corrupt file and a weak highlight are all rejected before any Buffer request",
              len(outcomes) == 3 and all(code == 409 for code, _ in outcomes) and not http.called
              and "predates" in outcomes[0][1] and "could not be read" in outcomes[1][1] and "sound spike" in outcomes[2][1])
        job.update(watch_broadcast="test-stream", clips=[weak])
        accounts.save_settings({"watch_buffer": True})
        weak["watch_export"] = {"state": "queued", "due": 0, "attempts": 0}
        watchlist.export_round()
        check("a pending automatic export is held before a network call", weak["watch_export"]["state"] == "held"
              and weak["watch_export"]["attempts"] == 0 and not http.called)

    configured = creators.create({"name": "Fixture", "layout": "gameplay", "game_mic_track": "2"})
    recorded = jobs.create({"type": "live", "name": "GTA VI"}, {}, enqueue=False, creator=configured["id"], kind="live")
    session = live.Session(recorded, configured)
    session.pending.extend(bytes(live.SR * 2))
    with patch.object(transcribe, "transcribe") as asr, patch.object(session, "_triggers") as trigger:
        check("live mixed audio only advances the clock and cannot trigger clips by quoting a character",
              session._transcribe_step() and session.t_done == 1 and not asr.called and not trigger.called)
    session.manual.append({"start": 0, "end": 1})
    session.words = [{"w": "Getaway", "s": 0.1, "e": 0.5}]
    with patch.object(brain, "describe") as describe, patch.object(session, "_add_clip") as add:
        session._manual_step()
        check("a hand-selected live gameplay clip never turns a stale NPC transcript into copy",
              not describe.called and add.called and not add.call_args.args[0]["hook"])
    session.words = []
    test_live_source(session, Path(job["source"]["path"]))

    channel = {"id": "quality-tiktok", "service": "tiktok", "isDisconnected": False, "isLocked": False}
    draft = {"__typename": "PostActionSuccess", "post": {"id": "quality-post", "status": "draft"}}
    with (patch.dict(os.environ, BUFFER_API_KEY="test-key"), patch.object(buffer.httpx, "post", side_effect=[
            httpx.Response(200, json={"data": {"channel": channel}}),
            httpx.Response(200, json={"data": {"createPost": draft}})]) as http):
        job["clips"].append(clip)
        state = buffer.create(job, clip)
        check("a reviewed clip still creates only a draft", state["state"] == "draft" and http.call_count == 2
              and "saveToDraft: true" in http.call_args.kwargs["json"]["query"])


def test_live_source(session, source):
    path = session.buf / "p01_00001.ts"
    media.run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0", "-map", "0:a:1",
               "-c", "copy", str(path)], timeout=15)
    session.ended = True
    session.segments = [(path, 0, 8)]
    clip = make_clip(session.job, "live-source")
    with patch.object(jobs, "render_one") as render_one:
        session._render(clip)
    streams = media.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                         "-of", "csv=p=0", clip["src"]], timeout=10).split()
    indexes = {line.strip(",") for line in streams}
    check("live clip assembly retains the isolated audio stream alongside programme audio",
          render_one.called and clip.get("mic_track") == 2 and len(indexes) == 2)


def main():
    db.init()
    owner, _ = auth._create("quality-owner@example.test", "Owner")
    with db.engine.begin() as conn:
        conn.execute(sa.update(db.users).where(db.users.c.id == owner.id).values(is_admin=True))
    render.install_fonts()
    configured_examples = os.environ.get("PEAKCUT_QUALITY_EXAMPLES")
    source = Path(configured_examples or work.name) / "synthetic-source.mp4"
    fixture(source)
    crop = render._face_crop([8, 3, 139, 87], 640, 360)
    check("an edge-aligned facecam gets a padding margin without exceeding the frame", crop[0] == 0 and crop[1] == 0
          and crop[2] >= 150 and crop[3] >= 94 and render._face_crop([620, 0, 40, 50], 640, 360) is None)
    with ctx.as_user(owner.id):
        job, clip = test_render(source)
        test_selection_and_export(job, clip)
    return bool(fails)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        db.engine.dispose()
        work.cleanup()
