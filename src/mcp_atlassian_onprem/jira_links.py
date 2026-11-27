"""Links between issues. Both ends must be inside the allowed projects."""

from __future__ import annotations

import re

from .http import AtlassianError
from .jira import JiraService


class JiraLinks:
    def __init__(self, jira: JiraService) -> None:
        self.jira = jira
        self.client = jira.client

    async def list_link_types(self) -> dict:
        data = await self.client.get_json("/rest/api/2/issueLinkType")
        return {
            "link_types": [
                {"name": t.get("name"), "outward": t.get("outward"), "inward": t.get("inward")}
                for t in data.get("issueLinkTypes", [])
            ]
        }

    async def _resolve(self, relation: str) -> tuple[str, bool]:
        """Return the link type name and whether the relation is its inward direction."""
        types = (await self.list_link_types())["link_types"]
        wanted = relation.strip().casefold()

        def same(value: str | None) -> bool:
            return (value or "").casefold() == wanted

        matches = {(t["name"], False) for t in types if same(t["outward"])}
        # For symmetric types both directions read the same, so one of them is enough.
        matches |= {
            (t["name"], True)
            for t in types
            if same(t["inward"]) and (t["name"], False) not in matches
        }
        if not matches:
            matches = {
                (t["name"], False)
                for t in types
                if same(t["name"])
                and (t["outward"] or "").casefold() == (t["inward"] or "").casefold()
            }
        if len(matches) != 1:
            options = ", ".join(
                dict.fromkeys(repr(t[d]) for t in types for d in ("outward", "inward"))
            )
            raise AtlassianError(f"Unknown or ambiguous relation {relation!r}. Use: {options}")
        return matches.pop()

    async def link_issues(self, key: str, relation: str, target_key: str) -> dict:
        key = await self.jira._writable_key(key)
        target = await self.jira._writable_key(target_key)
        if key == target:
            raise AtlassianError("An issue cannot be linked to itself")
        name, inward = await self._resolve(relation)
        # Counter-intuitive but correct: in this request the "inwardIssue" is the one that
        # reads "<inwardIssue> <outward description> <outwardIssue>".
        source, destination = (target, key) if inward else (key, target)
        await self.client.send_json(
            "POST",
            "/rest/api/2/issueLink",
            {
                "type": {"name": name},
                "inwardIssue": {"key": source},
                "outwardIssue": {"key": destination},
            },
        )
        result = {"key": key, "target": target, "link_type": name}
        # The link exists now, so report a failed check instead of raising.
        try:
            links = (await self.jira.get_issue(key))["links"]
            result["links"] = [link for link in links if link["key"] == target]
        except AtlassianError as exc:
            result["link_check_failed"] = str(exc)
        return result

    async def delete_link(self, key: str, link_id: str) -> dict:
        link_id = str(link_id).strip()
        if not re.fullmatch(r"[0-9]+", link_id):
            raise AtlassianError("link_id must be a number from jira_get_issue")
        issue = await self.jira.get_issue(key)
        # get_issue hides links to other projects, so those cannot be deleted either.
        link = next((item for item in issue["links"] if str(item["id"]) == link_id), None)
        if link is None:
            raise AtlassianError(f"{issue['key']} has no link with id {link_id}")
        await self.jira._writable_key(link["key"])
        await self.client.delete(f"/rest/api/2/issueLink/{link_id}")
        return {"key": issue["key"], "deleted": link}
