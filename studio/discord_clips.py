"""Send finished-clip notifications from a separate worker."""
import http.client
import hashlib
import json
import logging
import math
import re
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from . import config, creators, ctx, db, jobs

_log = logging.getLogger("peakcut.discord")
_wake = threading.Event()
_pending = db.discord_clips
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER = 60.0


def _webhook_path(url):
    match = re.fullmatch(r"https://discord\.com(/api/(?:v[0-9]+/)?webhooks/[0-9]+/[A-Za-z0-9_-]+)", url)
    return match[1] if match else None


_WEBHOOK_PATH = _webhook_path(config.DISCORD_CLIPS_WEBHOOK_URL)
_EVENT_PATHS = {"live": _webhook_path(config.DISCORD_LIVE_WEBHOOK_URL),
                "status": _webhook_path(config.DISCORD_STATUS_WEBHOOK_URL)}


def enqueue(job, clip):
    """Remember this clip once, even if two workers report it at the same time."""
    if not _WEBHOOK_PATH:
        return
    _enqueue(_pending, user_id=job["user"], job_id=job["id"], clip_id=clip["id"])


def event(feed, event_id, message):
    path = _WEBHOOK_PATH if feed == "clips" else _EVENT_PATHS.get(feed)
    if path:
        digest = hashlib.sha256(event_id.encode()).hexdigest()
        _enqueue(db.discord_events, user_id=ctx.uid(), feed=feed, event_id=digest, message=message[:1000])


def _enqueue(table, **values):
    try:
        with db.engine.begin() as cn:
            insert = sqlite.insert if cn.dialect.name == "sqlite" else postgresql.insert
            cn.execute(insert(table).values(**values, due_at=db.now()).on_conflict_do_nothing())
    except Exception:  # noqa: BLE001 - a notification error must not fail the clip
        _log.warning("Could not queue a Discord clip notification.")
    else:
        _wake.set()


def _key(table, row):
    return sa.and_(*(col == row._mapping[col.key] for col in table.primary_key))


def _claim(table, *filters):
    due = table.c.due_at <= db.now()
    with db.engine.begin() as cn:
        row = cn.execute(sa.select(table).where(due, *filters).order_by(table.c.due_at).limit(1)).first()
        # Record the attempt before sending. A restart cannot safely replay an unconfirmed request.
        if row and cn.execute(table.update().where(_key(table, row), due, table.c.attempts == row.attempts)
                              .values(due_at=None, attempts=row.attempts + 1)).rowcount == 1:
            return row
    return None


def _video_link(job_id, name):
    if not config.ACCOUNTS or any(c.isspace() for c in config.PUBLIC_URL):
        return None
    try:
        url = urlsplit(config.PUBLIC_URL)
    except ValueError:
        return None
    if url.scheme != "https" or not url.hostname or url.username is not None or url.query or url.fragment:
        return None
    return f"{config.PUBLIC_URL}/media/{quote(job_id, safe='')}/{quote(name, safe='')}"


def _payload(row):
    with ctx.as_user(row.user_id):
        job = jobs.get(row.job_id)
        clip = next((c for c in job["clips"] if c["id"] == row.clip_id), None) if job else None
        if not clip or not clip.get("file") or not (Path(job["dir"]) / "clips" / clip["file"]).is_file():
            return None
        creator = creators.get(job.get("creator")) or {}
    streamer = job.get("uploader") or creator.get("name") or "Unknown streamer"
    title = clip.get("title") or clip.get("hook") or "Clip"
    embed = {"title": " ".join(title.split())[:160] or "Clip",
             "fields": [{"name": "Streamer", "value": " ".join(streamer.split())[:120] or "Unknown streamer"},
                        {"name": "Duration", "value": f"{clip['end'] - clip['start']:.1f} s"}]}
    link = _video_link(job["id"], clip["file"])
    if link:
        embed["url"] = link
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def _post(path, payload):
    """Return a minimum retry delay when we know Discord did not accept the message."""
    body = json.dumps(payload).encode("utf-8")
    conn = http.client.HTTPSConnection("discord.com", timeout=5)
    try:
        try:
            conn.connect()
        except OSError:
            return 0.0
        try:
            conn.request("POST", path + "?wait=true", body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
        except (OSError, http.client.HTTPException):
            # A lost response can mean Discord already created the message.
            _log.warning("Discord did not confirm a clip notification.")
            return None
        if response.status == 429:
            try:
                delay = float(response.getheader("Retry-After") or 0)
            except ValueError:
                delay = 0.0
            if math.isfinite(delay) and 0 <= delay <= _MAX_RETRY_AFTER:
                return delay
        if not 200 <= response.status < 300:
            _log.warning("Discord clip notification ended with HTTP %d.", response.status)
        return None
    finally:
        conn.close()


def _send_next(path, table=_pending, feeds=None):
    row = _claim(table, table.c.feed.in_(feeds)) if feeds else _claim(table)
    if row is None:
        return False
    payload = {"content": row.message, "allowed_mentions": {"parse": []}} if feeds else _payload(row)
    if payload is None:
        return True
    delay = _post(feeds[row.feed] if feeds else path, payload)
    if delay is None:
        return True
    attempts = row.attempts + 1
    if attempts >= _MAX_ATTEMPTS:
        _log.warning("Discord clip notification ran out of attempts.")
    else:
        with db.engine.begin() as cn:
            cn.execute(table.update().where(_key(table, row)).values(due_at=db.now() + max(delay, 2 ** attempts)))
    return True


def _run(path, feeds=None):
    while True:
        _wake.clear()
        try:
            while (_send_next(None, db.discord_events, feeds) if feeds else _send_next(path)):
                pass
        except Exception:  # noqa: BLE001 - neither exception details nor the webhook may go into logs
            _log.warning("Could not process a Discord clip notification.")
        _wake.wait(2)


def start():
    if _WEBHOOK_PATH:
        threading.Thread(target=_run, args=(_WEBHOOK_PATH,), daemon=True, name="discord-clips").start()
    elif config.DISCORD_CLIPS_WEBHOOK_URL:
        _log.warning("DISCORD_CLIPS_WEBHOOK_URL must be an HTTPS webhook on discord.com.")
    feeds = {feed: path for feed, path in {**_EVENT_PATHS, "clips": _WEBHOOK_PATH}.items() if path}
    if feeds:
        threading.Thread(target=_run, args=(None, feeds), daemon=True, name="discord-events").start()
