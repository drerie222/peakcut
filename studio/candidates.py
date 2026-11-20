"""A durable queue of stream moments awaiting review or rendering."""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import ai_limits, brain, config, ctx, facecam, jobs, load, media, transcribe, usage

_lock = threading.RLock()
_budget_lock = threading.Lock()
_running = set()
_SEGMENT = re.compile(r"p\d+_\d+\.ts")
_PENDING = {"waiting", "accepted", "failed"}
_MAX_HISTORY = 40
_MAX_QUIET_PENDING = 3


def _broadcast_jobs(job):
    broadcast = job.get("watch_broadcast")
    if broadcast:
        return [job] + [other for other in jobs.all_jobs() if other["id"] != job["id"]
                        and other.get("watch_broadcast") == broadcast and other["user"] == job["user"]
                        and other.get("creator") == job.get("creator")]
    return [job]


def _check_budget(job, now, *, reserve=False):
    with _budget_lock:
        recent = sorted(at for record in _broadcast_jobs(job) for at in record.get("live_ai_requests", [])
                        if at > now - 3600)
        limit = config.LIVE_HIGHLIGHT_REQUESTS
        if not limit:
            raise ai_limits.Deferred("stream_budget_disabled", now + 3600)
        if len(recent) >= limit:
            raise ai_limits.Deferred("stream_hourly_budget", recent[-limit] + 3600)
        if reserve:
            job["live_ai_requests"] = [at for at in job.get("live_ai_requests", []) if at > now - 3600] + [now]
            stats = job.setdefault("pipeline", {})
            stats["live_ai_requests"] = stats.get("live_ai_requests", 0) + 1
            jobs.save(job)


def request_budget(job, on_request=None):
    def reserve(now):
        _check_budget(job, now, reserve=True)
        if on_request:
            on_request()

    return ai_limits.request_budget(reserve)


def _folder(job, entry):
    return Path(job["dir"]) / "candidates" / entry["id"]


def _segments(job, entry):
    folder = _folder(job, entry)
    return [(folder / name, a, b) for name, a, b in entry["segments"] if _SEGMENT.fullmatch(name)]


def _finish(job, entry, state, reason):
    entry.update(state=state, reason=reason)
    if state == "rejected":
        stats = job.setdefault("pipeline", {})
        stats["candidate_rejected"] = stats.get("candidate_rejected", 0) + 1
    jobs.save(job)
    if state not in _PENDING:
        shutil.rmtree(_folder(job, entry), ignore_errors=True)


def enqueue(job, settings, segments, start, end, peak, signal, *, context=None):
    """Keep hard links before the recorder advances its rolling buffer."""
    with _lock:
        history = job.setdefault("candidates", [])
        if brain._overlaps(start, end, job["clips"] + [c for c in history if c["state"] != "expired"]):
            return False
        quiet = context is not None and context["quiet"]
        pending = sum(c["state"] in _PENDING for j in jobs.all_jobs() for c in j.get("candidates", []))
        quiet_pending = sum(c["state"] in {"waiting", "accepted"} and c.get("context", {}).get("quiet", False) for c in history)
        if (load.disk_full() or pending >= config.LIVE_CANDIDATES
                or (quiet and (quiet_pending >= _MAX_QUIET_PENDING
                               or pending >= max(1, config.LIVE_CANDIDATES - 1)))):
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
        if context is not None:
            entry.update(context=context, analysis="not_called", min_len=settings["min_len"], max_len=settings["max_len"])
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
        stats = job.setdefault("pipeline", {})
        stats["candidate_count"] = stats.get("candidate_count", len(history) - 1) + 1
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
            held = clip.get("status") == "held"
            state = "held" if held else "rendered" if done else "failed"
            reason = clip.get("error") if held else entry.get("reason", "") if done else "Rendering failed. Retry this highlight."
            _finish(job, entry, state, reason)


def _dispatch(job, entry, clip):
    if entry["id"] not in _running:
        _running.add(entry["id"])
        if not jobs.try_submit_live(lambda: _render(job, entry, clip)):
            _running.discard(entry["id"])


