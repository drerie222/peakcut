"""A small durable queue of gameplay moments awaiting visual review or rendering."""
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import ai_limits, brain, config, ctx, facecam, jobs, load

_lock = threading.RLock()
_running = set()
_SEGMENT = re.compile(r"p\d+_\d+\.ts")
_PENDING = {"waiting", "accepted", "failed"}
_MAX_HISTORY = 40


def _folder(job, entry):
    return Path(job["dir"]) / "candidates" / entry["id"]


def _segments(job, entry):
    folder = _folder(job, entry)
    return [(folder / name, a, b) for name, a, b in entry["segments"] if _SEGMENT.fullmatch(name)]


def _finish(job, entry, state, reason):
    entry.update(state=state, reason=reason)
    jobs.save(job)
    if state not in _PENDING:
        shutil.rmtree(_folder(job, entry), ignore_errors=True)


def enqueue(job, settings, segments, start, end, peak, signal):
    """Keep hard links before the recorder advances its rolling buffer."""
    with _lock:
        history = job.setdefault("candidates", [])
        if brain._overlaps(start, end, job["clips"] + [c for c in history if c["state"] in _PENDING | {"rendered"}]):
            return False
        if (load.disk_full() or sum(c["state"] in _PENDING for j in jobs.all_jobs() for c in j.get("candidates", []))
                >= config.LIVE_CANDIDATES):
            stats = job.setdefault("pipeline", {})
            stats["candidate_overload"] = stats.get("candidate_overload", 0) + 1
            return False
        bounds = [(p, a, b) for p, a, b in segments if b > start - 8 and a < end + 8
                  and _SEGMENT.fullmatch(p.name)]
        if (not bounds or bounds[0][1] > start + 2 or bounds[-1][2] < end - 1
                or any(nxt[1] - prev[2] > 1 for prev, nxt in zip(bounds, bounds[1:]))):
            stats = job.setdefault("pipeline", {})
            stats["candidate_missing_media"] = stats.get("candidate_missing_media", 0) + 1
            return False
        now = time.time()
        entry = dict(id=uuid.uuid4().hex[:16], start=start, end=end, peak=peak, signal=signal,
                     created=now, due=now, state="waiting", attempts=0, min_score=settings["min_score"],
                     min_gap=settings["min_gap_min"] * 60, per_hour=settings["live_per_hour"],
                     layout=settings["layout"], style=settings["style"], segments=[])
        folder = _folder(job, entry)
        try:
            folder.mkdir(parents=True)
            for path, a, b in bounds:
                os.link(path, folder / path.name)
                entry["segments"].append((path.name, a, b))
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            stats = job.setdefault("pipeline", {})
            stats["candidate_missing_media"] = stats.get("candidate_missing_media", 0) + 1
            return False
        history.append(entry)
        finished = [c for c in history if c["state"] not in _PENDING]
        for old in finished[:-_MAX_HISTORY]:
            history.remove(old)
        jobs.save(job)
        return True


def _render(job, entry, clip):
    from . import creators, live
    try:
        session = live.Session(job, creators.get(job["creator"]) or creators.DEFAULTS)
        session.ended = True
        session.segments = _segments(job, entry)
        if clip.get("src") and Path(clip["src"]).is_file():
            jobs.render_one(job, clip, [])
        else:
            session._render(clip)
    except Exception:
        clip.update(status="error", error="The saved highlight could not be rendered.")
    finally:
        with _lock:
            _running.discard(entry["id"])
            done = clip.get("status") == "done"
            _finish(job, entry, "rendered" if done else "failed", "" if done else "Rendering failed. Retry this highlight.")


def _dispatch(job, entry, clip):
    if entry["id"] not in _running:
        _running.add(entry["id"])
        if not jobs.try_submit_live(lambda: _render(job, entry, clip)):
            _running.discard(entry["id"])


