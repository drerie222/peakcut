"""Admission and retry policy shared by text and image requests."""
import collections
import contextvars
import datetime as dt
import hashlib
import json
import random
import re
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import config

_reservation = contextvars.ContextVar("ai_request_reservation", default=None)


@contextmanager
def request_budget(reserve):
    token = _reservation.set(reserve)
    try:
        yield
    finally:
        _reservation.reset(token)


class Deferred(RuntimeError):
    def __init__(self, reason, retry_at):
        self.reason, self.retry_at = reason, retry_at
        super().__init__(f"AI deferred ({reason}).")


def _reset(now):
    try:
        tz = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        tz = dt.timezone(dt.timedelta(hours=-8))
    tomorrow = dt.datetime.fromtimestamp(now, tz).date() + dt.timedelta(days=1)
    return dt.datetime.combine(tomorrow, dt.time.min, tz).timestamp()


def classify(response, now):
    """Only parse known quota fields. Error payloads can contain secrets or input text."""
    delay = 0.
    try:
        hint = response.headers.get("Retry-After", "")
        delay = float(hint) if hint.replace(".", "", 1).isdigit() else parsedate_to_datetime(hint).timestamp() - now
    except (TypeError, ValueError, OverflowError):
        pass
    reason = "provider_rate"
    try:
        error = response.json().get("error", {})
        details = error.get("details", []) if isinstance(error, dict) else []
        code = error.get("code") if isinstance(error, dict) else None
        message = str(error.get("message", "")) if isinstance(error, dict) else ""
        daily_hint = re.search(r"quota exceeded for metric:\s*[\w./]*perday|limit:\s*0\b", message, re.I)
        if code == "insufficient_quota" or daily_hint:
            reason = "daily_quota"
        for item in details:
            kind = str(item.get("@type", ""))
            if kind.endswith("RetryInfo"):
                hint = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(item.get("retryDelay", "")))
                if hint:
                    delay = max(delay, float(hint[1]))
            if not kind.endswith("QuotaFailure"):
                continue
            for rule in item.get("violations", []):
                quota = str(rule.get("quotaId", "")) + str(rule.get("quotaMetric", ""))
                quota = quota.lower().replace("_", "")
                if "perday" in quota or str(rule.get("quotaValue")) == "0":
                    reason = "daily_quota"
                elif "token" in quota and reason != "daily_quota":
                    reason = "provider_tokens"
    except (AttributeError, ValueError, TypeError):
        pass
    return reason, max(0., delay)


class Limiter:
    def __init__(self):
        self.cv = threading.Condition()
        self.waiting = collections.deque()
        self.active = False
        self.recent = collections.defaultdict(collections.deque)
        self.blocks = {}
        self.daily = {}
        self.failures = collections.Counter()
        self.counts = collections.Counter()
        self.latest = {}
        self.loaded = False

    def _load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            data = json.loads((config.DATA_DIR / "ai-limits.json").read_text())
            self.daily.update(data.get("daily", {}))
            self.blocks.update({key: ("daily_quota", value) for key, value in data.get("blocked", {}).items()})
        except (OSError, ValueError, TypeError):
            pass

    def _save(self, now):
        path = config.DATA_DIR / "ai-limits.json"
        try:
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"daily": {k: v for k, v in self.daily.items() if v[0] > now},
                                        "blocked": {k: v[1] for k, v in self.blocks.items()
                                                    if v[0] == "daily_quota" and v[1] > now}}))
            temp.replace(path)
        except OSError:
            pass

    @staticmethod
    def scope(provider, address, key, model):
        return hashlib.sha256("\0".join((provider, address, key, model)).encode()).hexdigest()

    def _admit(self, provider, scope, estimated, started, deadline):
        with self.cv:
            self._load()
            if len(self.waiting) >= config.AI_MAX_WAITERS:
                raise Deferred("queue_full", time.time() + 15)
            ticket = object()
            self.waiting.append(ticket)
            try:
                while True:
                    now = time.time()
                    reason, until = self.blocks.get(scope, ("", 0))
                    if until > now:
                        raise Deferred(reason, until)
                    reset, count = self.daily.get(scope, (0, 0))
                    if now >= reset:
                        reset, count = _reset(now), 0
                    if config.AI_RPD and count >= config.AI_RPD:
                        raise Deferred("local_daily_budget", reset)
                    recent = self.recent[scope]
                    while recent and recent[0][0] <= now - 60:
                        recent.popleft()
                    rpm = config.AI_RPM if provider == "gemini" or config.AI_LIMIT_ALL else 0
                    tpm = config.AI_TPM
                    if tpm and estimated > tpm:
                        raise Deferred("request_exceeds_token_budget", now + 60)
                    if (rpm and len(recent) >= rpm) or (tpm and sum(t for _, t in recent) + estimated > tpm):
                        raise Deferred("local_rate", recent[0][0] + 60)
                    if self.waiting[0] is ticket and not self.active:
                        reserve = _reservation.get()
                        if reserve is not None:
                            reserve(now)
                        self.active = True
                        self.daily[scope] = (reset, count + 1)
                        recent.append((now, estimated))
                        self.counts["requests"] += 1
                        self.counts["queue_wait_ms"] += round((time.monotonic() - started) * 1000)
                        if config.AI_RPD:
                            self._save(now)
                        return
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise Deferred("queue_wait", now + 10)
                    self.cv.wait(left)
            finally:
                self.waiting.remove(ticket)
                self.cv.notify_all()

    def request(self, provider, address, key, model, estimated, send):
        scope = self.scope(provider, address, key, model)
        start = time.monotonic()
        for attempt in range(2):
            self._admit(provider, scope, estimated, start, start + 4)
            try:
                possibly_sent = False
                try:
                    response = send()
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                    response = None
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                    possibly_sent = True
                    response = None
                now = time.time()
                with self.cv:
                    self.latest = {"model": model, "provider": provider}
                    status = response.status_code if response is not None else 503
                    if status != 429 and status < 500:
                        self.failures.pop(scope, None)
                        self.counts["successes" if status == 200 else "client_errors"] += 1
                        self.counts["estimated_input_tokens"] += estimated
                        self.latest["reason"] = "" if status == 200 else "provider_request_error"
                        return response
                    self.counts["rate_errors" if status == 429 else "transient_errors"] += 1
                    self.failures[scope] += 1
                    reason, delay = classify(response, now) if response is not None else ("transient", 15 if possibly_sent else 0)
                    if status != 429:
                        reason = "transient"
                    backoff = min(120., 2 ** min(7, self.failures[scope])) + random.uniform(0, 1)
                    until = (max(_reset(now), now + delay) if reason == "daily_quota" else now + max(delay, backoff))
                    self.blocks[scope] = (reason, until)
                    self.latest.update(reason=reason, retry_at=until)
                    if reason == "daily_quota":
                        self._save(now)
            finally:
                with self.cv:
                    self.active = False
                    self.cv.notify_all()
            if attempt or until - time.time() > 3 or time.monotonic() - start > 4:
                raise Deferred(reason, until)
            time.sleep(max(0, until - time.time()))

    def status(self):
        with self.cv:
            self._load()
            now = time.time()
            blocks = [(reason, deadline) for reason, deadline in self.blocks.values() if deadline > now]
            data = {**self.counts, **self.latest, "waiting": len(self.waiting), "blocked_models": len(blocks)}
            if blocks and not data.get("reason"):
                data["reason"], data["retry_at"] = min(blocks, key=lambda block: block[1])
            return data


limiter = Limiter()
