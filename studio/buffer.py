"""Manually save finished PeakCut clips as Buffer drafts for one connected TikTok channel."""
import hashlib
import ipaddress
import math
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from . import config, ctx, db, jobs

_API = "https://api.buffer.com"
_CHANNEL = """query TikTokChannel($id: ChannelId!) {
  channel(input: {id: $id}) { id service isDisconnected isLocked }
}"""
_CREATE = """mutation SaveTikTokDraft($channel: ChannelId!, $text: String!, $video: String!) {
  createPost(input: {
    channelId: $channel, text: $text, assets: [{video: {url: $video}}],
    mode: addToQueue, schedulingType: notification, saveToDraft: true, needsApproval: false
  }) {
    __typename
    ... on PostActionSuccess { post { id status } }
    ... on MutationError { message }
  }
}"""
_table = db.buffer_drafts
_cooldown_lock = threading.Lock()
_retry_at = 0.0
_UNCERTAIN = "Buffer may already have saved this draft. Check the TikTok drafts in Buffer."


class Error(Exception):
    def __init__(self, status, message, *, uncertain=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain
        self.retry_after = retry_after


def configured():
    return bool(config.ACCOUNTS and config.BUFFER_TIKTOK_CHANNEL_ID and config.buffer_api_key())


def _public_base():
    try:
        url = urlsplit(config.PUBLIC_URL)
        host = (url.hostname or "").rstrip(".")
        if any(c.isspace() or c in "\\<>" for c in config.PUBLIC_URL) or url.scheme != "https" or (
                url.username is not None or url.query or url.fragment or url.path):
            return None
        try:
            public = ipaddress.ip_address(host).is_global
        except ValueError:
            public = "." in host and not host.endswith((".localhost", ".local", ".internal"))
        if public:
            return config.PUBLIC_URL
    except ValueError:
        pass
    return None


def _rate_limited(headers, attempt):
    global _retry_at
    try:
        delay = int(headers.get("Retry-After", "0"))
    except ValueError:
        delay = 0
    delay = max(2 ** attempt, min(30 * 86400, delay))
    with _cooldown_lock:
        _retry_at = max(_retry_at, time.monotonic() + delay)
    return delay


def _check_cooldown():
    with _cooldown_lock:
        delay = math.ceil(_retry_at - time.monotonic())
    if delay > 0:
        wait = f"{delay} seconds" if delay < 60 else f"about {math.ceil(delay / 60)} minutes"
        raise Error(429, f"Buffer is rate limiting requests. Try again in {wait}.", retry_after=delay)


def _request(key, query, variables, *, mutation=False):
    for attempt in range(1, 4):
        _check_cooldown()
        try:
            reply = httpx.post(_API, headers={"Authorization": f"Bearer {key}"},
                               json={"query": query, "variables": variables}, timeout=10,
                               follow_redirects=False, trust_env=False)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise Error(503, "Could not connect to Buffer. Try again shortly.") from None
        except (httpx.RequestError, httpx.InvalidURL):
            raise Error(502, _UNCERTAIN if mutation else "Buffer did not respond.", uncertain=mutation) from None

        try:
            body = reply.json()
        except ValueError:
            body = None
        data = body.get("data") if isinstance(body, dict) else None
        errors = body.get("errors") if isinstance(body, dict) else None
        codes = {str(error["extensions"].get("code")) for error in errors
                 if isinstance(error, dict) and isinstance(error.get("extensions"), dict)} if isinstance(errors, list) else set()
        if reply.status_code == 429 or (data is None and codes == {"RATE_LIMIT_EXCEEDED"}):
            delay = _rate_limited(reply.headers, attempt)
            if attempt < 3 and delay <= 4:
                time.sleep(delay)
                continue
            _check_cooldown()
            raise Error(429, "Buffer is rate limiting requests. Try again shortly.", retry_after=delay)
        if reply.status_code in (401, 403) or (data is None and codes and codes <= {"UNAUTHORIZED", "FORBIDDEN"}):
            raise Error(502, "Buffer refused access. Check the API key and channel permissions.")
        if data is None and codes == {"NOT_FOUND"}:
            raise Error(502, "Buffer could not find this channel. Check BUFFER_TIKTOK_CHANNEL_ID.")
        if reply.is_success and not errors and isinstance(data, dict):
            return data
        raise Error(502, _UNCERTAIN if mutation else "Buffer could not check the channel.", uncertain=mutation)


def _hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _clip_file(job, clip):
    name = clip.get("file", "")
    if clip.get("status") != "done" or not re.fullmatch(r"[\w-]+\.mp4", name or ""):
        return None
    path = Path(job["dir"]) / "clips" / name
    return path if path.is_file() else None


def _match(job_id, clip_id):
    return sa.and_(_table.c.user_id == ctx.uid(), _table.c.job_id == job_id, _table.c.clip_id == clip_id,
                   _table.c.channel_id == config.BUFFER_TIKTOK_CHANNEL_ID)


def _draft_status(row, clip):
    return {"state": ("draft" if row.post_id else "unconfirmed") if row else "none",
            "video_link": bool(row and row.media_hash and row.version == clip.get("version", 0)
                               and clip.get("status") == "done")}


def _existing_draft(row, clip):
    if row.post_id:
        return _draft_status(row, clip)
    raise Error(409, _UNCERTAIN)


def status(job_id, clip):
    with db.engine.connect() as cn:
        row = cn.execute(sa.select(_table).where(_match(job_id, clip["id"]))).first()
    return _draft_status(row, clip)


def revoke(job_id, clip_id):
    with db.engine.begin() as cn:
        cn.execute(_table.update().where(_match(job_id, clip_id)).values(media_hash=None))


def video_file(token):
    if not configured() or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    with db.engine.connect() as cn:
        row = cn.execute(sa.select(_table).where(_table.c.media_hash == _hash(token),
                                                _table.c.channel_id == config.BUFFER_TIKTOK_CHANNEL_ID)).first()
    if not row:
        return None
    with ctx.as_user(row.user_id):
        job = jobs.get(row.job_id)
        clip = next((c for c in job["clips"] if c["id"] == row.clip_id), None) if job else None
        if clip and clip.get("version", 0) == row.version:
            return _clip_file(job, clip)
    return None


def _caption(clip, title, description):
    if title is None:
        title = clip.get("title") or clip.get("hook") or "Clip"
    if description is None:
        description = clip.get("description") or ""
    if not isinstance(title, str) or not isinstance(description, str):
        raise Error(400, "The title and description must be text.")
    text = "\n\n".join(filter(None, (title.strip() or "Clip", description.strip())))
    try:
        size = len(text.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise Error(400, "The title or description contains invalid text.") from None
    if size > 2200:
        raise Error(400, "Shorten the title and description to fit TikTok's 2,200-character video caption limit.")
    return text


def create(job, clip, title=None, description=None):
    key, channel_id = config.buffer_api_key(), config.BUFFER_TIKTOK_CHANNEL_ID
    if not config.ACCOUNTS:
        raise Error(503, "Enable PeakCut sign-in before connecting Buffer.")
    if not key or not channel_id:
        raise Error(503, "Set BUFFER_API_KEY and BUFFER_TIKTOK_CHANNEL_ID to enable Buffer drafts.")
    base = _public_base()
    if not base:
        raise Error(400, "Set PEAKCUT_PUBLIC_URL to the public HTTPS address Buffer can use to fetch the video.")
    if not _clip_file(job, clip):
        raise Error(409, "Wait until the clip has finished rendering.")
    version = clip.get("version", 0)
    text = _caption(clip, title, description)
    match = _match(job["id"], clip["id"])
    with db.engine.connect() as cn:
        existing = cn.execute(sa.select(_table).where(match)).first()
    if existing:
        return _existing_draft(existing, clip)

    channel = _request(key, _CHANNEL, {"id": channel_id}).get("channel")
    if not isinstance(channel, dict) or channel.get("id") != channel_id or channel.get("service") != "tiktok":
        raise Error(400, "BUFFER_TIKTOK_CHANNEL_ID must be the ID of a TikTok channel in Buffer.")
    if channel.get("isDisconnected") is not False or channel.get("isLocked") is not False:
        raise Error(400, "The TikTok channel is disconnected or locked in Buffer.")
    token = secrets.token_urlsafe(32)
    if clip.get("version", 0) != version or not _clip_file(job, clip):
        raise Error(409, "The clip changed. Open the finished clip and try again.")
    with db.engine.begin() as cn:
        insert = sqlite.insert if cn.dialect.name == "sqlite" else postgresql.insert
        added = cn.execute(insert(_table).values(user_id=ctx.uid(), job_id=job["id"], clip_id=clip["id"],
                                                 channel_id=channel_id, version=version, media_hash=_hash(token))
                           .on_conflict_do_nothing()).rowcount
        if not added:
            row = cn.execute(sa.select(_table).where(match)).first()
            if row:
                return _existing_draft(row, clip)
            raise Error(409, "Another request was saving this draft. Try again.")

    try:
        result = _request(key, _CREATE, {"channel": channel_id, "text": text,
                                        "video": f"{base}/pub/buffer/{token}.mp4"}, mutation=True).get("createPost")
        if isinstance(result, dict) and result.get("__typename") in (
                "InvalidInputError", "LimitReachedError", "NotFoundError", "UnauthorizedError"):
            raise Error(502, "Buffer rejected the draft. Check the channel permissions, caption and video access.")
        post = result.get("post") if isinstance(result, dict) and result.get("__typename") == "PostActionSuccess" else None
        if not isinstance(post, dict) or post.get("status") != "draft" or not isinstance(post.get("id"), str) or not post["id"]:
            raise Error(502, _UNCERTAIN, uncertain=True)
    except Error as exc:
        if not exc.uncertain:
            with db.engine.begin() as cn:
                cn.execute(_table.delete().where(match))
        raise
    with db.engine.begin() as cn:
        cn.execute(_table.update().where(match).values(post_id=post["id"]))
    return status(job["id"], clip)
