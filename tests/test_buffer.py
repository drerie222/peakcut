"""Buffer draft export with fake API responses and PeakCut's real routes and database."""
import os
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

BASE = "https://peakcut.example.test"
data = tempfile.TemporaryDirectory(prefix="pc-buffer-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="", PEAKCUT_ACCOUNTS="1",
                  PEAKCUT_PUBLIC_URL=BASE, BUFFER_API_KEY="", BUFFER_TIKTOK_CHANNEL_ID="channel-test")
sys.path.insert(0, ".")

import httpx  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from studio import auth, buffer, config, ctx, db, jobs, server  # noqa: E402

KEY = secrets.token_urlsafe(24)
VIDEO = b"video fixture"
ORIGIN = {"Origin": BASE}
CHANNEL = {"id": "channel-test", "service": "tiktok", "isDisconnected": False, "isLocked": False}
fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def channel(**changes):
    return httpx.Response(200, json={"data": {"channel": dict(CHANNEL, **changes)}})


def saved(status="draft"):
    return httpx.Response(200, json={"data": {"createPost": {
        "__typename": "PostActionSuccess", "post": {"id": "post-test", "status": status}}}})


def new_clip(job, cid):
    clip = dict(id=cid, file=f"{cid}.mp4", status="done", version=1, title="Example title", description="Example details")
    (Path(job["dir"]) / "clips" / clip["file"]).write_bytes(VIDEO)
    job["clips"].append(clip)
    return f"/api/admin/jobs/{job['id']}/clips/{cid}/buffer-draft", clip


def test_routes(admin, guest, member, job):
    path, clip = new_clip(job, "clip01")
    with patch.object(buffer.httpx, "post") as post:
        check("a missing key disables Buffer without making requests", admin.post(path, headers=ORIGIN).status_code == 503
              and not post.called and not admin.get("/api/status").json()["buffer_drafts"])

    with patch.dict(os.environ, BUFFER_API_KEY=KEY), patch.object(buffer.httpx, "post") as post:
        check("signed-out and non-admin users cannot create drafts",
              guest.post(path, headers=ORIGIN).status_code == 404 and member.post(path, headers=ORIGIN).status_code == 404)
        check("cross-site requests cannot create drafts", admin.post(path, headers={"Origin": "https://other.example"}).status_code == 403)
        check("callers cannot set a channel, video URL or publish mode",
              admin.post(path, headers=ORIGIN, json={"saveToDraft": False, "video": BASE}).status_code == 400)
        with patch.object(config, "PUBLIC_URL", "https://127.0.0.1:8765"):
            check("a loopback server cannot be used as Buffer's video source",
                  admin.post(path, headers=ORIGIN).status_code == 400)
        clip["status"] = "rendering"
        check("only finished clips with a file can be exported", admin.post(path, headers=ORIGIN).status_code == 409)
        clip["status"] = "done"
        check("video captions respect Buffer's UTF-16 limit",
              admin.post(path, headers=ORIGIN, json={"title": "🚗" * 1101}).status_code == 400)
        check("validation and access checks make no API requests", not post.called)
        info = admin.get("/api/status")
        check("the clip editor sees availability without receiving credentials",
              info.json()["buffer_drafts"] and KEY not in info.text and not member.get("/api/status").json()["buffer_drafts"])

        post.side_effect = [channel(service="youtube")]
        check("the target must be a TikTok channel", admin.post(path, headers=ORIGIN).status_code == 400 and post.call_count == 1)
        post.reset_mock()
        post.side_effect = [channel(isDisconnected=True)]
        check("a disconnected TikTok channel is rejected before creating a draft",
              admin.post(path, headers=ORIGIN).status_code == 400 and post.call_count == 1)

        def api_reply(url, **kwargs):
            if "mutation" not in kwargs["json"]["query"]:
                return channel()
            try:
                buffer.create(job, clip)
            except buffer.Error as exc:
                check("an overlapping request sees the persistent reservation", exc.status == 409)
            return saved()

        post.reset_mock()
        post.side_effect = api_reply
        result = admin.post(path, headers=ORIGIN, json={"title": "Edited title", "description": "Edited notes"})
        check("the authorized action returns a saved draft", result.status_code == 200
              and result.json() == {"state": "draft", "video_link": True})
        check("Buffer is called over HTTPS with the environment key and redirects off",
              all(call.args == ("https://api.buffer.com",) and call.kwargs["headers"]["Authorization"] == f"Bearer {KEY}"
                  and not call.kwargs["follow_redirects"] for call in post.call_args_list))
        mutation = post.call_args.kwargs["json"]
        check("the mutation is locked to unscheduled manual-review drafts",
              "saveToDraft: true" in mutation["query"] and "schedulingType: notification" in mutation["query"]
              and "shareNow" not in mutation["query"] and "dueAt" not in mutation["query"])
        check("Buffer receives the title and description together and the configured channel only",
              mutation["variables"]["text"] == "Edited title\n\nEdited notes" and mutation["variables"]["channel"] == "channel-test")
        video_url = mutation["variables"]["video"]
        token_path = urlsplit(video_url).path
        check("Buffer gets a direct HTTPS asset link", video_url.startswith(BASE + "/pub/buffer/"))
        fetch = guest.get(token_path, headers={"Range": "bytes=0-4"})
        check("the exact link streams bytes without sign-in and forbids caching",
              fetch.status_code == 206 and fetch.content == VIDEO[:5] and fetch.headers["cache-control"] == "private, no-store")
        check("Buffer can also check the file with HEAD", guest.head(token_path).status_code == 200)
        check("another token cannot read the clip", guest.get("/pub/buffer/" + "x" * 43 + ".mp4").status_code == 404)
        check("the database contains no API key or usable asset token", not stored_secret(video_url))
        db.engine.dispose()
        check("repeat requests reuse the saved draft across database connections", admin.post(path, headers=ORIGIN).status_code == 200
              and post.call_count == 2)
        clip["version"] = 2
        check("a remake invalidates the old link", guest.get(token_path).status_code == 404)
        clip["version"] = 1
        check("revoking the file link keeps the draft and its duplicate protection",
              admin.delete(path, headers=ORIGIN).json() == {"state": "draft", "video_link": False}
              and guest.get(token_path).status_code == 404
              and admin.post(path, headers=ORIGIN).json()["state"] == "draft" and post.call_count == 2)