def _speech_context(job, entry, segments):
    seconds = min(config.LIVE_HIGHLIGHT_SPEECH, entry["end"] - entry["start"])
    if not seconds:
        return "", "disabled"
    folder = _folder(job, entry)
    manifest, wav = folder / "review.ffconcat", folder / "review.wav"
    base = segments[0][1]
    at = max(entry["start"], min(entry["end"] - seconds, entry["peak"] - seconds * 0.55))
    started = time.monotonic()
    try:
        # Only the validated segment basenames enter the manifest. Decode the programme track for analysis only.
        manifest.write_text("".join(f"file '{p.name}'\nduration {b - a:.6f}\n" for p, a, b in segments), encoding="utf-8")
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "1", *load.ffmpeg_threads(),
                   "-i", str(manifest), "-ss", f"{max(0, at - base):.3f}", "-t", f"{seconds:.3f}",
                   "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], timeout=30)
        words = transcribe.transcribe(media.load_audio(wav))
        offset = at - entry["start"]
        speech = " | ".join(f"[{line['s'] + offset:.1f}s] {line['text']}" for line in brain.make_lines(words))
        if len(speech) > 1600:
            speech = speech[:750] + " … " + speech[-825:]
        return speech, "available" if speech else "no_speech"
    except Exception:
        return "", "failed"
    finally:
        jobs.add_timing(job, "highlight_speech", time.monotonic() - started)
        manifest.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)


@load.serial_work
def _prepare(job, entry):
    folder = _folder(job, entry)
    sheet, metadata = folder / "review.jpg", folder / "review.json"
    try:
        return sheet.read_bytes(), json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    start, end, peak = (entry[k] for k in ("start", "end", "peak"))
    if entry["context"]["quiet"]:
        times = [start + (end - start) * fraction for fraction in (.05, .25, .5, .75, .95)]
    else:
        times = [start + .5, max(start + .5, peak - 8), peak, min(end - .5, peak + 8), end - .5]
    segments = _segments(job, entry)
    started = time.monotonic()
    try:
        jpeg, frames = facecam.frame_strip(segments, times, with_times=True)
    finally:
        jobs.add_timing(job, "highlight_frames", time.monotonic() - started)
    if not jpeg:
        return b"", None
    speech, speech_state = _speech_context(job, entry, segments)
    # Drop timing labels before hashing. A repeated still with different spoken words must be reviewed.
    digest = hashlib.sha256(jpeg + re.sub(r"\[\d+(?:\.\d+)?s\]", "", speech).casefold().encode()).hexdigest()
    prepared = dict(frames=frames, speech=speech, speech_state=speech_state, digest=digest)
    temp = metadata.with_suffix(".tmp")
    sheet.write_bytes(jpeg)
    temp.write_text(json.dumps(prepared), encoding="utf-8")
    temp.replace(metadata)
    return jpeg, prepared


