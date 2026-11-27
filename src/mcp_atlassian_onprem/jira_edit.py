"""Editing fields of existing issues. Editable fields are always read from Jira."""

from __future__ import annotations

from typing import Any

from .http import AtlassianError
from .jira import JiraService
from .jira_create import _describe, _empty

# Never editable here. The value says what to use instead, if anything.
_BLOCKED = {
    "reporter": "the reporter cannot be changed",
    "project": "issues cannot be moved between projects",
    "issuetype": "the issue type cannot be changed",
    "parent": "the parent cannot be changed",
    "security": "the security level controls who sees the issue",
    "assignee": "use jira_assign_issue",
    "comment": "use jira_add_comment",
    "issuelinks": "use jira_link_issues or jira_delete_link",
    "worklog": "work logs are not edited here",
    "attachment": "attachments cannot be changed",
}
_SPRINT = ":gh-sprint"
_SPRINT_HINT = "use jira_add_to_sprint or jira_move_to_backlog"


def _custom(meta: dict) -> str:
    return str((meta.get("schema") or {}).get("custom", ""))


def _blocked(field_id: str, meta: dict) -> str | None:
    if _custom(meta).endswith(_SPRINT):
        return _SPRINT_HINT
    return _BLOCKED.get(field_id)


class JiraEditor:
    def __init__(self, jira: JiraService) -> None:
        self.jira = jira
        self.client = jira.client

    async def _editmeta(self, key: str) -> dict[str, dict]:
        data = await self.client.get_json(f"/rest/api/2/issue/{key}/editmeta")
        return data.get("fields") or {}

    async def get_edit_fields(self, key: str) -> dict:
        key = await self.jira._writable_key(key)
        fields = await self._editmeta(key)
        return {
            "key": key,
            "fields": [
                _describe({**meta, "fieldId": field_id})
                for field_id, meta in fields.items()
                if not _blocked(field_id, meta)
            ],
        }

    async def update_issue(self, key: str, values: dict[str, Any]) -> dict:
        if not values:
            raise AtlassianError("Nothing to change: give at least one field")
        values = dict(values)
        blocked = [f"{name} ({_BLOCKED[name]})" for name in sorted(values) if name in _BLOCKED]
        if blocked:
            raise AtlassianError(f"These fields cannot be set: {', '.join(blocked)}")
        key = await self.jira._writable_key(key)
        fields = await self._editmeta(key)
        for name in sorted(values):
            if name in fields and (reason := _blocked(name, fields[name])):
                raise AtlassianError(f"{name} cannot be set: {reason}")
        unknown = sorted(values.keys() - fields.keys())
        if unknown:
            valid = ", ".join(f for f, meta in fields.items() if not _blocked(f, meta))
            raise AtlassianError(
                f"Fields not editable on {key}: {', '.join(unknown)}. Editable field ids: {valid}"
            )
        # An epic link points at another issue, which must stay inside the allowed projects.
        for name, value in list(values.items()):
            if _custom(fields[name]).endswith(":gh-epic-link") and not _empty(value):
                if not isinstance(value, str):
                    raise AtlassianError(f"{name} must be an issue key")
                values[name] = await self.jira._writable_key(value)

        try:
            await self.client.send_json("PUT", f"/rest/api/2/issue/{key}", {"fields": values})
        except AtlassianError as exc:
            if exc.status != 400:
                raise
            return {
                "updated": False,
                "key": key,
                "reason": "Jira rejected the request. Nothing was changed.",
                "jira_errors": exc.details,
            }
        return {"updated": True, "key": key, "fields": sorted(values)}
