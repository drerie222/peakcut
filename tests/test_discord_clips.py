"""Finished clips and the Discord worker, with a temporary database and no network."""
import json
import os
import secrets
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

BASE = "https://peakcut.example.test"
data = tempfile.TemporaryDirectory(prefix="pc-discord-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="", PEAKCUT_ACCOUNTS="1", PEAKCUT_PUBLIC_URL=BASE,
                  PEAKCUT_FETCH_MODEL="0", DISCORD_CLIPS_WEBHOOK_URL="")
sys.path.insert(0, ".")

import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from studio import auth, config, creators, ctx, db, discord_clips, jobs, live, posting, render, server, updates  # noqa: E402

TOKEN = secrets.token_urlsafe(24)       # made up for this test
PATH = f"/api/webhooks/123/{TOKEN}"
fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def new_clip(job, clip_id):
    clip = dict(id=clip_id, title="  GTA chase  ", hook="", description="", hashtags=[], score=1,
                start=1, end=13.5, captions=False)
    job["clips"].append(clip)
    return clip


def fake_render(src, folder, name, clip, words, width, height):
    folder.mkdir(exist_ok=True)
    name += ".mp4"
    (folder / name).write_bytes(b"test video")
    return {"file": name}


def test_delivery():
    owner, _ = auth._create("owner@example.test", "Owner")
    viewer, _ = auth._create("viewer@example.test", "Viewer")
    with ctx.as_user(owner.id):
        creator = creators.create({"name": "Creator workspace"})
        job = jobs.create({"type": "file", "name": "stream.mp4"}, {}, enqueue=False, creator=creator["id"])
        job.update(source_path=str(Path(job["dir"]) / "source.mp4"), width=1280, height=720,
                   uploader="The streamer", duration=45)
        clip = new_clip(job, "clip01")

    with patch.object(discord_clips, "_post") as send:
        discord_clips.enqueue(job, clip)
        check("an unset webhook leaves the queue empty", not discord_clips._send_next(PATH))
        check("an unset webhook sends nothing", not send.called)

    with (patch.object(discord_clips, "_WEBHOOK_PATH", PATH), patch.object(jobs, "on_clip_ready", []),
          patch.object(jobs, "start"), patch.object(live, "start"), patch.object(posting, "start"),
          patch.object(updates, "start"), patch.object(discord_clips, "start") as start_worker):
        server._startup()
        check("the server registers the render callback and starts the worker",
              discord_clips.enqueue in jobs.on_clip_ready and start_worker.called)
        with (ctx.as_user(owner.id), patch.object(render, "render_clip", side_effect=fake_render),
              patch.object(discord_clips, "_post", return_value=None) as send):
            jobs.render_one(job, clip, [])
            check("a finished render queues delivery without contacting Discord", clip["status"] == "done" and not send.called)
            db.engine.dispose()
            sent = discord_clips._send_next(PATH)
            check("the worker sends the saved notification once after reconnecting to the database", sent and send.call_count == 1)
            payload = send.call_args.args[1] if send.called else {}
            embed = (payload.get("embeds") or [{}])[0]
            fields = {f["name"]: f["value"] for f in embed.get("fields", [])}
            check("the notification includes title, actual uploader and duration",
                  embed.get("title") == "GTA chase" and fields == {"Streamer": "The streamer", "Duration": "12.5 s"})
            check("the notification cannot ping Discord members", payload.get("allowed_mentions") == {"parse": []})
            url = embed.get("url")
            check("the notification uses the existing protected video route", url == f"{BASE}/media/{job['id']}/clip01.mp4")
            if url:
                client = TestClient(server.app, base_url=BASE)
                check("signed-out visitors cannot open the video link", client.get(url).status_code == 401)
                client.cookies.set(auth.COOKIE, auth.start_session(viewer.id))
                check("another PeakCut user cannot open the video link", client.get(url).status_code == 404)
                client.cookies.set(auth.COOKIE, auth.start_session(owner.id))
                check("the clip owner can open the link", client.get(url).content == b"test video")

            jobs.render_one(job, clip, [])
            with ThreadPoolExecutor(max_workers=4) as workers:
                list(workers.map(lambda _: discord_clips.enqueue(job, clip), range(8)))
            check("remakes and concurrent duplicate events do not send a second message",
                  clip["version"] == 2 and not discord_clips._send_next(PATH) and send.call_count == 1)

            broken = new_clip(job, "broken")
            with patch.object(render, "render_clip", side_effect=RuntimeError("cannot encode")):
                jobs.render_one(job, broken, [])
            check("a failed render does not queue a notification",
                  broken["status"] == "error" and not discord_clips._send_next(PATH))

            removed = new_clip(job, "removed")
            jobs.render_one(job, removed, [])
            (Path(job["dir"]) / "clips" / removed["file"]).unlink()
            discord_clips._send_next(PATH)
            check("a queued clip removed before delivery is skipped", send.call_count == 1)

            offline = new_clip(job, "offline")
            job["uploader"] = ""
            with patch.object(config, "ACCOUNTS", False):
                jobs.render_one(job, offline, [])
                discord_clips._send_next(PATH)
                embed = send.call_args.args[1]["embeds"][0]
                check("local mode sends the creator name and omits the video link",
                      "url" not in embed and embed["fields"][0]["value"] == "Creator workspace")
            with patch.object(config, "PUBLIC_URL", "http://peakcut.example.test"):
                check("plain HTTP never produces a video link", discord_clips._video_link(job["id"], offline["file"]) is None)
    return job