def _review(job, entry):
    start, end, peak = (entry[k] for k in ("start", "end", "peak"))
    clip_id = "moment-" + entry["id"]
    multimodal = "context" in entry
    sent = 0
    wall = None

    def admitted():
        nonlocal sent
        sent += 1
        entry["analysis"] = "analyzing"
        ctx.bump(job["user"])

    try:
        _check_budget(job, time.time())
        usage.set_context(job.get("creator"), job["title"] or "Live stream")
        if multimodal:
            jpeg, prepared = _prepare(job, entry)
        else:
            jpeg = facecam.frame_strip(_segments(job, entry), (max(start, peak - 8), peak, min(end - .25, peak + 8)))
        if not jpeg:
            with _lock:
                entry["analysis"] = "image_failed"
                _finish(job, entry, "failed", "AI not called: the saved frames could not be read.")
            return
        if multimodal:
            entry.update(speech_state=prepared["speech_state"], digest=prepared["digest"])
            if any(other is not entry and other.get("digest") == entry["digest"] and other.get("reviewed")
                   for record in _broadcast_jobs(job) for other in record.get("candidates", [])):
                with _lock:
                    entry["analysis"] = "not_called"
                    _finish(job, entry, "rejected", "AI not called: this footage and speech were already reviewed.")
                return
        wall = time.monotonic()
        with request_budget(job, admitted):
            if multimodal:
                clip, reason = brain.judge_entertainment(
                    jpeg, start, end, job["title"], job.get("game"), frames=prepared["frames"], speech=prepared["speech"],
                    signals=entry["context"]["signals"], min_len=entry["min_len"], max_len=entry["max_len"],
                    timeout=config.LIVE_AI_TIMEOUT)
            else:
                clip, _ = brain.judge_gameplay(jpeg, start, end, job["title"], job.get("game"), config.LIVE_AI_TIMEOUT)
                reason = "The sampled frames did not support a strong setup and payoff."
    except ai_limits.Deferred as exc:
        with _lock:
            entry.update(due=max(time.time() + 5, exc.retry_at), reason=exc.reason,
                         analysis="api_failed" if sent else "waiting", wait_reason=exc.reason)
            if sent:
                _failed_request(job, entry)
            jobs.save(job)
        return
    except Exception:
        with _lock:
            if wall is None:
                entry["analysis"] = "image_failed"
                _finish(job, entry, "failed", "AI not called: the saved footage could not be prepared.")
            else:
                _failed_request(job, entry)
                entry.update(due=time.time() + 60 * entry["attempts"], analysis="api_failed", wait_reason="",
                             reason="The API did not return a usable highlight decision.")
                jobs.save(job)
        return
    finally:
        if wall is not None:
            jobs.add_timing(job, "highlight_ai", time.monotonic() - wall)
            jobs.save(job)
    with _lock:
        stats = job.setdefault("pipeline", {})
        stats["visual_checks"] = stats.get("visual_checks", 0) + 1
        entry.update(reviewed=True, wait_reason="")
        if not clip or clip["score"] < entry["min_score"]:
            entry["analysis"] = "no_highlight"
            _finish(job, entry, "rejected", reason if not clip else
                    f"Score {clip['score']} below the required {entry['min_score']}. {reason}")
            return
        entry["analysis"] = "verified"
        start, end = clip["start"], clip["end"]
        recent = [c for c in job["clips"] if c.get("score", 0) > 0 and c.get("made_at", 0) >= time.time() - 3600
                  and c.get("status") not in {"held", "error"}]
        if (len(recent) >= entry["per_hour"] or brain._overlaps(start, end, job["clips"])
                or any(abs(start - c["start"]) < entry["min_gap"] for c in job["clips"])):
            _finish(job, entry, "rejected", "An existing clip overlaps this moment or the clip frequency limit was reached.")
            return
        clip.update(id=clip_id, candidate=entry["id"], status="queued", version=0,
                    evidence=clip.get("evidence", "visual"), peak_sec=clip.get("peak_sec", peak),
                    reason=clip.get("reason") or "Visible action",
                    quality_min_score=entry["min_score"], style=entry["style"], layout=entry["layout"],
                    captions=job["options"].get("captions", True), made_at=time.time())
        if multimodal:
            clip.update(gameplay_source=True, require_quality=True)
        job["clips"].append(clip)
        _finish(job, entry, "accepted", clip["reason"])
        _dispatch(job, entry, clip)


def _failed_request(job, entry):
    stats = job.setdefault("pipeline", {})
    stats["highlight_failures"] = stats.get("highlight_failures", 0) + 1
    entry["attempts"] += 1
    if entry["attempts"] >= 3:
        entry.update(state="failed", wait_reason="")


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
                            _finish(job, entry, "rendered", entry.get("reason", ""))
                        elif clip and clip.get("status") == "held":
                            _finish(job, entry, "held", clip.get("error", "Held by quality checks."))
                        elif clip and jobs.get(job["id"]):
                            _dispatch(job, entry, clip)
                    elif entry["state"] == "waiting" and entry["due"] <= now:
                        due.append((entry["due"], job, entry))
    if not due or load.render_disk_full():
        return
    _, job, entry = min(due, key=lambda item: item[0] + (60 if item[2].get("context", {}).get("quiet") else 0))
    with ctx.as_user(job["user"]):
        if config.ai_ready():
            _review(job, entry)
        else:
            with _lock:
                entry.update(due=now + 60, analysis="not_called", reason="No image-capable AI is configured.", wait_reason="")
                jobs.save(job)


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
        entry.update(state="accepted" if clip else "waiting", due=time.time(), attempts=0, reason="",
                     analysis="verified" if clip else "not_called", wait_reason="")
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
