"""Small-server scheduling and retention, with no external services."""
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import sqlalchemy as sa
from fastapi import HTTPException
from fastapi.testclient import TestClient

work = tempfile.TemporaryDirectory(prefix="pc-hetzner-")
BASE = "https://peakcut.example.test"
os.environ.update(PEAKCUT_DATA=work.name, DATABASE_URL="", PEAKCUT_ACCOUNTS="1", PEAKCUT_PUBLIC_URL=BASE,
                  PEAKCUT_LOW_RESOURCE="1", PEAKCUT_MAX_LIVE="1", PEAKCUT_DRAFT_ONLY="1",
                  PEAKCUT_ALLOW_SIGNUP="0", PEAKCUT_VERIFY_EMAIL="0", BUFFER_API_KEY="",
                  BUFFER_TIKTOK_CHANNEL_ID="test-tiktok")
sys.path.insert(0, ".")

from studio import auth, buffer, config, creators, ctx, db, discord_clips, jobs, live, load  # noqa: E402
from studio import selection, server, storage, tools, watchlist  # noqa: E402

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def response_code(fn, *args):
    try:
        fn(*args)
    except (HTTPException, buffer.Error) as exc:
        return exc.status_code if isinstance(exc, HTTPException) else exc.status
    return 200


def test_selection(owner):
    now = time.time()
    with ctx.as_user(owner.id):
        watchlist.seed()
        rows = creators.all_creators()
        first = next(c for c in rows if c["name"] == "Kai Cenat")
        other = next(c for c in rows if c["name"] == "Buddha")
        first["watch_status"] = {"live_since": now, "title": "GTA 6 soon!", "category": "Grand Theft Auto V"}
        other["watch_status"] = {"live_since": now, "title": "Launch day", "category": "Grand Theft Auto VI"}
        order, scores = selection.rank([first, other], {}, now)
        check("GTA VI category wins over a misleading title on a higher priority channel",
              order[0] == other and len(scores[first["id"]]["factors"]) == 4)
        first["watch_status"]["category"] = ""
        check("a title provides only the smaller unverified boost", selection.score(first, [], now)["points"] == 40)
        other["watch_status"].update(category="", live_since=now-3600)
        past = [{"created": now-3600, "duration": 3600, "clips": [{"status": "done"}]}]
        check("recording time and finished clips favour a streamer waiting for a first turn",
              selection.rank([first, other], {first["id"]: past}, now)[0][0] == other)
        first["watch_status"].update(title="GTA 6", category="Grand Theft Auto VI", live_since=now-3000)
        other["watch_status"]["live_since"] = now - config.WATCH_FAIR_WAIT_SEC - 1
        order, scores = selection.rank([first, other], {}, now)
        check("the longest overdue stream is first even when higher priority and game metadata give the other a higher score",
              order[0] == other and scores[other["id"]]["points"] < scores[first["id"]]["points"])
        other["watch_status"]["live_since"] = now
        first["watch_status"].update(title="", category="", live_since=now)
        check("with equal game evidence and no history priority A leads", selection.rank([other, first], {}, now)[0][0] == first)
        creators.update(other["id"], {"watch_priority": "A"})
        creators.update(first["id"], {"watch_priority": "C"})
        first["watchlist"]["priority"] = creators.get(first["id"])["watchlist"]["priority"]
        other["watchlist"]["priority"] = creators.get(other["id"])["watchlist"]["priority"]
        check("edited priority changes the next selection", selection.rank([first, other], {}, now)[0][0] == other)

        switches = []
        active = SimpleNamespace(job={"id": "current", "creator": first["id"], "watch_broadcast": "stream"},
                                 started_at=now-config.WATCH_MIN_SEC, stop=switches.append)
        with patch.object(jobs, "save"):
            _, scores = selection.rank([first, other], {}, now)
            watchlist._rotate(active, [other], scores, 0, now)
            check("one observation of a higher priority cannot interrupt the current stream", not switches)
            watchlist._rotate(active, [other], scores, now, now)
            check("a confirmed lead still respects switch cooldown", not switches)
            watchlist._rotate(active, [other], scores, 0, now)
            check("a sustained lead requests an orderly handoff to the chosen streamer",
                  len(switches) == 1 and active.job["watch_switch_to"] == other["id"]
                  and active.job["watch_switched_at"] == now)
            check("minimum recording time and a small lead protect against oscillation",
                  not selection.rotation_reason(config.WATCH_MIN_SEC-1, now, 100, True)
                  and not selection.rotation_reason(config.WATCH_MIN_SEC, now, 15, True))
            check("a waiting competitor gets a turn even with a low score",
                  selection.rotation_reason(config.WATCH_MAX_SEC, now, -100, False) == "Recording turn finished")
    stopped = SimpleNamespace(ingesting=True, finished=False, user_stopped=True)
    with patch.object(live, "SESSIONS", {"stopping": stopped}):
        check("a draining process still occupies the recording slot", live.recording_count() == 1)
        stopped.ingesting = False
        check("the slot is free after the capture process exits", live.recording_count() == 0)


