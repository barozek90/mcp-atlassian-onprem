"""Issue creation. Required fields are always read from Jira, per project and issue type."""

from __future__ import annotations

from typing import Any

from .http import AtlassianError
from .jira import JiraService

# Set by the server itself, never by the caller.
_RESERVED = {"project", "issuetype"}
# Listed by createmeta, but not settable through "fields".
_UNSUPPORTED = {"attachment", "issuelinks"}
_MAX_ALLOWED_VALUES = 200
_MAX_PAGES = 20


def _empty(value: Any) -> bool:
    return (
        value is None
        or (isinstance(value, (str, list, dict)) and not value)
        or (isinstance(value, str) and not value.strip())
    )


def _option(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    option = {k: value[k] for k in ("id", "key", "name", "value") if k in value}
    if value.get("children"):
        option["children"] = [_option(child) for child in value["children"]]
    return option


def _example(schema: dict) -> Any:
    """The JSON shape Jira expects for a field of this type."""
    kind = schema.get("type")
    if kind == "array":
        item = schema.get("items")
        return ["text"] if item == "string" else [_example({"type": item})]
    if str(schema.get("custom", "")).endswith(":cascadingselect"):
        return {"value": "parent option", "child": {"value": "child option"}}
    return {
        "string": "text",
        "number": 1,
        "date": "2026-01-31",
        "datetime": "2026-01-31T12:00:00.000+0100",
        "user": {"name": "username"},
        "group": {"name": "group name"},
        "option": {"value": "option value"},
        "priority": {"name": "priority name"},
        "resolution": {"name": "resolution name"},
        "version": {"name": "version name"},
        "component": {"name": "component name"},
        "securitylevel": {"id": "level id"},
        "timetracking": {"originalEstimate": "1d 2h"},
        "issuelink": {"key": "KEY-1"},
    }.get(kind or "", "see allowed_values")


def _describe(field: dict) -> dict:
    schema = field.get("schema") or {}
    kind = schema.get("type")
    if kind == "array":
        kind = f"array of {schema.get('items')}"
    result: dict[str, Any] = {
        "id": field["fieldId"],
        "name": field.get("name"),
        "type": kind,
        "example": _example(schema),
    }
    if str(schema.get("custom", "")).endswith(":gh-epic-link"):
        result["example"] = "KEY-1"
    allowed = field.get("allowedValues")
    if allowed:
        result["allowed_values"] = [_option(v) for v in allowed[:_MAX_ALLOWED_VALUES]]
        if len(allowed) > _MAX_ALLOWED_VALUES:
            result["allowed_values_truncated"] = len(allowed)
    return result


class JiraCreator:
    def __init__(self, jira: JiraService) -> None:
        self.jira = jira
        self.client = jira.client

    def _project(self, key: str) -> str:
        key = key.strip().upper()
        if key not in self.jira.projects:
            raise AtlassianError(
                f"Project {key!r} is outside the allowed projects: {', '.join(self.jira.projects)}"
            )
        return key

    # --- metadata ------------------------------------------------------------

    async def _pages(self, path: str) -> list[dict]:
        values: list[dict] = []
        for _ in range(_MAX_PAGES):
            data = await self.client.get_json(path, {"startAt": len(values), "maxResults": 50})
            page = data.get("values") or []
            values += page
            last = data.get("last", data.get("isLast"))
            total = data.get("total")
            if not page or last or (total is not None and len(values) >= total):
                return values
        raise AtlassianError("Jira returned too many pages of create metadata")

    async def _legacy_types(self, project: str) -> list[dict]:
        data = await self.client.get_json(
            "/rest/api/2/issue/createmeta",
            {"projectKeys": project, "expand": "projects.issuetypes.fields"},
        )
        for entry in data.get("projects", []):
            if entry.get("key") == project:
                return entry.get("issuetypes", [])
        return []

    async def _issue_types(self, project: str) -> list[dict]:
        try:
            types = await self._pages(f"/rest/api/2/issue/createmeta/{project}/issuetypes")
        except AtlassianError as exc:
            if exc.status != 404:
                raise
            types = await self._legacy_types(project)
        if not types:
            raise AtlassianError(
                f"No issue types available in {project}. The account may lack the "
                "'Create Issues' permission there."
            )
        return types

    async def _metadata(self, project: str, wanted: str) -> tuple[dict, dict[str, dict]]:
        """Return the issue type and its settable fields, keyed by field id."""
        types = await self._issue_types(project)
        wanted = str(wanted).strip()
        matches = [t for t in types if str(t.get("id")) == wanted] or [
            t for t in types if str(t.get("name", "")).casefold() == wanted.casefold()
        ]
        if len(matches) != 1:
            available = ", ".join(f"{t.get('name')} (id {t.get('id')})" for t in types)
            raise AtlassianError(f"Unknown issue type {wanted!r} in {project}. Use: {available}")
        issue_type = matches[0]

        if "fields" in issue_type and isinstance(issue_type["fields"], dict):
            raw = [{**meta, "fieldId": key} for key, meta in issue_type["fields"].items()]
        else:
            raw = await self._pages(
                f"/rest/api/2/issue/createmeta/{project}/issuetypes/{issue_type['id']}"
            )
        if not raw:
            raise AtlassianError(f"Jira returned no fields for {issue_type.get('name')}")
        fields = {f["fieldId"]: f for f in raw if f.get("fieldId") not in _RESERVED | _UNSUPPORTED}
        if issue_type.get("subtask"):
            parent = fields.get("parent") or {
                "fieldId": "parent",
                "name": "Parent",
                "schema": {"type": "issuelink"},
            }
            fields["parent"] = {**parent, "required": True, "hasDefaultValue": False}
        return issue_type, fields

    async def get_create_fields(self, project_key: str, issue_type: str | None = None) -> dict:
        project = self._project(project_key)
        if not issue_type:
            return {
                "project": project,
                "issue_types": [
                    {"id": t.get("id"), "name": t.get("name"), "subtask": t.get("subtask")}
                    for t in await self._issue_types(project)
                ],
                "next_step": "Call again with issue_type to get its required fields.",
            }
        chosen, fields = await self._metadata(project, issue_type)
        groups: dict[str, list[dict]] = {"required": [], "defaulted": [], "optional": []}
        for field in fields.values():
            if not field.get("required"):
                group = "optional"
            elif field.get("hasDefaultValue"):
                group = "defaulted"
            else:
                group = "required"
            groups[group].append(_describe(field))
        return {
            "project": project,
            "issue_type": {"id": chosen.get("id"), "name": chosen.get("name")},
            "required_fields": groups["required"],
            "required_with_default": groups["defaulted"],
            "optional_fields": groups["optional"],
            "note": "Workflow validators may require more than listed here. "
            "reporter defaults to the current account.",
        }

    # --- creation ------------------------------------------------------------

    async def _check_references(
        self, project: str, values: dict[str, Any], fields: dict[str, dict]
    ) -> None:
        """Fields that point at other issues must stay inside the allowed projects."""
        parent = values.get("parent")
        if not _empty(parent):
            if (
                not isinstance(parent, dict)
                or set(parent) != {"key"}
                or not isinstance(parent["key"], str)
            ):
                raise AtlassianError('parent must look like {"key": "KEY-1"}')
            key = await self.jira._writable_key(parent["key"])
            if key.rsplit("-", 1)[0] != project:
                raise AtlassianError("The parent must belong to the same project")
            values["parent"] = {"key": key}
        for field_id, value in list(values.items()):
            custom = str((fields[field_id].get("schema") or {}).get("custom", ""))
            if custom.endswith(":gh-epic-link") and not _empty(value):
                if not isinstance(value, str):
                    raise AtlassianError(f"{field_id} must be an issue key")
                values[field_id] = await self.jira._writable_key(value)

    async def create_issue(
        self, project_key: str, issue_type: str, values: dict[str, Any] | None = None
    ) -> dict:
        project = self._project(project_key)
        values = dict(values or {})
        reserved = sorted(_RESERVED & values.keys())
        if reserved:
            raise AtlassianError(f"Do not set {', '.join(reserved)} in fields")
        chosen, fields = await self._metadata(project, issue_type)
        unknown = sorted(values.keys() - fields.keys())
        if unknown:
            raise AtlassianError(
                f"Unknown fields: {', '.join(unknown)}. Valid field ids: {', '.join(fields)}"
            )

        required = [
            f for f in fields.values() if f.get("required") and not f.get("hasDefaultValue")
        ]
        # Jira fills in the reporter itself.
        missing = [
            f for f in required if f["fieldId"] != "reporter" and _empty(values.get(f["fieldId"]))
        ]
        context = {
            "project": project,
            "issue_type": {"id": chosen.get("id"), "name": chosen.get("name")},
        }
        if missing:
            return {
                "created": False,
                **context,
                "reason": "Required fields are missing. Nothing was created.",
                "missing_required_fields": [_describe(f) for f in missing],
            }
        await self._check_references(project, values, fields)

        payload = {
            "fields": {
                **values,
                "project": {"key": project},
                "issuetype": {"id": chosen["id"]},
            }
        }
        try:
            created = await self.client.send_json("POST", "/rest/api/2/issue", payload)
        except AtlassianError as exc:
            if exc.status != 400:
                raise
            return {
                "created": False,
                **context,
                "reason": "Jira rejected the request. Nothing was created.",
                "jira_errors": exc.details,
                "required_fields": [_describe(f) for f in required],
            }
        key = (created or {}).get("key")
        return {
            "created": True,
            **context,
            "key": key,
            "url": f"{self.client.base_url}/browse/{key}",
        }
