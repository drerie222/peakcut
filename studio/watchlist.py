"""A gaming watchlist backed by the existing creator, recording and rendering pipeline."""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from . import accounts, auth, buffer, config, creators, ctx, discord_clips, jobs, load, profiles

PRESET = (
    ("A", "IShowSpeed", "youtube", "@IShowSpeed"), ("A", "Kai Cenat", "twitch", "kaicenat"),
    ("A", "xQc", "twitch", "xqc"), ("A", "N3on", "kick", "n3on"), ("A", "Adin Ross", "kick", "adinross"),
    ("A", "summit1g", "twitch", "summit1g"), ("A", "LIRIK", "twitch", "lirik"),
    ("A", "shroud", "twitch", "shroud"), ("A", "CaseOh", "twitch", "caseoh_"),
    ("A", "Jynxzi", "twitch", "jynxzi"), ("A", "Sodapoppin", "twitch", "sodapoppin"),
    ("A", "Sykkuno", "twitch", "sykkuno"), ("B", "Buddha", "twitch", "buddha"),
    ("B", "Lord Kebun", "kick", "lordkebun"), ("B", "Ramee", "twitch", "ramee"),
    ("B", "AnthonyZ", "twitch", "anthonyz"), ("B", "Zerkaa", "twitch", "zerkaa"),
    ("B", "Tee Grizzley", "twitch", "teegrizzley"), ("B", "Timmac", "twitch", "timmac"),
    ("B", "Ray__C", "twitch", "ray__c"), ("B", "koil", "twitch", "koil"), ("B", "Whippy", "twitch", "whippy"),
    ("B", "dasMEHDI", "twitch", "dasmehdi"), ("B", "PENTA", "twitch", "penta"),
    ("C", "TGG", "youtube", "@TGG_"), ("C", "DarkViperAU", "twitch", "darkviperau"),
    ("C", "Spaceboy", "twitch", "spaceboy"), ("C", "Lysium", "twitch", "lysium"),
    ("C", "GTAWiseGuy", "twitch", "gtawiseguy"), ("C", "RatedEpicz", "twitch", "ratedepicz"),
)
_HOSTS = {"youtube": "www.youtube.com", "twitch": "www.twitch.tv", "kick": "kick.com"}
_PLATFORMS = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}
_probes = ThreadPoolExecutor(max_workers=4, thread_name_prefix="watchlist")


def channel_url(raw):
    """Validate an editable watchlist entry and share lookups across equivalent URLs."""
    url = urlsplit(raw if "://" in raw else "https://" + raw)
    host = (url.hostname or "").removeprefix("www.").removeprefix("m.")
    platform = next((p for p, h in _HOSTS.items() if h.removeprefix("www.") == host), "")
    path = url.path.rstrip("/")
    if platform == "youtube":
        path = re.sub(r"/(?:live|featured|videos|streams)$", "", path)
        valid = re.fullmatch(r"/(?:@[\w.-]+|(?:channel|c|user)/[\w.-]+)", path)
    else:
        valid = re.fullmatch(r"/[A-Za-z0-9_][A-Za-z0-9_-]{1,39}", path)
        path = path.lower()
    if not platform or not valid or url.scheme not in ("http", "https") or (
            url.username is not None or url.port is not None or url.query or url.fragment):
        raise ValueError("Use a channel link from YouTube, Twitch or Kick.")
    return platform, f"https://{_HOSTS[platform]}{path}"


def seed():
    """Import once without starting recordings or replacing settings already in use."""
    with creators._lock:
        rows = creators.all_creators()
        known = {c.get("watchlist", {}).get("key") for c in rows}
        existing = {}
        for c in rows:
            for url in c["channels"].values():
                try:
                    _, normalized = channel_url(url)
                    existing[normalized.lower()] = c
                except ValueError:
                    continue
        added = 0
        for order, (priority, name, platform, path) in enumerate(PRESET):
            url = f"https://{_HOSTS[platform]}/{path}"
            key = f"{platform}:{path.lower()}"
            if key in known:
                continue
            marker = {"key": key, "priority": priority, "order": order}
            old = existing.get(url.lower())
            if old and not old.get("watchlist"):
                try:
                    channel_url(old["channel"])
                except ValueError:
                    old = None
            if old and not old.get("watchlist"):
                old["watchlist"] = marker
                creators._write(old)
            else:
                if len(rows) >= config.MAX_CREATORS:
                    continue
                fresh = creators.create({"name": name, "channel": url, "layout": "gameplay",
                                         "min_len": 25, "max_len": 90, "trigger_words": ""}, watchlist=marker)
                rows.append(fresh)
            added += 1
        return added


def _probe(creator):
    try:
        platform, url = channel_url(creator["channel"])
    except ValueError:
        return "", {"error": "Check the channel link."}
    return _PLATFORMS[platform], profiles.live_shared(_PLATFORMS[platform], url)