def _review(job, entry):
    start, end, peak = (entry[k] for k in ("start", "end", "peak"))
    wall = time.monotonic()
    clip_id = "moment-" + entry["id"]
    try:
        jpeg = facecam.frame_strip(_segments(job, entry), (max(start, peak - 8), peak, min(end - .25, peak + 8)))
        if not jpeg:
            with _lock:
                _finish(job, entry, "failed", "The saved frames could not be read.")
            return
        clip, _ = brain.judge_gameplay(jpeg, start, end, job["title"], job.get("game"), config.LIVE_AI_TIMEOUT)
    except ai_limits.Deferred as exc:
        with _lock:
            entry.update(due=max(time.time() + 5, exc.retry_at), reason=exc.reason)
            jobs.save(job)
        return
    except Exception:
        with _lock:
            entry["attempts"] += 1
            entry.update(due=time.time() + 60 * entry["attempts"], reason="The visual reviewer did not answer.")
            if entry["attempts"] >= 3:
                entry["state"] = "failed"
            jobs.save(job)
        return
    with _lock:
        stats = job.setdefault("pipeline", {})
        stats["visual_checks"] = stats.get("visual_checks", 0) + 1
        stats["visual_seconds"] = round(stats.get("visual_seconds", 0) + time.monotonic() - wall, 2)
        if not clip or clip["score"] < entry["min_score"]:
            _finish(job, entry, "rejected", "The sampled frames did not support a strong setup and payoff.")
            return
        recent = [c for c in job["clips"] if c.get("score", 0) > 0 and c.get("made_at", 0) >= time.time() - 3600]
        if (len(recent) >= entry["per_hour"] or brain._overlaps(start, end, job["clips"])
                or any(abs(start - c["start"]) < entry["min_gap"] for c in job["clips"])):
            _finish(job, entry, "rejected", "An existing clip overlaps this moment or the clip frequency limit was reached.")
            return
        clip.update(id=clip_id, candidate=entry["id"], status="queued", version=0,
                    evidence="visual", peak_sec=peak, reason=clip.get("reason") or "Visible action",
                    quality_min_score=entry["min_score"], style=entry["style"], layout=entry["layout"],
                    captions=job["options"].get("captions", True), made_at=time.time())
        job["clips"].append(clip)
        _finish(job, entry, "accepted", "")
        _dispatch(job, entry, clip)


def run_round():
    now = time.time()
    due = []
    with _lock:
        for job in jobs.all_jobs():
            with ctx.as_user(job["user"]):
                for entry in job.get("candidates", []):
                    if entry["id"] in _running or entry["state"] not in _PENDING:
                        continue
                    if now - entry["created"] >= config.LIVE_CANDIDATE_HOURS * 3600:
                        clip = next((c for c in job["clips"] if c.get("candidate") == entry["id"]), None)
                        if clip and clip.get("status") in ("queued", "rendering"):
                            clip.update(status="error", error="The retained highlight expired before rendering.")
                        _finish(job, entry, "expired", "The temporary highlight footage reached its retention limit.")
                    elif entry["state"] == "accepted":
                        clip = next((c for c in job["clips"] if c.get("candidate") == entry["id"]), None)
                        if clip and clip.get("status") == "done":
                            _finish(job, entry, "rendered", "")
                        elif clip and jobs.get(job["id"]):
                            _dispatch(job, entry, clip)
                    elif entry["state"] == "waiting" and entry["due"] <= now:
                        due.append((entry["due"], job, entry))
    if config.ai_ready() and due and not load.render_disk_full():
        _, job, entry = min(due, key=lambda item: item[0])
        with ctx.as_user(job["user"]):
            _review(job, entry)


def retry(job, candidate_id):
    with _lock:
        entry = next((c for c in job.get("candidates", []) if c["id"] == candidate_id), None)
        if not entry or entry["state"] != "failed":
            return False
        if not _folder(job, entry).is_dir() or time.time() - entry["created"] >= config.LIVE_CANDIDATE_HOURS * 3600:
            return False
        clip = next((c for c in job["clips"] if c.get("candidate") == candidate_id), None)
        if clip:
            clip.update(status="queued", error=None)
        entry.update(state="accepted" if clip else "waiting", due=time.time(), attempts=0, reason="")
        jobs.save(job)
        return True


def start():
    import logging

    def worker():
        # A restart can happen between creating the hard links and saving the queue entry.
        for job in jobs.all_jobs():
            retained = {c["id"] for c in job.get("candidates", []) if c["state"] in _PENDING}
            for folder in (Path(job["dir"]) / "candidates").glob("*"):
                if re.fullmatch(r"[a-f0-9]{16}", folder.name) and folder.name not in retained and not folder.is_symlink():
                    shutil.rmtree(folder, ignore_errors=True)
        while True:
            try:
                run_round()
            except Exception:
                logging.getLogger(__name__).warning("Could not process the saved highlight queue.")
            time.sleep(5)

    threading.Thread(target=worker, daemon=True, name="live-highlight-queue").start()
