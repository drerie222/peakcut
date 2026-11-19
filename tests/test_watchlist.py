"""Local gaming fixture, with platform checks, Discord and Buffer replaced."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

BASE = "https://peakcut.example.test"
data = tempfile.TemporaryDirectory(prefix="pc-watch-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="", PEAKCUT_ACCOUNTS="1", PEAKCUT_PUBLIC_URL=BASE,
                  PEAKCUT_FETCH_MODEL="0", PEAKCUT_UPDATE_CHECK="0", PEAKCUT_VERIFY_EMAIL="0",
                  DISCORD_CLIPS_WEBHOOK_URL="", DISCORD_LIVE_WEBHOOK_URL="", DISCORD_STATUS_WEBHOOK_URL="",
                  BUFFER_API_KEY="", BUFFER_TIKTOK_CHANNEL_ID="gaming-channel")
sys.path.insert(0, ".")

import httpx
import sqlalchemy as sa
from fastapi.testclient import TestClient
from studio import accounts, auth, brain, buffer, config, creators, ctx, db, discord_clips, facecam
from studio import jobs, live, load, media, posting, profiles, render, server, transcribe, watchlist

ORIGIN = {"Origin": BASE}
FEEDS = {"live": "/sim-live", "status": "/sim-status", "clips": "/sim-clips"}
fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name, flush=True)
    if not ok:
        fails.append(name)


def test_import(client):
    check("import creates thirty unique channel workspaces", client.post("/api/watchlist/import", headers=ORIGIN).json() == {"added": 30})
    rows = client.get("/api/creators").json()
    check("three priority groups and all three platforms survive the import",
          len(rows) == 30 and {c["watchlist"]["priority"] for c in rows} == {"A", "B", "C"}
          and {watchlist.channel_url(c["channel"])[0] for c in rows} == {"youtube", "twitch", "kick"})
    check("new entries are disabled, with gameplay layout and support for long clips",
          all(not c["auto_watch"] and not c["autopost"] and c["layout"] == render.FULL_GAMEPLAY and c["max_len"] >= 60 for c in rows))
    check("featured YouTube handles and the CaseOh underscore are retained",
          {"https://www.youtube.com/@IShowSpeed", "https://www.youtube.com/@TGG_", "https://www.twitch.tv/caseoh_"}
          <= {c["channel"] for c in rows})
    c = next(c for c in rows if c["name"] == "PENTA")
    path = "/api/creators/" + c["id"]
    got = client.put(path, json={"name": "PENTA (edited)"}, headers=ORIGIN)
    check("an imported streamer can be edited", got.status_code == 200 and got.json()["name"] == "PENTA (edited)")
    check("import is idempotent after editing", client.post("/api/watchlist/import", headers=ORIGIN).json() == {"added": 0})
    check("a spoofed platform domain is refused", client.put(path, json={"channel": "https://twitch.tv.example.test/penta"}, headers=ORIGIN).status_code == 400)
    setting = client.put("/api/admin/watchlist", json={"watch_recordings": 30, "watch_buffer": True}, headers=ORIGIN)
    check("the recording setting respects the three-stream server ceiling", setting.json()["limit"] == 3)
    return rows


def test_poll(owner, client, rows):
    selected = [next(c for c in rows if c["name"] == name) for name in ("Kai Cenat", "xQc", "Buddha", "Spaceboy")]
    for c in reversed(selected):
        check(f"{c['name']} can be enabled", client.put("/api/creators/" + c["id"], json={"auto_watch": True},
                                                       headers=ORIGIN).json()["auto_watch"])
    profiles._live.clear()
    names = []

    def status(platform, url):
        names.append(url)
        return {"live": True, "title": "GTA road race", "started": 100, "embed": url.split("/")[-1]}

    def start(session):
        live.SESSIONS[session.job["id"]] = session

    with (patch.object(config, "MAX_LIVE_TOTAL", 3), patch.object(profiles, "live_now", side_effect=status),
          patch.object(live.Session, "start", start), patch.object(load, "disk_full", return_value=False),
          patch.object(load, "busy", return_value=False), patch.object(discord_clips, "_EVENT_PATHS", FEEDS)):
        live._watch_user(owner.id)
        check("three slots go to A then B, regardless of the order entries were enabled",
              [s.job["creator"] for s in live.active()] == [c["id"] for c in selected[:3]])
        check("all enabled streams are still checked at capacity", len(names) == 4)
        remaining = next(c for c in client.get("/api/creators").json() if c["id"] == selected[3]["id"])
        check("standby shows live, platform and stream title",
              remaining["watch_status"]["state"] == "live" and remaining["watch_status"]["platform"] == "Twitch"
              and remaining["watch_status"]["title"] == "GTA road race" and not remaining["live_job"])
        first = live.active()[0]
        live._watch_user(owner.id)
        check("polls inside the cache window never contact the platforms again", len(names) == 4)
        client.put("/api/creators/" + selected[0]["id"], json={"auto_watch": False}, headers=ORIGIN)
        check("disabling a streamer stops its recording and allows a later re-enable",
              first.user_stopped and not first.job.get("watch_stopped"))
        live._watch_user(owner.id)
        check("a free slot goes to an enabled live streamer waiting in priority C",
              any(s.job["creator"] == selected[3]["id"] for s in live.active()) and len(live.active()) == 3)
        with db.engine.connect() as cn:
            notices = cn.execute(sa.select(db.discord_events).where(db.discord_events.c.feed == "live")).all()
        check("repeated live checks enqueue exactly one Discord notice per broadcast", len(notices) == 4)
        with ctx.as_user(owner.id):
            c = creators.get(selected[3]["id"])
            watchlist._observation(c, "Twitch", {"error": "Platform rate limited."})
            c = creators.get(c["id"])
            watchlist._observation(c, "Twitch", status("Twitch", c["channel"]))
        with db.engine.connect() as cn:
            notices = cn.execute(sa.select(db.discord_events).where(db.discord_events.c.feed == "live")).all()
        check("an API error and recovery do not invent an offline/online transition", len(notices) == 4)
    return first.job


def test_backoff():
    clock = SimpleNamespace(time=lambda: 1000)
    error = HTTPError("https://kick.com", 429, "simulated", {"Retry-After": "180"}, None)
    with (patch.object(profiles, "time", clock), patch.object(profiles, "_live", {}),
          patch.object(profiles, "_live_retry", {}), patch.object(profiles, "_platform_retry", {}),
          patch.object(profiles, "live_now", side_effect=error) as call):
        limited = profiles.live_shared("Kick", "https://kick.com/n3on")
        profiles.live_shared("Kick", "https://kick.com/adinross")
        clock.time = lambda: 1179
        profiles.live_shared("Kick", "https://kick.com/n3on")
        check("Retry-After backs off other channels on the same platform too", bool(limited.get("error")) and call.call_count == 1)
        clock.time = lambda: 1181
        call.side_effect = None
        call.return_value = {"live": False}
        check("polling resumes after the platform wait", profiles.live_shared("Kick", "https://kick.com/n3on") == {"live": False, "error": ""})


def gaming_fixture(path):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "nullsrc=s=640x360:r=8,noise=alls=26:allf=t,drawbox=x=0:y=325:w=640:h=5:color=white@0.35:t=fill",
                    "-f", "lavfi", "-i", "color=c=0xDD9966:s=152x88:r=8",
                    "-f", "lavfi", "-i", "color=c=0x40AAF0:s=84x32:r=8",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
                    "-filter_complex", "[0:v][1:v]overlay=0:0[base];[base][2:v]overlay=x='40+mod(t*42,460)':y=284[v]",
                    "-map", "[v]", "-map", "3:a", "-map", "4:a",
                    "-t", "66", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2", "-c:a", "aac", str(path)],
                   check=True, timeout=60)


def test_render_and_export(owner, job):
    path = Path(data.name) / "racing-fixture.mp4"
    gaming_fixture(path)
    clip = dict(id="racing-highlight", status="queued", start=2, end=63, score=95, title="The final stunt",
                description="An unexpected racing finish.", hashtags=["GTA6"], hook="THE FINAL STUNT",
                layout="gameplay", captions=True, style="classic", src=str(path), src_offset=0, mic_track=2, evidence="visual")
    job.update(width=640, height=360, title="GTA road race")
    job["clips"].append(clip)
    words = [{"s": 30, "e": 31, "w": "What"}, {"s": 31, "e": 31.5, "w": "a"}, {"s": 31.5, "e": 32, "w": "stunt!"}]
    jpeg = facecam.frame_strip([(path, 0, 66)], (10, 30, 40))
    check("the visual judge gets a contact sheet from real video frames", jpeg[:2] == b"\xff\xd8")
    with patch.object(brain.ai, "see", return_value=(
            '{"title":"The final stunt","score":95,"category":"clutch","reason":"A car clears the gap."}', 0, 0)):
        c, _ = brain.judge_gameplay(jpeg, 2, 63, job["title"], "gta6")
        check("the visual judge can return a scored 61-second GTA highlight", c["score"] == 95 and c["end"] - c["start"] == 61)
        s = live.SESSIONS[job["id"]]
        s.settings.update(min_gap_min=0)
        with patch.object(s, "_add_clip") as add:
            s._visual_highlight(0, 75, "Jump: chat", {})
            check("the visual path avoids near-duplicate clips before spending another AI call", not add.called)
    face = facecam.detect(path)
    check("the racing fixture's synthetic facecam is detected", bool(face) and face["corner"] == "top-left")
    if face:
        clip["facecam"] = [face[k] for k in ("x", "y", "w", "h")]
    with (ctx.as_user(owner.id), patch.object(config, "OUT_FPS", 8), patch.object(render, "_qsv_ok", False),
          patch.object(transcribe, "transcribe", return_value=words),
          patch.object(brain, "translate_caption_words", return_value=words) as translate,
          patch.object(jobs, "on_clip_ready", [watchlist.on_clip_ready, discord_clips.enqueue, posting.on_clip_ready]),
          patch.object(discord_clips, "_WEBHOOK_PATH", FEEDS["clips"]), patch.object(posting, "enqueue") as publish):
        jobs.render_one(job, clip, words)
        check("the rendered file queues a draft without entering direct publishing",
              clip["status"] == "done" and clip.get("watch_export", {}).get("state") == "queued" and not publish.called)
        product = Path(job["dir"]) / "clips" / clip["file"]
        info = media.probe(product)
        check("the full render is 9:16 and longer than sixty seconds",
              (info["width"], info["height"]) == (1080, 1920) and info["duration"] >= 60)
        subtitle_file = product.with_suffix(".ass").read_text(encoding="utf-8")
        check("the watchlist reuses the English caption path", translate.called and "what" in subtitle_file.lower())
        check("the rendered clip keeps the gameplay layout", clip["layout"] == "gameplay")

    channel = {"id": "gaming-channel", "service": "tiktok", "isLocked": False, "isDisconnected": False}
    reply = {"__typename": "PostActionSuccess", "post": {"id": "draft-test", "status": "draft"}}
    with (patch.dict(os.environ, BUFFER_API_KEY="fake-key-for-tests"),
          patch.object(discord_clips, "_WEBHOOK_PATH", FEEDS["clips"]),
          patch.object(buffer.httpx, "post", side_effect=[httpx.Response(200, json={"data": {"channel": channel}}),
                                                          httpx.Response(200, json={"data": {"createPost": reply}})]) as call):
        watchlist.export_round()
        check("the existing Buffer API saves the clip as a TikTok draft",
              clip["watch_export"]["state"] == "draft" and "saveToDraft: true" in call.call_args.kwargs["json"]["query"])
        draft = call.call_args.kwargs["json"]["variables"]
        check("the automatic draft includes title, description, hashtags and video URL",
              "The final stunt\n\nAn unexpected racing finish.\n\n#GTA6" == draft["text"] and draft["video"].startswith(BASE))
        watchlist.export_round()
        check("the same completed clip never sends a second Buffer mutation", call.call_count == 2)

    with patch.object(discord_clips, "_post", return_value=None) as send:
        while discord_clips._send_next(FEEDS["clips"]):
            pass
        while discord_clips._send_next(None, db.discord_events, FEEDS):
            pass
        check("all three Discord feeds use the shared dispatch, with a draft review message",
              set(c.args[0] for c in send.call_args_list) == set(FEEDS.values())
              and any("draft ready for review" in c.args[1].get("content", "") for c in send.call_args_list))


def main():
    db.init()
    owner, _ = auth._create("watch-owner@example.test", "Owner")
    with db.engine.begin() as cn:
        cn.execute(db.users.update().where(db.users.c.id == owner.id).values(is_admin=True))
    client = TestClient(server.app, base_url=BASE)
    client.cookies.set(auth.COOKIE, auth.start_session(owner.id))
    with patch.object(config, "MAX_LIVE_TOTAL", 3):
        rows = test_import(client)
        job = test_poll(owner, client, rows)
    test_backoff()
    test_render_and_export(owner, job)


try:
    main()
finally:
    db.engine.dispose()
    data.cleanup()

sys.exit(1 if fails else 0)
