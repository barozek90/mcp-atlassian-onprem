"""Editing fields, moving issues between sprints and linking them."""

import httpx
import pytest
from test_server import call, issue, payload

ISSUE = "/jira/rest/api/2/issue/OPS-1"
OTHER = "/jira/rest/api/2/issue/OPS-2"
EDITMETA = ISSUE + "/editmeta"
BOARDS = "/jira/rest/agile/1.0/board"
LINK = "/jira/rest/api/2/issueLink"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def field(name, kind="string", custom=None):
    schema = {"type": kind}
    if custom:
        schema["custom"] = f"com.example:{custom}"
    return {"name": name, "schema": schema}


def editable(fake):
    fake.routes[("GET", ISSUE)] = issue()
    fake.routes[("GET", EDITMETA)] = {
        "fields": {
            "summary": field("Summary"),
            "priority": field("Priority", "priority"),
            "reporter": field("Reporter", "user"),
            "customfield_1": field("Sprint", "array", "gh-sprint"),
            "customfield_2": field("Epic Link", "any", "gh-epic-link"),
        }
    }
    fake.routes[("PUT", ISSUE)] = httpx.Response(204)


async def test_edit_fields_hide_blocked_ones(rw_config, fake):
    editable(fake)
    result = await call(rw_config, fake, "jira_get_edit_fields", {"issue_key": "OPS-1"})
    ids = [f["id"] for f in payload(result)["fields"]]
    assert ids == ["summary", "priority", "customfield_2"]


async def test_update_priority(rw_config, fake):
    editable(fake)
    changes = {"priority": {"name": "High"}}
    result = await call(
        rw_config, fake, "jira_update_issue", {"issue_key": "OPS-1", "fields": changes}
    )
    assert payload(result)["updated"] is True
    assert fake.writes == [("PUT", ISSUE, {"fields": changes})]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"reporter": {"name": "someone"}},
        {"assignee": {"name": "someone"}},
        {"project": {"key": "OPS"}},
        {"security": {"id": "1"}},
        {"customfield_1": 9},
        {"unknown": "x"},
        {"customfield_2": "SECRET-1"},
        {"customfield_2": {"key": "OPS-2"}},
    ],
)
async def test_unsafe_updates_are_refused(rw_config, fake, fields):
    editable(fake)
    result = await call(
        rw_config, fake, "jira_update_issue", {"issue_key": "OPS-1", "fields": fields}
    )
    assert result.isError
    assert fake.writes == []


async def test_rejected_update_reports_jira_errors(rw_config, fake):
    editable(fake)
    fake.routes[("PUT", ISSUE)] = httpx.Response(400, json={"errors": {"priority": "invalid"}})
    result = await call(
        rw_config,
        fake,
        "jira_update_issue",
        {"issue_key": "OPS-1", "fields": {"priority": {"name": "Nope"}}},
    )
    data = payload(result)
    assert data["updated"] is False
    assert data["jira_errors"] == ["priority: invalid"]


def sprints(fake):
    fake.routes[("GET", ISSUE)] = issue()
    fake.routes[("GET", BOARDS)] = {"isLast": True, "values": [{"id": 4, "name": "Board"}]}
    fake.routes[("GET", BOARDS + "/4/sprint")] = {
        "isLast": True,
        "values": [
            {"id": 9, "name": "Sprint 9", "state": "active"},
            {"id": 8, "name": "Sprint 8", "state": "closed"},
        ],
    }


async def test_list_sprints_of_allowed_projects(rw_config, fake):
    sprints(fake)
    result = await call(rw_config, fake, "jira_list_sprints")
    assert [s["id"] for s in payload(result)["sprints"]] == [9]
    assert fake.requests[0].url.params["projectKeyOrId"] == "OPS"


async def test_add_to_sprint(rw_config, fake):
    sprints(fake)
    path = "/jira/rest/agile/1.0/sprint/9/issue"
    fake.routes[("POST", path)] = httpx.Response(204)
    result = await call(
        rw_config, fake, "jira_add_to_sprint", {"issue_key": "OPS-1", "sprint_id": 9}
    )
    assert not result.isError
    assert fake.writes == [("POST", path, {"issues": ["OPS-1"]})]