def test_retries(job):
    clip = new_clip(job, "retry")
    clip.update(fake_render(None, Path(job["dir"]) / "clips", clip["id"], clip, [], 1280, 720))
    with (patch.object(discord_clips, "_WEBHOOK_PATH", PATH), patch.object(db, "now", return_value=100.0) as clock,
          patch.object(discord_clips, "_post", side_effect=[0.0, 6.0, 0.0]) as send):
        discord_clips.enqueue(job, clip)
        check("a connection failure schedules a retry", discord_clips._send_next(PATH) and send.call_count == 1)
        clock.return_value = 101.9
        check("the worker waits for the first backoff", not discord_clips._send_next(PATH))
        clock.return_value = 102.0
        db.engine.dispose()
        check("the retry survives a database reconnect", discord_clips._send_next(PATH))
        clock.return_value = 107.9
        check("a longer Discord Retry-After takes precedence", not discord_clips._send_next(PATH))
        clock.return_value = 108.0
        check("the third attempt runs at the scheduled time", discord_clips._send_next(PATH) and send.call_count == 3)
        clock.return_value = 999.0
        discord_clips.enqueue(job, clip)
        check("attempts stop at three, even when the same clip is reported again", not discord_clips._send_next(PATH))
    with db.engine.connect() as cn:
        row = cn.execute(sa.select(db.discord_clips).where(db.discord_clips.c.clip_id == clip["id"])).one()
        check("the database stores the retry count without webhook credentials", row.attempts == 3 and TOKEN not in repr(row))


def test_transport():
    check("only canonical Discord HTTPS webhooks are accepted",
          discord_clips._webhook_path(f"https://discord.com{PATH}") == PATH
          and all(discord_clips._webhook_path(url) is None for url in
                  (f"https://example.test{PATH}", f"http://discord.com{PATH}", f"https://discord.com{PATH}?thread_id=1")))
    with (patch.object(discord_clips.http.client, "HTTPSConnection") as connection,
          patch.object(discord_clips._log, "warning") as warning):
        conn = connection.return_value
        response = conn.getresponse.return_value
        response.status = 200
        payload = {"allowed_mentions": {"parse": []}}
        result = discord_clips._post(PATH, payload)
        args, kwargs = conn.request.call_args
        check("the HTTPS request asks Discord for confirmation without uploading a file",
              result is None and connection.call_args.args == ("discord.com",)
              and args == ("POST", PATH + "?wait=true") and json.loads(kwargs["body"]) == payload)

        conn.request.reset_mock()
        conn.connect.side_effect = TimeoutError(TOKEN)
        check("a timeout before sending is safe to retry", discord_clips._post(PATH, {}) == 0 and not conn.request.called)
        conn.connect.side_effect = None
        conn.getresponse.side_effect = TimeoutError(TOKEN)
        check("a lost response is not retried because Discord may have received it", discord_clips._post(PATH, {}) is None)
        conn.getresponse.side_effect = None
        response.status, response.getheader.return_value = 429, "3.5"
        check("Discord's rate-limit delay is read", discord_clips._post(PATH, {}) == 3.5)
        response.getheader.return_value = "120"
        check("a rate limit outside the retry window ends delivery", discord_clips._post(PATH, {}) is None)
        response.status = 307
        conn.request.reset_mock()
        check("redirects are not followed", discord_clips._post(PATH, {}) is None and conn.request.call_count == 1)
        response.status = 403
        check("a rejected webhook is not retried", discord_clips._post(PATH, {}) is None)
        response.status = 502
        check("an ambiguous Discord server error is not retried", discord_clips._post(PATH, {}) is None)
        check("log messages never include the webhook token", warning.called and TOKEN not in repr(warning.call_args_list))


try:
    db.init()
    test_retries(test_delivery())
    test_transport()
finally:
    db.engine.dispose()
    data.cleanup()

sys.exit(1 if fails else 0)
