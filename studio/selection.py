"""Rank observed live streams using platform metadata and the work this account has already done."""
from . import config, games

_PRIORITY = {"A": 30, "B": 15, "C": 0}
_GTA = "Grand Theft Auto VI"


def score(creator, history, now):
    status = creator["watch_status"]
    priority = creator["watchlist"]["priority"]
    parts = [(f"Priority {priority}", _PRIORITY[priority])]
    category = status.get("category")
    if games.name_of(category) == _GTA:
        parts.append(("GTA VI platform category", config.WATCH_GTA_CATEGORY_POINTS))
    elif not category and games.detect(status.get("title"))[0] == _GTA:
        parts.append(("GTA VI in title, unverified", config.WATCH_GTA_TITLE_POINTS))

    recent = [j for j in history if j["created"] >= now - 86400 and j.get("duration", 0) > 0]
    last_end = max((j["created"] + j["duration"] for j in recent), default=0)
    waiting = max(0, now - max(status["live_since"], last_end))
    minutes = sum(j["duration"] for j in recent) / 60
    made = sum(c.get("status") == "done" for j in recent for c in j["clips"])
    parts += [("Waiting for a recording", min(60, int(waiting / 60))),
              (f"{int(minutes)} minutes recorded in 24 hours", -min(24, int(minutes / 5))),
              (f"{made} finished clips in 24 hours", -min(12, made * 2))]
    return {"points": sum(value for _, value in parts), "factors": parts, "waiting_sec": waiting}


def rank(candidates, history, now):
    decisions = {c["id"]: score(c, history.get(c["id"], ()), now) for c in candidates}
    def rank_key(c):
        verdict = decisions[c["id"]]
        waiting = verdict["waiting_sec"]
        if waiting >= config.WATCH_FAIR_WAIT_SEC:
            return (0, -waiting, c["watchlist"]["order"])
        return (1, -verdict["points"], c["watchlist"]["order"])

    order = sorted(candidates, key=rank_key)
    return order, decisions


def rotation_reason(age, since_switch, lead, confirmed):
    if age < config.WATCH_MIN_SEC or since_switch < config.WATCH_COOLDOWN_SEC:
        return ""
    if age >= config.WATCH_MAX_SEC:
        return "Recording turn finished"
    if confirmed and lead >= config.WATCH_SWITCH_MARGIN:
        return "Another stream has a sustained selection lead"
    return ""