@pytest.mark.parametrize("sprint_id", [8, 77])
async def test_closed_or_foreign_sprint_is_refused(rw_config, fake, sprint_id):
    sprints(fake)
    result = await call(
        rw_config, fake, "jira_add_to_sprint", {"issue_key": "OPS-1", "sprint_id": sprint_id}
    )
    assert result.isError
    assert fake.writes == []


async def test_move_to_backlog(rw_config, fake):
    fake.routes[("GET", ISSUE)] = issue()
    path = "/jira/rest/agile/1.0/backlog/issue"
    fake.routes[("POST", path)] = httpx.Response(204)
    result = await call(rw_config, fake, "jira_move_to_backlog", {"issue_key": "OPS-1"})
    assert not result.isError
    assert fake.writes == [("POST", path, {"issues": ["OPS-1"]})]


def linkable(fake, links=()):
    fake.routes[("GET", ISSUE)] = issue(issuelinks=list(links))
    fake.routes[("GET", OTHER)] = issue("OPS-2")
    fake.routes[("GET", "/jira/rest/api/2/issueLinkType")] = {
        "issueLinkTypes": [
            {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
            {"name": "Relates", "outward": "relates to", "inward": "relates to"},
        ]
    }
    fake.routes[("POST", LINK)] = httpx.Response(201)
    fake.routes[("DELETE", LINK + "/12")] = httpx.Response(204)


@pytest.mark.parametrize(
    "relation, link_type, first, second",
    [
        ("blocks", "Blocks", "OPS-1", "OPS-2"),
        ("Is Blocked By", "Blocks", "OPS-2", "OPS-1"),
        ("relates to", "Relates", "OPS-1", "OPS-2"),
        ("Relates", "Relates", "OPS-1", "OPS-2"),
    ],
)
async def test_link_direction(rw_config, fake, relation, link_type, first, second):
    linkable(fake)
    result = await call(
        rw_config,
        fake,
        "jira_link_issues",
        {"issue_key": "OPS-1", "relation": relation, "target_key": "OPS-2"},
    )
    assert not result.isError
    assert fake.writes == [
        (
            "POST",
            LINK,
            {
                "type": {"name": link_type},
                "inwardIssue": {"key": first},
                "outwardIssue": {"key": second},
            },
        )
    ]


@pytest.mark.parametrize(
    "relation, target",
    [("duplicates", "OPS-2"), ("blocks", "SECRET-1"), ("blocks", "OPS-1")],
)
async def test_unsafe_links_are_refused(rw_config, fake, relation, target):
    linkable(fake)
    result = await call(
        rw_config,
        fake,
        "jira_link_issues",
        {"issue_key": "OPS-1", "relation": relation, "target_key": target},
    )
    assert result.isError
    assert fake.writes == []


def existing_link(target):
    return {
        "id": "12",
        "type": {"outward": "blocks"},
        "outwardIssue": {"key": target, "fields": {}},
    }


async def test_delete_link(rw_config, fake):
    linkable(fake, [existing_link("OPS-2")])
    result = await call(
        rw_config, fake, "jira_delete_link", {"issue_key": "OPS-1", "link_id": "12"}
    )
    assert payload(result)["deleted"]["key"] == "OPS-2"
    assert [(r.method, r.url.path) for r in fake.requests if r.method != "GET"] == [
        ("DELETE", LINK + "/12")
    ]


@pytest.mark.parametrize("target, link_id", [("OPS-2", "13"), ("SECRET-1", "12"), ("OPS-2", "x")])
async def test_foreign_link_is_not_deleted(rw_config, fake, target, link_id):
    linkable(fake, [existing_link(target)])
    result = await call(
        rw_config, fake, "jira_delete_link", {"issue_key": "OPS-1", "link_id": link_id}
    )
    assert result.isError
    assert all(r.method == "GET" for r in fake.requests)
