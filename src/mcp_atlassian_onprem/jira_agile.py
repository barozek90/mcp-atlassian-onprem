"""Sprints of the Scrum boards that belong to the allowed projects."""

from __future__ import annotations

from .http import AtlassianError
from .jira import JiraService

_OPEN_STATES = ("active", "future")


class JiraSprints:
    def __init__(self, jira: JiraService) -> None:
        self.jira = jira
        self.client = jira.client

    async def _open_sprints(self) -> dict[int, dict]:
        sprints: dict[int, dict] = {}
        seen: set[int] = set()
        for project in self.jira.projects:
            boards = await self.jira._pages(
                "/rest/agile/1.0/board", {"projectKeyOrId": project, "type": "scrum"}
            )
            for board in boards:
                if board["id"] in seen:
                    continue
                seen.add(board["id"])
                for sprint in await self.jira._pages(
                    f"/rest/agile/1.0/board/{int(board['id'])}/sprint",
                    {"state": ",".join(_OPEN_STATES)},
                ):
                    if sprint.get("state") not in _OPEN_STATES:
                        continue
                    sprints.setdefault(
                        sprint["id"],
                        {
                            "id": sprint["id"],
                            "name": sprint.get("name"),
                            "state": sprint.get("state"),
                            "start": sprint.get("startDate"),
                            "end": sprint.get("endDate"),
                            "board": board.get("name"),
                        },
                    )
        return sprints

    async def list_sprints(self) -> dict:
        sprints = await self._open_sprints()
        return {"sprints": sorted(sprints.values(), key=lambda s: (s["state"], s["id"]))}

    async def add_to_sprint(self, key: str, sprint_id: int) -> dict:
        key = await self.jira._writable_key(key)
        sprint = (await self._open_sprints()).get(sprint_id)
        if sprint is None:
            raise AtlassianError(
                f"Sprint {sprint_id} is not an active or future sprint of the allowed projects. "
                "See jira_list_sprints."
            )
        await self.client.send_json(
            "POST", f"/rest/agile/1.0/sprint/{int(sprint_id)}/issue", {"issues": [key]}
        )
        return {"key": key, "sprint": sprint}

    async def move_to_backlog(self, key: str) -> dict:
        key = await self.jira._writable_key(key)
        await self.client.send_json("POST", "/rest/agile/1.0/backlog/issue", {"issues": [key]})
        return {"key": key, "moved_to": "backlog"}
