"""A gaming watchlist backed by the existing creator, recording and rendering pipeline."""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from . import accounts, auth, buffer, config, creators, ctx, discord_clips, jobs, load, profiles, selection

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
_probes = ThreadPoolExecutor(max_workers=2, thread_name_prefix="watchlist")
_decisions = {}
_challengers = {}


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
    return _PLATFORMS[platform], profiles.live_shared(_PLATFORMS[platform], url, pace=True)


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
    since = old.get("live_since") if broadcast and broadcast == old.get("broadcast") else None
    value = {"state": state, "platform": platform, "title": title, "error": error, "broadcast": broadcast,
             "live_since": since or (time.time() if broadcast else None),
             "category": str(info.get("category") or "")[:100],
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


def decisions():
    return _decisions.get(ctx.uid(), {})


def _show(rows):
    if rows != decisions():
        _decisions[ctx.uid()] = rows
        ctx.bump()


def _can_record(c, history, now):
    status = c.get("watch_status") or {}
    if status.get("state") != "live" or not status.get("live_since"):
        return False
    prior = [j for j in history if j.get("watch_broadcast") == status["broadcast"]
             and j.get("watch_channel") == c["channel"]]
    if any(j.get("watch_stopped") for j in prior):
        return False
    failed = [j for j in prior if j.get("status") == "error" and not j.get("duration")]
    return len(failed) < 3 and (not failed or now - failed[0]["created"] >= 120 * 2 ** (len(failed) - 1))


def _rotate(active, waiting, rows, last_switch, now):
    uid = ctx.uid()
    if not active or not waiting or not active.job.get("watch_broadcast"):
        _challengers.pop(uid, None)
        return
    job = active.job
    challenger = waiting[0]
    current = rows.get(job["creator"])
    lead = rows[challenger["id"]]["points"] - current["points"] if current else 0
    key = (job["id"], challenger["id"])
    confirmed = _challengers.get(uid) == key
    if lead >= config.WATCH_SWITCH_MARGIN:
        _challengers[uid] = key
    else:
        _challengers.pop(uid, None)
    reason = selection.rotation_reason(now - active.started_at, now - last_switch, lead, confirmed)
    if reason:
        job.update(watch_switch_to=challenger["id"], watch_switched_at=now)
        active.stop(reason)
        jobs.save(job)
        rows[challenger["id"]]["reason"] = f"Next after handoff · {reason}"
        _challengers.pop(uid, None)


def poll(todo):
    """Check enabled channels, rank known live streams and hand off a full slot when justified."""
    from . import live
    observed = list(_probes.map(_probe, todo))
    for c, (platform, info) in zip(todo, observed):
        _observation(c, platform, info)
    # Fetch the current settings after the network checks. A disabled or edited channel stays stopped.
    limit = recording_limit()
    full, busy = load.render_disk_full(), load.busy()
    day = time.strftime("%Y-%m-%d")
    if not full:
        health = "CPU or memory load is high." if busy else "Watchlist is checking channels."
        discord_clips.event("status", f"{day}:{health}", health)
    history = jobs.list_jobs()
    by_creator = {}
    for job in history:
        by_creator.setdefault(job["creator"], []).append(job)
    now = time.time()
    candidates, blocked = [], {}
    for c in todo:
        current = creators.get(c["id"])
        if not current or not current["auto_watch"] or current["channel"] != c["channel"]:
            continue
        if _can_record(current, by_creator.get(c["id"], ()), now):
            candidates.append(current)
        elif (current.get("watch_status") or {}).get("state") == "live":
            blocked[c["id"]] = {"reason": "Stopped manually or waiting after recording failures"}
    order, rows = selection.rank(candidates, by_creator, now)
    rows.update(blocked)
    own = {s.job["creator"]: s for s in live.active_mine()}
    waiting = [c for c in order if c["id"] not in own]
    switched = max((j for j in history if j.get("watch_switched_at")), key=lambda j: j["watch_switched_at"], default={})
    last_switch = switched.get("watch_switched_at", 0)
    if now - last_switch < config.WATCH_COOLDOWN_SEC:
        first = switched.get("watch_switch_to")
        if any(c["id"] == first for c in waiting):
            waiting.sort(key=lambda c: c["id"] != first)
    for c in order:
        row = rows[c["id"]]
        row["reason"] = ("Recording · retaining the current slot" if c["id"] in own
                         else "Waiting for disk space" if full else "Waiting for CPU or memory" if busy
                         else "Waiting for the next turn · long wait" if row["waiting_sec"] >= config.WATCH_FAIR_WAIT_SEC
                         else "Waiting for a recording slot")
    if full or busy or limit == 0:
        _challengers.pop(ctx.uid(), None)
        _show(rows)
        return
    if live.recording_count() >= limit:
        if limit == 1 and len(own) == 1:
            _rotate(next(iter(own.values())), waiting, rows, last_switch, now)
        _show(rows)
        return
    _challengers.pop(ctx.uid(), None)
    for c in waiting:
        if live.recording_count() >= limit:
            break
        try:
            live.start_session(c, watch=c["watch_status"], watch_limit=limit)
            row = rows[c["id"]]
            row["reason"] = ("Selected for recording · longest wait" if row["waiting_sec"] >= config.WATCH_FAIR_WAIT_SEC
                             else "Selected for recording")
        except ValueError as exc:
            message = str(exc)
            rows[c["id"]]["reason"] = message
            creators.log(c["id"], message)
            discord_clips.event("status", f"{day}:{c['id']}:start:{message}", f"{c['name']}. {message}")
    _show(rows)


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