def _observation(c, platform, info):
    old = c.get("watch_status") or {}
    error = info.get("error") or ""
    state = "unknown" if error else "live" if info.get("live") else "offline"
    broadcast = old.get("broadcast", "")
    if state == "live":
        ident = info.get("started") or (info.get("embed") if platform == "YouTube" else None)
        broadcast = str(ident) if ident else broadcast or str(int(time.time()))
    elif state == "offline":
        broadcast = ""
    title = str(info.get("title") or old.get("title") or "")[:160] if state != "offline" else ""
    value = {"state": state, "platform": platform, "title": title, "error": error, "broadcast": broadcast,
             "embed": info.get("embed") or "", "started": info.get("started")}
    current = creators.watch_status(c["id"], c["channel"], value)
    if not current:
        return None
    # A failed check keeps the last broadcast identity so recovery never looks like a new stream.
    if state == "live" and broadcast != old.get("broadcast"):
        discord_clips.event("live", f"{c['id']}:{c['channel']}:{broadcast}:live",
                            f"{c['name']} is live on {platform}. {title}")
    if state == "offline" and old.get("broadcast"):
        discord_clips.event("live", f"{c['id']}:{c['channel']}:{old['broadcast']}:offline",
                            f"{c['name']} went offline on {platform}.")
    if error and error != old.get("error"):
        day = time.strftime("%Y-%m-%d")
        discord_clips.event("status", f"{day}:{c['id']}:{error}", f"{c['name']}. {error}")
    return current


def recording_limit():
    return max(0, min(int(accounts.settings()["watch_recordings"]), config.MAX_LIVE_TOTAL, config.MAX_LIVE_PER_USER))


def poll(todo):
    """Check every enabled channel, then take available recording slots in priority order."""
    from . import live
    observed = list(_probes.map(_probe, todo))
    for c, (platform, info) in zip(todo, observed):
        _observation(c, platform, info)
    # Fetch the current settings after the network checks. A disabled or edited channel stays stopped.
    limit = recording_limit()
    full, busy = load.disk_full(), load.busy()
    day = time.strftime("%Y-%m-%d")
    health = "Disk space is low." if full else "CPU or memory load is high." if busy else "Watchlist is checking channels."
    discord_clips.event("status", f"{day}:{health}", health)
    history = jobs.list_jobs()
    for c in todo:
        current = creators.get(c["id"])
        if not current or not current["auto_watch"] or current["channel"] != c["channel"]:
            continue
        status = current.get("watch_status") or {}
        if status.get("state") != "live" or live.active_for(c["id"]) or len(live.active()) >= limit or full or busy:
            continue
        broadcast = status["broadcast"]
        previous = [j for j in history if j["creator"] == c["id"] and j.get("watch_broadcast") == broadcast
                    and j.get("watch_channel") == c["channel"]]
        if len(previous) >= 3 or any(j.get("watch_stopped") for j in previous):
            continue
        if previous and time.time() - previous[0]["created"] < (120 * 2 ** (len(previous) - 1)):
            continue
        try:
            live.start_session(current, watch=status, watch_limit=limit)
        except ValueError as exc:
            message = str(exc)
            creators.log(c["id"], message)
            discord_clips.event("status", f"{day}:{c['id']}:start:{message}", f"{c['name']}. {message}")


def on_clip_ready(job, clip):
    if job.get("watch_broadcast") and accounts.settings()["watch_buffer"] and clip.get("score", 0) > 0:
        clip["watch_export"] = {"state": "queued", "due": time.time(), "attempts": 0}
        jobs.save(job)


def _export(job, clip, state):
    if not accounts.settings()["watch_buffer"] or not buffer.configured():
        return
    owner = auth.get_user(job["user"])
    if not owner or owner.disabled or not owner.is_admin:
        return
    state["attempts"] += 1
    try:
        description = "\n\n".join(filter(None, (clip.get("description"), " ".join("#" + h for h in clip.get("hashtags", [])))))
        buffer.create(job, clip, description=description)
    except buffer.Error as exc:
        if (exc.status in (429, 503) and not exc.uncertain and state["attempts"] < 3):
            state.update(due=time.time() + max(exc.retry_after or 0, 30 * 2 ** (state["attempts"] - 1)), error=str(exc))
        else:
            state.update(state="failed", error=str(exc))
            discord_clips.event("status", f"{job['id']}:{clip['id']}:buffer-failed", "A Buffer draft needs attention. Check the watchlist.")
    except Exception:  # noqa: BLE001 - internal failures must not hold up other drafts or expose settings
        state.update(state="failed", error="Could not complete the Buffer export. Check the draft in Buffer.")
        discord_clips.event("status", f"{job['id']}:{clip['id']}:buffer-failed", "A Buffer draft needs attention. Check the watchlist.")
    else:
        state.update(state="draft", error="")
        discord_clips.event("clips", f"{job['id']}:{clip['id']}:buffer-draft",
                            f"TikTok draft ready for review in Buffer. {clip.get('title') or 'Clip'}")
    jobs.save(job)


def export_round():
    """Recover queued draft exports after a restart and leave ambiguous mutations with Buffer's reservation."""
    now = time.time()
    for job in jobs.all_jobs():
        if not job.get("watch_broadcast"):
            continue
        with ctx.as_user(job["user"]):
            changed = False
            if job.get("error") and not job.get("watch_error_reported"):
                discord_clips.event("status", f"{job['id']}:recording", "A watchlist recording failed. Check the watchlist.")
                job["watch_error_reported"] = changed = True
            for clip in list(job["clips"]):
                state = clip.get("watch_export") or {}
                if clip.get("status") == "error" and not clip.get("watch_error_reported"):
                    discord_clips.event("status", f"{job['id']}:{clip['id']}:render", "A watchlist clip failed. Check the watchlist.")
                    clip["watch_error_reported"] = changed = True
                if clip.get("status") == "done" and state.get("state") == "queued" and state["due"] <= now:
                    _export(job, clip, state)
            if changed:
                jobs.save(job)


def _exports():
    import logging
    while True:
        try:
            export_round()
        except Exception:  # noqa: BLE001 - errors from external services can contain private URLs
            logging.getLogger(__name__).warning("The watchlist could not process pending exports.")
        time.sleep(10)


def start():
    import threading
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_exports, daemon=True, name="watch-exports").start()