def test_limits():
    parent = Path(work.name) / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    for path, ceiling, used, reclaimable in ((parent, 4294967296, 1000000000, 0),
                                             (child, 2952790016, 2600000000, 200000000)):
        (path / "memory.max").write_text(str(ceiling))
        (path / "memory.current").write_text(str(used))
        (path / "memory.stat").write_text(f"inactive_file {reclaimable}\n")
    check("RAM headroom uses the tightest cgroup and accounts for reclaimable files",
          load._cgroup_ram_gb([child, parent]) == 0.552790016)

    def check_serial(wrapper, title):
        release, entered, competing, next_entry = (threading.Event() for _ in range(4))

        @wrapper
        def hold():
            entered.set()
            release.wait(3)

        @wrapper
        def enter():
            next_entry.set()

        def attempt():
            competing.set()
            enter()

        a, b = threading.Thread(target=hold), threading.Thread(target=attempt)
        a.start()
        first_entered = entered.wait(2)
        b.start()
        overlap = not competing.wait(2) or next_entry.wait(0.1)
        release.set()
        a.join(4)
        b.join(4)
        check(title, first_entered and not overlap and next_entry.is_set())

    with patch.object(load, "busy", return_value=False):
        check_serial(load.serial_work, "local analysis and encoding cannot occupy the heavy work slot concurrently")
        check_serial(load.serial_ai, "remote AI calls are limited separately to one at a time")
    with (patch.object(load, "busy", side_effect=[True, False, False]),
          patch.object(load, "render_disk_full", side_effect=[True, False]), patch.object(load.time, "sleep") as sleep):
        with load.heavy_work(writes_video=True):
            check("rendering is deferred for CPU/RAM pressure and an exhausted disk reserve", sleep.call_count == 2)

    with patch.object(jobs, "_live_q", queue.Queue(1)):
        jobs.submit_live(lambda: None)
        attempting, added = threading.Event(), threading.Event()

        def submit():
            attempting.set()
            jobs.submit_live(lambda: None)
            added.set()

        t = threading.Thread(target=submit)
        t.start()
        blocked = attempting.wait(2) and not added.wait(0.1)
        jobs._live_q.get()
        t.join(2)
        check("a full live queue applies backpressure and resumes once a worker takes a job", blocked and added.is_set())
    with patch.object(tools, "missing", return_value=False), patch.object(jobs, "busy_count", return_value=0):
        with patch.object(jobs, "waiting_total", return_value=config.MAX_QUEUED_VIDEOS):
            check("the global video queue returns 429 at capacity", response_code(server._job_slots) == 429)
    with patch.object(jobs, "_q", jobs._FairQueue()):
        job = jobs.create({"type": "file", "path": "fixture", "name": "Fixture"}, {}, enqueue=False)
        clip = {"id": "clip001", "status": "done", "version": 1, "start": 0, "end": 10}
        job["clips"].append(clip)
        for _ in range(config.MAX_QUEUED_RENDERS):
            jobs.submit(lambda: None)
        try:
            jobs.request_render(job["id"], clip["id"], {"start": 1})
        except jobs.RenderQueueFull:
            check("an overloaded render queue leaves the existing clip/version untouched",
                  clip["start"] == 0 and clip["status"] == "done" and clip["version"] == 1)
        else:
            check("an overloaded render queue leaves the existing clip/version untouched", False)