def stored_secret(video_url):
    with db.engine.connect() as cn:
        rows = repr(cn.execute(sa.select(db.buffer_drafts)).all())
    return KEY in rows or urlsplit(video_url).path.split("/")[-1].removesuffix(".mp4") in rows


def test_errors(client, job):
    path, _ = new_clip(job, "rate-limit")
    other, _ = new_clip(job, "other")
    waits = []
    clock = SimpleNamespace(monotonic=lambda: 10 + sum(waits), sleep=waits.append)
    with (patch.dict(os.environ, BUFFER_API_KEY=KEY), patch.object(buffer, "time", clock),
          patch.object(buffer, "_retry_at", 0), patch.object(buffer.httpx, "post") as post):
        post.side_effect = [channel(), httpx.Response(429, headers={"Retry-After": "3"}),
                            httpx.Response(429, headers={"Retry-After": "1"}), saved()]
        check("short rate limits are retried with Buffer's delay and exponential backoff",
              client.post(path, headers=ORIGIN).status_code == 200 and waits == [3, 4] and post.call_count == 4)
        post.reset_mock()
        post.side_effect = [httpx.Response(429, headers={"Retry-After": "753"})]
        r = client.post(other, headers=ORIGIN)
        check("long rate limits surface Retry-After without waiting on the request",
              r.status_code == 429 and r.headers.get("retry-after") == "753" and waits == [3, 4])
        check("the cooldown also stops new requests before they reach Buffer",
              client.post(other, headers=ORIGIN).status_code == 429 and post.call_count == 1)
        waits.append(753)
        post.reset_mock()
        post.side_effect = [httpx.Response(403, json={"secret": KEY})]
        denied = client.post(other, headers=ORIGIN)
        check("permission errors explain setup without leaking raw response details",
              denied.status_code == 502 and "permissions" in denied.text and KEY not in denied.text)
        post.reset_mock()
        post.side_effect = [channel(), httpx.Response(200, json={"data": {"createPost": {
            "__typename": "InvalidInputError", "message": KEY}}})]
        invalid = client.post(other, headers=ORIGIN)
        check("typed GraphQL rejections clear the reservation without leaking raw details",
              invalid.status_code == 502 and KEY not in invalid.text and client.get(other).json()["state"] == "none")
        post.reset_mock()
        post.side_effect = [channel(), httpx.ReadTimeout(KEY)]
        unknown = client.post(other, headers=ORIGIN)
        check("an ambiguous timeout keeps the reservation without retrying",
              unknown.status_code == 502 and KEY not in unknown.text and post.call_count == 2
              and client.get(other).json()["state"] == "unconfirmed")
        db.engine.dispose()
        check("an unconfirmed mutation is never replayed, even after reconnecting",
              client.post(other, headers=ORIGIN).status_code == 409 and post.call_count == 2)

        wrong, _ = new_clip(job, "wrong-status")
        post.reset_mock()
        post.side_effect = [channel(), saved("scheduled")]
        check("an unexpected non-draft result is never reported as successful",
              client.post(wrong, headers=ORIGIN).status_code == 502 and client.get(wrong).json()["state"] == "unconfirmed")


def main():
    db.init()
    owner, _ = auth._create("owner@example.test", "Owner")
    member, _ = auth._create("member@example.test", "Member")
    with db.engine.begin() as cn:
        cn.execute(db.users.update().where(db.users.c.id == owner.id).values(is_admin=True))
    admin, guest, reader = [TestClient(server.app, base_url=BASE) for _ in range(3)]
    admin.cookies.set(auth.COOKIE, auth.start_session(owner.id))
    reader.cookies.set(auth.COOKIE, auth.start_session(member.id))
    with ctx.as_user(member.id):
        member_job = jobs.create({"type": "file", "name": "source.mp4"}, {}, enqueue=False)
        (Path(member_job["dir"]) / "clips").mkdir()
        member_path, _ = new_clip(member_job, "private")
    check("a PeakCut administrator cannot export another owner's clip",
          admin.post(member_path, headers=ORIGIN).status_code == 404)
    with ctx.as_user(owner.id):
        job = jobs.create({"type": "file", "name": "source.mp4"}, {}, enqueue=False)
        (Path(job["dir"]) / "clips").mkdir()
    test_routes(admin, guest, reader, job)
    test_errors(admin, job)


try:
    main()
finally:
    db.engine.dispose()
    data.cleanup()

sys.exit(1 if fails else 0)
