"""Time tracking: reading and logging work, editing only the account's own entries."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any

from .http import AtlassianError
from .jira import MAX_RESULTS, JiraService, _person, _visibility

_PART = r"[0-9]+(?:\.[0-9]+)?[wdhm]"
_DURATION = re.compile(rf"{_PART}(?: +{_PART})*")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _duration(value: str, name: str, *, allow_zero: bool = False) -> str:
    """Validate a Jira duration. Units are required: a bare number depends on Jira settings."""
    text = value.strip().lower()
    if not _DURATION.fullmatch(text):
        raise AtlassianError(f"{name} {value!r} is not a duration such as '1h 30m', '45m' or '2d'")
    if not allow_zero and not any(float(n) for n in re.findall(r"[0-9.]+", text)):
        raise AtlassianError(f"{name} must be greater than zero")
    return text


def _started(value: str) -> str:
    """Turn a date or an ISO datetime into the only format Jira accepts."""
    text = value.strip()
    try:
        if _DATE.fullmatch(text):
            # Noon keeps the day unchanged when Jira shows it in another time zone.
            moment = datetime.combine(date.fromisoformat(text), time(12))
        elif "T" in text:
            moment = datetime.fromisoformat(text)
        else:
            raise ValueError
        if moment.tzinfo is None:
            moment = moment.astimezone()
    except (ValueError, OverflowError):
        raise AtlassianError(
            f"started {value!r} must be a date (2026-01-31) or a datetime (2026-01-31T09:00)"
        ) from None
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}{moment:%z}"


def _estimate(value: str) -> dict[str, str]:
    """Query parameters that say what happens to the remaining estimate."""
    mode = value.strip().lower()
    if mode in {"auto", "leave"}:
        return {"adjustEstimate": mode}
    return {
        "adjustEstimate": "new",
        "newEstimate": _duration(value, "remaining_estimate", allow_zero=True),
    }


def _same_user(author: dict, me: dict) -> bool:
    # The user key survives renames, so it wins over the name whenever both sides have it.
    for field in ("key", "name"):
        if author.get(field) and me.get(field):
            return author[field] == me[field]
    return False


class JiraWorklogs:
    def __init__(self, jira: JiraService) -> None:
        self.jira = jira
        self.client = jira.client

    def _worklog(self, worklog: dict) -> dict:
        return {
            "id": worklog.get("id"),
            "author": _person(worklog.get("author")),
            "started": worklog.get("started"),
            "time_spent": worklog.get("timeSpent"),
            "time_spent_seconds": worklog.get("timeSpentSeconds"),
            "comment": worklog.get("comment"),
            "visibility": worklog.get("visibility"),
            "created": worklog.get("created"),
            "updated": worklog.get("updated"),
        }

    async def get_worklogs(self, key: str, limit: int = 50, start: int = 0) -> dict:
        key = await self.jira._writable_key(key)
        data = await self.client.get_json(f"/rest/api/2/issue/{key}/worklog")
        worklogs = data.get("worklogs") or []
        start = max(0, start)
        page = worklogs[start : start + max(1, min(limit, MAX_RESULTS))]
        # Jira Server returns every entry at once. If it ever returns fewer than the
        # total, say so instead of presenting a partial list as complete.
        total = max(data.get("total") or 0, len(worklogs))
        result: dict[str, Any] = {
            "key": key,
            "total": total,
            "start": start,
            "worklogs": [self._worklog(w) for w in page],
        }
        if total > len(worklogs):
            result["warning"] = f"Jira returned only {len(worklogs)} of {total} entries"
        return result

    async def add_worklog(
        self,
        key: str,
        time_spent: str,
        comment: str | None = None,
        started: str | None = None,
        remaining_estimate: str = "auto",
        visibility_role: str | None = None,
        visibility_group: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"timeSpent": _duration(time_spent, "time_spent")}
        if comment:
            payload["comment"] = comment
        if started is not None:
            payload["started"] = _started(started)
        visibility = _visibility(visibility_role, visibility_group)
        if visibility:
            payload["visibility"] = visibility
        params = _estimate(remaining_estimate)
        key = await self.jira._writable_key(key)
        created = await self.client.send_json(
            "POST", f"/rest/api/2/issue/{key}/worklog", payload, params
        )
        return {"key": key, "worklog": self._worklog(created or {})}

    async def update_worklog(
        self,
        key: str,
        worklog_id: str,
        time_spent: str | None = None,
        comment: str | None = None,
        started: str | None = None,
        remaining_estimate: str = "auto",
    ) -> dict:
        worklog_id = str(worklog_id).strip()
        if not re.fullmatch(r"[0-9]+", worklog_id):
            raise AtlassianError("worklog_id must be a number from jira_get_worklogs")
        if time_spent is None and comment is None and started is None:
            raise AtlassianError("Nothing to change: give time_spent, comment or started")
        changes: dict[str, Any] = {}
        if time_spent is not None:
            changes["timeSpent"] = _duration(time_spent, "time_spent")
        if comment is not None:
            changes["comment"] = comment
        if started is not None:
            changes["started"] = _started(started)
        params = _estimate(remaining_estimate)

        key = await self.jira._writable_key(key)
        path = f"/rest/api/2/issue/{key}/worklog/{worklog_id}"
        current = await self.client.get_json(path)
        me = await self.client.get_json("/rest/api/2/myself")
        if not _same_user(current.get("author") or {}, me):
            raise AtlassianError(
                f"Worklog {worklog_id} belongs to another user. Only own entries can be edited."
            )
        # Send the existing values back, so that nothing is lost or becomes visible to more
        # people than before.
        payload = {
            field: current[field]
            for field in ("comment", "started", "timeSpentSeconds", "visibility")
            if current.get(field) is not None
        }
        if "timeSpent" in changes:
            payload.pop("timeSpentSeconds", None)
        payload.update(changes)
        updated = await self.client.send_json("PUT", path, payload, params)
        return {"key": key, "worklog": self._worklog(updated or {})}
