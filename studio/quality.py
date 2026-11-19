"""Ad breaks and static screens.

Twitch and YouTube stitch commercials into a stream, and a "be right back" slate
can sit where the good moment is. A clip taken from there is worthless, so those
seconds are found and left out.

Metric: mean absolute luma difference between consecutive frames
(tblend=difference + signalstats). Measured on real captures:

    ad slate         ~0.19
    gameplay clip    ~1.6 - 2.5

A flat 0-255 luma difference separates them with a wide margin, so no OCR, no
reference image and no model is needed. The frames are read once for the whole
video, and clips are then checked against that map.
"""

from __future__ import annotations

import re
import math
import subprocess
from pathlib import Path

import numpy as np

from . import config, load, media
from .render import FULL_GAMEPLAY

_FFMPEG = "ffmpeg"

_PTS = re.compile(r"pts_time:([0-9.]+)")
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")
REVIEW_REVISION = 1
_SAMPLE_FPS, _WIDTH, _HEIGHT = 8, 64, 64


@load.serial_work
def samples(path, start, seconds, *, region=None, mask=None, native=False):
    """Decode a bounded window. A six-second check produces less than 200 KiB."""
    filters = []
    if region:
        x, y, w, h = region
        filters.append(f"crop={w}:{h}:{x}:{y}")
    if mask:
        x, y, w, h = mask
        filters.append(f"drawbox=x=iw*{x:.6f}:y=ih*{y:.6f}:w=iw*{w:.6f}:h=ih*{h:.6f}:color=black:t=fill")
    if not native:
        filters.append(f"fps={_SAMPLE_FPS}")
    filters.extend((f"scale={_WIDTH}:{_HEIGHT}:flags=area", "format=gray"))
    cap = ["-frames:v", "3"] if native else []
    try:
        data = subprocess.run(
            [_FFMPEG, "-v", "error", "-ss", f"{max(0, start):.3f}", *load.ffmpeg_threads(), "-i", str(path),
             "-t", f"{seconds:.3f}", "-filter_threads", "1", "-map", "0:v:0", "-vf",
             ",".join(filters), *cap, "-an", "-c:v", "rawvideo",
             "-threads", "1", "-f", "rawvideo", "-"],
            capture_output=True, timeout=20, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return np.empty((0, _WIDTH * _HEIGHT), dtype=np.uint8)
    size = _WIDTH * _HEIGHT
    return np.frombuffer(data[:len(data) // size * size], dtype=np.uint8).reshape(-1, size)


def _visible(frames):
    # Reject nearly black frames while allowing dark scenes with visible foreground detail.
    return np.count_nonzero(frames > 26, axis=1) >= frames.shape[1] * 0.08


def opening(path, start, duration, *, strict=False, mask=None):
    """Seconds to the first sustained visible frame, or None when the opening cannot be cleared."""
    frames = samples(path, start, min(6.0, duration), **({"mask": mask} if mask else {}))
    visible = _visible(frames)
    for index in range(min(len(visible) - 2, 4 * _SAMPLE_FPS + 1)):
        if visible[index:index + 3].all():
            if index == 0 and strict and not _clear_opening(samples(path, start, min(3.0, duration), mask=mask, native=True)):
                continue
            return (index + 1) / _SAMPLE_FPS if index else 0.0
    return None


def _moving(frames):
    if len(frames) < 3:
        return False
    differences = np.abs(np.diff(frames.astype(np.int16), axis=0))
    return bool((np.count_nonzero(differences > 10, axis=1) >= frames.shape[1] * 0.01).sum() >= 2)


def _clear_opening(frames):
    return len(frames) >= 3 and _visible(frames[:3]).all()


def review(path, clip, version, *, gameplay=False, source_issue=None):
    """Preflight the finished asset. Results only apply to the version that was decoded."""
    issues = [source_issue] if source_issue else []
    try:
        expected = float(clip["end"]) - float(clip["start"])
        info = media.probe(path)
    except Exception:  # invalid or missing media must never create a draft
        issues.append("The video or audio track could not be read.")
    else:
        duration = info["duration"]
        if (info["width"], info["height"]) != (config.OUT_W, config.OUT_H):
            issues.append("The video is not 1080 × 1920.")
        if (not math.isfinite(duration) or not math.isfinite(expected) or duration < 2.8
                or abs(duration - expected) > max(0.65, min(1.5, expected * 0.04))):
            issues.append("The rendered duration does not match the selected cut.")
            return {"version": version, "revision": REVIEW_REVISION, "issues": issues}
        opts = {}
        if clip.get("layout") == FULL_GAMEPLAY:
            region = clip.get("gameplay_region")
            if (not isinstance(region, (list, tuple)) or len(region) != 4
                    or not all(type(value) is int and value >= 0 for value in region)
                    or not (0 < region[2] <= config.OUT_W - region[0] and 0 < region[3] <= config.OUT_H - region[1])):
                issues.append("The rendered gameplay area could not be checked.")
                return {"version": version, "revision": REVIEW_REVISION, "issues": issues}
            opts = {"region": region, "mask": clip.get("gameplay_mask")}
        first = samples(path, 0, min(3.0 if opts else 1.0, duration), **({**opts, "native": True} if opts else {}))
        if not _clear_opening(first):
            issues.append("The opening frames are black or unreadable.")
        if gameplay and duration >= 3:
            middle = samples(path, duration * 0.5, min(1.5, duration * 0.4), **opts)
            if not _visible(middle).any():
                issues.append("Gameplay is black or unreadable midway through the clip.")
    return {"version": version, "revision": REVIEW_REVISION, "issues": issues}


def gameplay_issue(path, clip, offset):
    if clip.get("evidence") == "audio_only":
        return "Only a sound spike was detected. This highlight needs visual confirmation."
    if clip.get("score", 0) and clip.get("score", 0) < clip.get("quality_min_score", 65):
        return "The highlight score is below the review threshold."
    if (clip.get("score", 0) and clip.get("evidence") not in ("visual", "isolated_mic")
            and clip.get("speech_source") != "isolated_mic"):
        return "The highlight needs visual confirmation or verified streamer speech."
    span = clip["end"] - clip["start"]
    start = clip["start"] - offset
    if span >= 3 and not any(_moving(samples(path, start + span * fraction, min(1.5, span / 3)))
                             for fraction in (0.0, 0.45, 0.8)):
        return "The selected gameplay appears frozen."
    return None


@load.serial_work
def motion_map(path: str | Path, bucket: float = 2.0) -> list[tuple[float, float]]:
    """(time, motion) for every bucket of the video, or nothing if it cannot be read.

    Motion is the mean luma difference between consecutive frames: near zero for a
    frozen screen, high for anything actually moving."""
    try:
        proc = subprocess.run(
            [_FFMPEG, "-hide_banner", "-loglevel", "error", *load.ffmpeg_threads(),
             "-i", str(path),
             "-vf", "tblend=all_mode=difference,signalstats,metadata=print:file=-",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=900,
        )
    except Exception:  # noqa: BLE001 - a missing ffmpeg must not stop a job
        return []
    times: list[float] = []
    vals: list[float] = []
    at = 0.0
    for line in (proc.stdout or "").splitlines():
        m = _PTS.search(line)
        if m:
            at = float(m.group(1))
            continue
        m = _YAVG.search(line)
        if m:
            times.append(at)
            vals.append(float(m.group(1)))
    if len(vals) > 1:
        times, vals = times[1:], vals[1:]      # the first blended frame is against black
    if not vals:
        return []
    buckets: dict[int, list[float]] = {}
    for t, v in zip(times, vals):
        buckets.setdefault(int(t / bucket), []).append(v)
    return [(i * bucket, sum(v) / len(v)) for i, v in sorted(buckets.items())]


def static_ranges(path: str | Path, bucket: float = 2.0, threshold: float | None = None,
                  min_seconds: float | None = None) -> list[tuple[float, float]]:
    """The stretches that look like an ad break, a slate or a frozen screen."""
    threshold = config.STATIC_THRESHOLD if threshold is None else threshold
    min_seconds = config.STATIC_MIN_SECONDS if min_seconds is None else min_seconds
    pairs = motion_map(path, bucket)
    if not pairs:
        return []
    runs: list[tuple[float, float]] = []
    start = None
    for t, v in pairs:
        if v < threshold:
            if start is None:
                start = t
        elif start is not None:
            runs.append((start, t))
            start = None
    if start is not None:
        runs.append((start, pairs[-1][0] + bucket))
    return [(a, b) for a, b in runs if b - a >= min_seconds]


def static_overlap(start: float, end: float, ranges: list[tuple[float, float]]) -> float:
    """How much of a clip (in seconds) sits inside a static stretch."""
    total = 0.0
    for a, b in ranges:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            total += hi - lo
    return total