def test_retention_and_integrations():
    now = time.time()
    job = jobs.create({"type": "live", "name": "Fixture"}, {}, kind="live", enqueue=False)
    folder = Path(job["dir"])
    chunks = folder / "buffer"
    chunks.mkdir()
    (folder / "clips").mkdir()
    raw, video, cover = (folder / "clips" / name for name in ("clip001.src.ts", "clip001.mp4", "clip001.jpg"))
    clip = {"id": "clip001", "status": "done", "file": video.name, "version": 1, "src": str(raw), "title": "Fixture"}
    job.update(status="done", clips=[clip])
    old_segment, recent_segment, unrelated = (chunks / name for name in ("p01_000001.ts", "p01_000002.ts", "keep.ts"))
    for path in (raw, video, cover, old_segment, recent_segment, unrelated):
        path.write_bytes(b"fixture")
        if path != recent_segment:
            os.utime(path, (now-3600000, now-3600000))

    channel = {"id": "test-tiktok", "service": "tiktok", "isDisconnected": False, "isLocked": False}
    replies = [httpx.Response(200, json={"data": {"channel": channel}}),
               httpx.Response(200, json={"data": {"createPost": {"__typename": "PostActionSuccess",
                                                                  "post": {"id": "draft-test", "status": "draft"}}}}),
               httpx.Response(200, json={"data": {"post": {"id": "draft-test", "status": "sent"}}})]
    with (patch.dict(os.environ, BUFFER_API_KEY="test-secret"),
          patch.object(buffer.httpx, "post", side_effect=replies) as http,
          patch.object(config, "RAW_RETENTION_HOURS", 168)):
        buffer.create(job, clip)
        token = http.call_args.kwargs["json"]["variables"]["video"].rsplit("/", 1)[1].removesuffix(".mp4")
        job["recover"] = True
        check("the retention sweep skips live jobs being recovered", storage.prune_recordings(now) == 0 and raw.exists())
        job.pop("recover")
        live.SESSIONS[job["id"]] = SimpleNamespace()
        check("the retention sweep skips live jobs whose session still owns raw files",
              storage.prune_recordings(now) == 0 and old_segment.exists())
        live.SESSIONS.pop(job["id"])
        clip["status"] = "queued"
        check("the retention sweep skips live jobs with pending renders", storage.prune_recordings(now) == 0 and raw.exists())
        clip["status"] = "done"
        check("expiry removes only old generated media and records loss of the editable raw clip",
              storage.prune_recordings(now) == 2 and not raw.exists() and not old_segment.exists()
              and recent_segment.exists() and unrelated.exists() and clip.get("raw_expired"))
        check("finished clips and active Buffer download tokens survive raw-file expiry",
              video.exists() and cover.exists() and buffer.video_file(token) == video)
        buffer.refresh_publication(job["id"], clip)
        check("Buffer status refresh uses a read-only request with the saved post ID",
              http.call_args.kwargs["json"]["query"].startswith("query ")
              and http.call_args.kwargs["json"]["variables"] == {"input": {"id": "draft-test"}}
              and buffer.status(job["id"], clip)["publication"] == "sent")
        buffer.create(job, clip)
        check("the status refresh and raw-file expiry preserve duplicate draft protection", http.call_count == 3)
        buffer.revoke(job["id"], clip["id"])
        check("the preserved Buffer download can still be revoked", buffer.video_file(token) is None and video.exists())
    with patch.object(discord_clips, "_EVENT_PATHS", {"status": "/sim-status"}):
        storage._warn_disk()
        storage._warn_disk()
    with patch.object(discord_clips, "_post", return_value=None) as sent:
        while discord_clips._send_next(None, db.discord_events, {"status": "/sim-status"}):
            pass
        check("disk warnings use the durable Discord status feed once per day",
              sent.call_count == 1 and "below the reserve" in sent.call_args.args[1]["content"])


def main():
    db.init()
    owner, _ = auth._create("hetzner-owner@example.test", "Owner")
    with db.engine.begin() as cn:
        cn.execute(db.users.update().where(db.users.c.id == owner.id).values(is_admin=True))
    guest = TestClient(server.app, base_url=BASE)
    admin = TestClient(server.app, base_url=BASE, cookies={auth.COOKIE: auth.start_session(owner.id)})
    check("the closed Hetzner profile denies anonymous self-registration",
          guest.post("/api/auth/signup", json={}, headers={"Origin": BASE}).status_code == 403)
    check("the draft-only profile denies the direct-publication route",
          admin.post("/api/posts", json={}, headers={"Origin": BASE}).status_code == 403)
    test_selection(owner)
    with ctx.as_user(owner.id):
        test_limits()
        test_retention_and_integrations()
    work.cleanup()
    return bool(fails)


if __name__ == "__main__":
    sys.exit(main())
