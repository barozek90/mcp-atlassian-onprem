import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_atlassian_onprem.server import build_server

META = "/jira/rest/api/2/issue/createmeta/OPS/issuetypes"
CREATE = "/jira/rest/api/2/issue"


@pytest.fixture
def anyio_backend():
    return "asyncio"


pytestmark = pytest.mark.anyio


def field(field_id, required=False, default=False, kind="string", **extra):
    return {
        "fieldId": field_id,
        "name": field_id.title(),
        "required": required,
        "hasDefaultValue": default,
        "schema": {"type": kind},
        **extra,
    }


FIELDS = [
    field("project", True, kind="project"),
    field("issuetype", True, kind="issuetype"),
    field("summary", True),
    field("reporter", True, kind="user"),
    field("priority", True, True, kind="priority"),
    field("description"),
    field(
        "customfield_10100",
        True,
        kind="option",
        allowedValues=[{"id": "1", "value": "Prod", "self": "x"}],
    ),
]


@pytest.fixture
def jira(fake):
    fake.routes[("GET", META)] = {
        "last": True,
        "values": [
            {"id": "3", "name": "Task", "subtask": False},
            {"id": "5", "name": "Sub-task", "subtask": True},
        ],
    }
    fake.routes[("GET", META + "/3")] = {"last": True, "values": FIELDS}
    fake.routes[("GET", META + "/5")] = {"last": True, "values": FIELDS}
    fake.routes[("POST", CREATE)] = httpx.Response(201, json={"key": "OPS-9"})
    return fake


async def call(config, fake, name, arguments):
    server = build_server(config, transport=fake.transport)
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        return await session.call_tool(name, arguments)


async def create(config, fake, fields, issue_type="Task", project="OPS"):
    return await call(
        config,
        fake,
        "jira_create_issue",
        {"project_key": project, "issue_type": issue_type, "fields": fields},
    )


def payload(result):
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


def ids(items):
    return [item["id"] for item in items]


async def test_lists_issue_types(rw_config, jira):
    result = await call(rw_config, jira, "jira_get_create_fields", {"project_key": "ops"})
    assert [t["name"] for t in payload(result)["issue_types"]] == ["Task", "Sub-task"]


async def test_reports_required_fields(rw_config, jira):
    result = await call(
        rw_config, jira, "jira_get_create_fields", {"project_key": "OPS", "issue_type": "task"}
    )
    data = payload(result)
    assert ids(data["required_fields"]) == ["summary", "reporter", "customfield_10100"]
    assert ids(data["required_with_default"]) == ["priority"]
    assert ids(data["optional_fields"]) == ["description"]
    custom = data["required_fields"][-1]
    assert custom["allowed_values"] == [{"id": "1", "value": "Prod"}]
    assert custom["example"] == {"value": "option value"}


async def test_missing_fields_are_returned_and_nothing_is_created(rw_config, jira):
    data = payload(await create(rw_config, jira, {"summary": "  "}))
    assert data["created"] is False
    assert ids(data["missing_required_fields"]) == ["summary", "customfield_10100"]
    assert jira.writes == []


async def test_creates_issue(rw_config, jira):
    values = {"summary": "S", "customfield_10100": {"value": "Prod"}}
    data = payload(await create(rw_config, jira, values))
    assert data["key"] == "OPS-9"
    assert jira.writes == [
        (
            "POST",
            CREATE,
            {"fields": {**values, "project": {"key": "OPS"}, "issuetype": {"id": "3"}}},
        )
    ]


async def test_jira_validation_errors_are_returned(rw_config, jira):
    jira.routes[("POST", CREATE)] = httpx.Response(
        400, json={"errors": {"customfield_10200": "Team is required."}}
    )
    data = payload(
        await create(rw_config, jira, {"summary": "S", "customfield_10100": {"id": "1"}})
    )
    assert data["created"] is False
    assert data["jira_errors"] == ["customfield_10200: Team is required."]
    assert "summary" in ids(data["required_fields"])


@pytest.mark.parametrize(
    "values",
    [
        {"project": {"key": "SECRET"}},
        {"issuetype": {"id": "1"}},
        {"unknown_field": "x"},
    ],
)
async def test_forbidden_fields(rw_config, jira, values):
    result = await create(rw_config, jira, {"summary": "S", **values})
    assert result.isError
    assert jira.writes == []


async def test_foreign_project_never_reaches_server(rw_config, jira):
    result = await create(rw_config, jira, {"summary": "S"}, project="SECRET")
    assert result.isError
    assert jira.requests == []


async def test_subtask_requires_parent(rw_config, jira):
    values = {"summary": "S", "customfield_10100": {"id": "1"}}
    data = payload(await create(rw_config, jira, values, issue_type="Sub-task"))
    assert ids(data["missing_required_fields"]) == ["parent"]


@pytest.mark.parametrize("parent", [{"key": "SECRET-1"}, {"id": "100"}, "OPS-1"])
async def test_parent_outside_scope_is_refused(rw_config, jira, parent):
    values = {"summary": "S", "customfield_10100": {"id": "1"}, "parent": parent}
    result = await create(rw_config, jira, values, issue_type="Sub-task")
    assert result.isError
    assert jira.writes == []


async def test_old_jira_falls_back_to_legacy_createmeta(rw_config, fake):
    fake.routes[("GET", "/jira/rest/api/2/issue/createmeta")] = {
        "projects": [
            {
                "key": "OPS",
                "issuetypes": [
                    {"id": "3", "name": "Task", "fields": {"summary": {"required": True}}}
                ],
            }
        ]
    }
    result = await call(
        rw_config, fake, "jira_get_create_fields", {"project_key": "OPS", "issue_type": "3"}
    )
    assert ids(payload(result)["required_fields"]) == ["summary"]


async def test_field_pages_are_followed(rw_config, jira):
    def answer(request):
        start = int(request.url.params["startAt"])
        return httpx.Response(200, json={"last": start == 1, "values": [FIELDS[2 + start]]})

    original = jira._handle
    jira.transport = httpx.MockTransport(
        lambda r: answer(r) if r.url.path == META + "/3" else original(r)
    )
    result = await call(
        rw_config, jira, "jira_get_create_fields", {"project_key": "OPS", "issue_type": "3"}
    )
    assert ids(payload(result)["required_fields"]) == ["summary", "reporter"]
