"""Jira Server / Data Center operations, restricted to the configured projects."""

from __future__ import annotations

import re
from typing import Any

from .http import AtlassianError, RestClient
from .query import scoped_jql

_ISSUE_KEY = re.compile(r"([A-Z][A-Z0-9_]*)-[0-9]+")

_SUMMARY_FIELDS = (
    "project,summary,status,issuetype,priority,assignee,reporter,created,updated,resolution"
)
_DETAIL_FIELDS = (
    _SUMMARY_FIELDS
    + ",description,labels,components,fixVersions,duedate,parent,subtasks,issuelinks,attachment"
)

MAX_RESULTS = 100
_MAX_PAGES = 20


def _name(value: dict | None) -> str | None:
    return (value or {}).get("name")


def _person(value: dict | None) -> dict | None:
    if not value:
        return None
    return {"username": value.get("name"), "display_name": value.get("displayName")}


def _visibility(role: str | None, group: str | None) -> dict | None:
    if role and group:
        raise AtlassianError("Use either visibility_role or visibility_group, not both")
    if role:
        return {"type": "role", "value": role}
    if group:
        return {"type": "group", "value": group}
    return None


class JiraService:
    def __init__(self, client: RestClient, projects: tuple[str, ...]) -> None:
        self.client = client
        self.projects = projects

    # --- scope ---------------------------------------------------------------

    def _in_scope(self, key: str) -> bool:
        match = _ISSUE_KEY.fullmatch(key or "")
        return bool(match) and match.group(1) in self.projects

    def _normalize(self, key: str) -> str:
        key = key.strip().upper()
        if not self._in_scope(key):
            raise AtlassianError(
                f"Issue {key!r} is outside the allowed projects: {', '.join(self.projects)}"
            )
        return key

    def _verify(self, issue: dict) -> dict:
        """Check what the server actually returned: moved issues keep their old keys."""
        project = ((issue.get("fields") or {}).get("project") or {}).get("key")
        if project not in self.projects or not self._in_scope(issue.get("key", "")):
            raise AtlassianError("The issue belongs to a project outside the allowed scope")
        return issue

    async def _fetch(self, key: str, fields: str = "project") -> dict:
        issue = await self.client.get_json(
            f"/rest/api/2/issue/{self._normalize(key)}", {"fields": fields}
        )
        return self._verify(issue)

    async def _writable_key(self, key: str) -> str:
        """Return the current key of an issue after confirming it is in scope."""
        return (await self._fetch(key))["key"]

    async def _pages(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Collect all values of a paginated resource."""
        values: list[dict] = []
        for _ in range(_MAX_PAGES):
            data = await self.client.get_json(
                path, {**(params or {}), "startAt": len(values), "maxResults": 50}
            )
            page = data.get("values") or []
            values += page
            last = data.get("last", data.get("isLast"))
            total = data.get("total")
            if not page or last or (total is not None and len(values) >= total):
                return values
        raise AtlassianError(f"Jira returned too many pages for {path}")

    # --- reading -------------------------------------------------------------

    def _summary(self, issue: dict) -> dict:
        fields = issue.get("fields") or {}
        return {
            "key": issue.get("key"),
            "summary": fields.get("summary"),
            "type": _name(fields.get("issuetype")),
            "status": _name(fields.get("status")),
            "priority": _name(fields.get("priority")),
            "resolution": _name(fields.get("resolution")),
            "assignee": _person(fields.get("assignee")),
            "reporter": _person(fields.get("reporter")),
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "url": f"{self.client.base_url}/browse/{issue.get('key')}",
        }

    def _related(self, issue: dict | None) -> dict | None:
        """Reference to a parent, subtask or link. Out of scope issues are hidden."""
        if not issue or not self._in_scope(issue.get("key", "")):
            return None
        fields = issue.get("fields") or {}
        return {
            "key": issue["key"],
            "summary": fields.get("summary"),
            "status": _name(fields.get("status")),
        }

    async def search(self, jql: str = "", limit: int = 25, start: int = 0) -> dict:
        data = await self.client.get_json(
            "/rest/api/2/search",
            {
                "jql": scoped_jql(jql, self.projects),
                "fields": _SUMMARY_FIELDS,
                "maxResults": max(1, min(limit, MAX_RESULTS)),
                "startAt": max(0, start),
            },
        )
        issues = []
        for issue in data.get("issues", []):
            try:
                issues.append(self._summary(self._verify(issue)))
            except AtlassianError:
                continue
        return {"total": data.get("total"), "start": data.get("startAt"), "issues": issues}

    async def get_issue(self, key: str) -> dict:
        issue = await self._fetch(key, _DETAIL_FIELDS)
        fields = issue["fields"]
        links = []
        for link in fields.get("issuelinks") or []:
            for direction in ("inward", "outward"):
                related = self._related(link.get(f"{direction}Issue"))
                if related:
                    relation = (link.get("type") or {}).get(direction)
                    links.append({"id": link.get("id"), "relation": relation, **related})
        return {
            **self._summary(issue),
            "description": fields.get("description"),
            "labels": fields.get("labels") or [],
            "components": [_name(c) for c in fields.get("components") or []],
            "fix_versions": [_name(v) for v in fields.get("fixVersions") or []],
            "due_date": fields.get("duedate"),
            "parent": self._related(fields.get("parent")),
            "subtasks": [r for s in fields.get("subtasks") or [] if (r := self._related(s))],
            "links": links,
            "attachments": [
                {
                    "id": a.get("id"),
                    "filename": a.get("filename"),
                    "mime_type": a.get("mimeType"),
                    "size": a.get("size"),
                    "author": _person(a.get("author")),
                    "created": a.get("created"),
                }
                for a in fields.get("attachment") or []
            ],
        }

    async def get_comments(self, key: str, limit: int = 50, start: int = 0) -> dict:
        key = await self._writable_key(key)
        data = await self.client.get_json(
            f"/rest/api/2/issue/{key}/comment",
            {"maxResults": max(1, min(limit, MAX_RESULTS)), "startAt": max(0, start)},
        )
        return {
            "key": key,
            "total": data.get("total"),
            "start": data.get("startAt"),
            "comments": [self._comment(c) for c in data.get("comments", [])],
        }

    def _comment(self, comment: dict) -> dict:
        return {
            "id": comment.get("id"),
            "author": _person(comment.get("author")),
            "created": comment.get("created"),
            "updated": comment.get("updated"),
            "visibility": comment.get("visibility"),
            "body": comment.get("body"),
        }

    # --- read-write mode -----------------------------------------------------

    async def read_attachment(self, key: str, attachment_id: str, max_bytes: int) -> tuple:
        """Return (metadata, content) of an attachment that belongs to the given issue."""
        issue = await self._fetch(key, "project,attachment")
        for attachment in issue["fields"].get("attachment") or []:
            if str(attachment.get("id")) == str(attachment_id).strip():
                break
        else:
            raise AtlassianError(f"{issue['key']} has no attachment with id {attachment_id!r}")
        if (attachment.get("size") or 0) > max_bytes:
            raise AtlassianError(
                f"Attachment has {attachment['size']} bytes, the limit is {max_bytes}"
            )
        path = self.client.path_from_url(attachment.get("content", ""))
        return attachment, await self.client.download(path, max_bytes)

    async def add_comment(
        self,
        key: str,
        body: str,
        visibility_role: str | None = None,
        visibility_group: str | None = None,
    ) -> dict:
        if not body.strip():
            raise AtlassianError("Comment body is empty")
        payload: dict[str, Any] = {"body": body}
        visibility = _visibility(visibility_role, visibility_group)
        if visibility:
            payload["visibility"] = visibility
        key = await self._writable_key(key)
        created = await self.client.send_json("POST", f"/rest/api/2/issue/{key}/comment", payload)
        return {"key": key, "comment": self._comment(created or {})}

    async def _transitions(self, key: str) -> list[dict]:
        data = await self.client.get_json(
            f"/rest/api/2/issue/{key}/transitions", {"expand": "transitions.fields"}
        )
        return data.get("transitions", [])

    async def list_transitions(self, key: str) -> dict:
        key = await self._writable_key(key)
        return {
            "key": key,
            "transitions": [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "to_status": _name(t.get("to")),
                    "required_fields": [
                        name for name, f in (t.get("fields") or {}).items() if f.get("required")
                    ],
                    "resolutions": [
                        _name(v)
                        for v in ((t.get("fields") or {}).get("resolution") or {}).get(
                            "allowedValues", []
                        )
                    ],
                }
                for t in await self._transitions(key)
            ],
        }

    async def transition(
        self,
        key: str,
        transition_id: str,
        comment: str | None = None,
        resolution: str | None = None,
    ) -> dict:
        key = await self._writable_key(key)
        wanted = str(transition_id).strip()
        match = next((t for t in await self._transitions(key) if t.get("id") == wanted), None)
        if match is None:
            raise AtlassianError(f"Transition {wanted!r} is not available for {key}")
        payload: dict[str, Any] = {"transition": {"id": wanted}}
        # Jira silently drops a comment if the transition screen has no comment field.
        inline_comment = bool(comment) and "comment" in (match.get("fields") or {})
        if inline_comment:
            payload["update"] = {"comment": [{"add": {"body": comment}}]}
        if resolution:
            payload["fields"] = {"resolution": {"name": resolution}}
        await self.client.send_json("POST", f"/rest/api/2/issue/{key}/transitions", payload)
        result: dict[str, Any] = {"key": key, "transition": match.get("name")}
        if comment and not inline_comment:
            # The status has already changed, so report a failure here instead of raising.
            try:
                await self.add_comment(key, comment)
            except AtlassianError as exc:
                result["warning"] = f"Status changed, but the comment was not added: {exc}"
        try:
            status = (await self._fetch(key, "project,status"))["fields"].get("status")
            result["status"] = _name(status)
        except AtlassianError as exc:
            result["status_check_failed"] = str(exc)
        return result

    async def find_assignable_users(self, key: str, query: str = "", limit: int = 20) -> dict:
        key = await self._writable_key(key)
        users = await self.client.get_json(
            "/rest/api/2/user/assignable/search",
            {"issueKey": key, "username": query, "maxResults": max(1, min(limit, 50))},
        )
        return {"key": key, "users": [_person(u) for u in users]}

    async def assign(self, key: str, username: str | None) -> dict:
        """Assign to a username, or unassign when username is None."""
        if username is not None:
            username = username.strip()
            # "-1" means "automatic assignee" in Jira, which is not what a caller named.
            if not username or username == "-1":
                raise AtlassianError("Give a real username, or null to unassign")
        key = await self._writable_key(key)
        await self.client.send_json("PUT", f"/rest/api/2/issue/{key}/assignee", {"name": username})
        return {"key": key, "assignee": username}

    async def assign_to_me(self, key: str) -> dict:
        me = await self.client.get_json("/rest/api/2/myself")
        return await self.assign(key, me.get("name") or "")
