"""End to end tests: a real MCP client session against a fake Atlassian server."""

import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_atlassian_onprem.config import load_config
from mcp_atlassian_onprem.server import build_server

READ_TOOLS = {
    "jira_search",
    "jira_get_issue",
    "jira_get_comments",
    "confluence_search",
    "confluence_get_page",
    "confluence_get_children",
    "confluence_get_comments",
    "confluence_list_attachments",
}
WRITE_TOOLS = {
    "jira_read_attachment",
    "jira_add_comment",
    "jira_list_transitions",
    "jira_transition_issue",
    "jira_find_assignable_users",
    "jira_assign_issue",
    "jira_assign_to_me",
    "jira_get_create_fields",
    "jira_create_issue",
    "jira_get_edit_fields",
    "jira_update_issue",
    "jira_list_sprints",
    "jira_add_to_sprint",
    "jira_move_to_backlog",
    "jira_list_link_types",
    "jira_link_issues",
    "jira_delete_link",
    "confluence_create_page",
    "confluence_update_page",
}

ISSUE = "/jira/rest/api/2/issue/OPS-1"
PAGE = "/rest/api/content/7"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def payload(result):
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


def issue(key="OPS-1", **fields):
    return {"key": key, "fields": {"project": {"key": key.split("-")[0]}, **fields}}


async def call(config, fake, name, arguments=None):
    server = build_server(config, transport=fake.transport)
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        return await session.call_tool(name, arguments or {})


async def tool_names(config, fake):
    server = build_server(config, transport=fake.transport)
    return {tool.name for tool in await server.list_tools()}


pytestmark = pytest.mark.anyio


async def test_read_only_exposes_only_read_tools(ro_config, fake):
    assert await tool_names(ro_config, fake) == READ_TOOLS


async def test_read_write_adds_write_tools(rw_config, fake):
    assert await tool_names(rw_config, fake) == READ_TOOLS | WRITE_TOOLS


@pytest.mark.parametrize("service, other", [("jira", "confluence"), ("confluence", "jira")])
async def test_read_write_of_one_service_leaves_the_other_read_only(env, fake, service, other):
    config = load_config(env, **{f"{service}_rw": True})
    added = await tool_names(config, fake) - READ_TOOLS
    assert added == {name for name in WRITE_TOOLS if name.startswith(service)}
    assert not any(name.startswith(other) for name in added)


def page(space="DOC", version=3, storage="<p>old</p>", kind="page"):
    return {
        "id": "7",
        "type": kind,
        "title": "T",
        "space": {"key": space},
        "version": {"number": version},
        "body": {"storage": {"value": storage}},
    }


async def test_create_page_in_default_space(rw_config, fake):
    fake.routes[("POST", "/rest/api/content")] = page()
    result = await call(
        rw_config, fake, "confluence_create_page", {"title": "New", "body": "<p>x</p>"}
    )
    assert not result.isError
    assert fake.writes[0][2]["space"] == {"key": "DOC"}
    assert fake.writes[0][2]["type"] == "page"


@pytest.mark.parametrize("arguments", [{"space": "HR"}, {"parent_id": "7"}])
async def test_create_page_outside_scope_is_refused(rw_config, fake, arguments):
    fake.routes[("GET", PAGE)] = page(space="HR")
    result = await call(
        rw_config,
        fake,
        "confluence_create_page",
        {"title": "New", "body": "<p>x</p>", **arguments},
    )
    assert result.isError
    assert fake.writes == []


async def test_edit_fragment_keeps_the_rest(rw_config, fake):
    fake.routes[("GET", PAGE)] = page(storage="<p>keep</p><p>old</p>")
    fake.routes[("PUT", PAGE)] = page(version=4)
    result = await call(
        rw_config,
        fake,
        "confluence_update_page",
        {"page_id": "7", "version": 3, "old_text": "old", "new_text": "new"},
    )
    assert payload(result)["version"] == 4
    sent = fake.writes[0][2]
    assert sent["body"]["storage"]["value"] == "<p>keep</p><p>new</p>"
    assert sent["version"] == {"number": 4}
    assert sent["title"] == "T"
    assert not {"space", "status", "ancestors"} & sent.keys()


@pytest.mark.parametrize(
    "existing, changes",
    [
        (page(space="HR"), {"version": 3, "body": "x"}),
        (page(), {"version": 2, "body": "x"}),
        (page(kind="comment"), {"version": 3, "body": "x"}),
        (page(storage="aa aa"), {"version": 3, "old_text": "aa", "new_text": "b"}),
        (page(), {"version": 3, "old_text": "missing", "new_text": "b"}),
        (page(), {"version": 3}),
    ],
)
async def test_unsafe_edits_are_refused(rw_config, fake, existing, changes):
    fake.routes[("GET", PAGE)] = existing
    result = await call(rw_config, fake, "confluence_update_page", {"page_id": "7", **changes})
    assert result.isError
    assert fake.writes == []


async def test_write_tool_cannot_be_called_in_read_only(ro_config, fake):
    result = await call(ro_config, fake, "jira_add_comment", {"issue_key": "OPS-1", "body": "x"})
    assert result.isError
    assert fake.requests == []


async def test_out_of_scope_key_never_reaches_server(rw_config, fake):
    result = await call(rw_config, fake, "jira_add_comment", {"issue_key": "SECRET-1", "body": "x"})
    assert result.isError
    assert fake.requests == []


async def test_moved_issue_is_not_written(rw_config, fake):
    # An old OPS key that now resolves to an issue in another project.
    fake.routes[("GET", ISSUE)] = issue("SECRET-7")
    result = await call(rw_config, fake, "jira_add_comment", {"issue_key": "OPS-1", "body": "x"})
    assert result.isError
    assert fake.writes == []


async def test_search_drops_foreign_issues_and_scopes_jql(ro_config, fake):
    fake.routes[("GET", "/jira/rest/api/2/search")] = {
        "total": 2,
        "issues": [issue("OPS-1"), issue("SECRET-1", summary="hidden")],
    }
    result = await call(ro_config, fake, "jira_search", {"jql": "status = Open"})
    assert [i["key"] for i in payload(result)["issues"]] == ["OPS-1"]
    assert "hidden" not in result.content[0].text
    request = fake.requests[0]
    assert request.url.params["jql"].startswith('project in ("OPS") AND (status = Open)')
    assert request.headers["Authorization"] == "Bearer jira-pat"


async def test_foreign_links_are_hidden(ro_config, fake):
    fake.routes[("GET", ISSUE)] = issue(
        issuelinks=[
            {"type": {"outward": "blocks"}, "outwardIssue": {"key": "SECRET-2", "fields": {}}},
            {"type": {"outward": "blocks"}, "outwardIssue": {"key": "OPS-2", "fields": {}}},
        ]
    )
    result = await call(ro_config, fake, "jira_get_issue", {"issue_key": "ops-1"})
    assert [link["key"] for link in payload(result)["links"]] == ["OPS-2"]


async def test_add_comment(rw_config, fake):
    fake.routes[("GET", ISSUE)] = issue()
    fake.routes[("POST", ISSUE + "/comment")] = httpx.Response(201, json={"id": "5"})
    result = await call(rw_config, fake, "jira_add_comment", {"issue_key": "OPS-1", "body": "hi"})
    assert not result.isError
    assert fake.writes == [("POST", ISSUE + "/comment", {"body": "hi"})]


@pytest.mark.parametrize("on_screen", [True, False])
async def test_transition_never_loses_the_comment(rw_config, fake, on_screen):
    fields = {"comment": {"required": False}} if on_screen else {}
    fake.routes[("GET", ISSUE)] = issue(status={"name": "Done"})
    fake.routes[("GET", ISSUE + "/transitions")] = {
        "transitions": [{"id": "31", "name": "Close", "fields": fields}]
    }
    fake.routes[("POST", ISSUE + "/transitions")] = httpx.Response(204)
    fake.routes[("POST", ISSUE + "/comment")] = httpx.Response(201, json={"id": "5"})
    result = await call(
        rw_config,
        fake,
        "jira_transition_issue",
        {"issue_key": "OPS-1", "transition_id": "31", "comment": "bye"},
    )
    assert payload(result)["status"] == "Done"
    paths = [path for _, path, _ in fake.writes]
    if on_screen:
        assert paths == [ISSUE + "/transitions"]
        assert fake.writes[0][2]["update"]["comment"][0]["add"]["body"] == "bye"
    else:
        assert paths == [ISSUE + "/transitions", ISSUE + "/comment"]


@pytest.mark.parametrize("username", ["user-1", None])
async def test_assign_and_unassign(rw_config, fake, username):
    fake.routes[("GET", ISSUE)] = issue()
    fake.routes[("PUT", ISSUE + "/assignee")] = httpx.Response(204)
    result = await call(
        rw_config, fake, "jira_assign_issue", {"issue_key": "OPS-1", "username": username}
    )
    assert not result.isError
    assert fake.writes == [("PUT", ISSUE + "/assignee", {"name": username})]


async def test_read_attachment(rw_config, fake):
    url = "https://jira.example.test/jira/secure/attachment/10/app%20log.txt"
    fake.routes[("GET", ISSUE)] = issue(
        attachment=[{"id": "10", "filename": "app log.txt", "size": 5, "content": url}]
    )
    fake.routes[("GET", "/jira/secure/attachment/10/app log.txt")] = httpx.Response(
        200, content="błąd".encode()
    )
    result = await call(
        rw_config, fake, "jira_read_attachment", {"issue_key": "OPS-1", "attachment_id": "10"}
    )
    assert not result.isError
    assert result.content[1].text == "błąd"


async def test_attachment_of_another_issue_is_refused(rw_config, fake):
    fake.routes[("GET", ISSUE)] = issue(attachment=[])
    result = await call(
        rw_config, fake, "jira_read_attachment", {"issue_key": "OPS-1", "attachment_id": "99"}
    )
    assert result.isError
    assert len(fake.requests) == 1


async def test_page_outside_space_is_refused(ro_config, fake):
    fake.routes[("GET", "/rest/api/content/7")] = {"id": "7", "space": {"key": "HR"}}
    for tool in ("confluence_get_page", "confluence_get_comments"):
        result = await call(ro_config, fake, tool, {"page_id": "7"})
        assert result.isError
    assert all(r.url.path == "/rest/api/content/7" for r in fake.requests)


async def test_page_as_markdown_with_code_macro(ro_config, fake):
    storage = (
        "<h1>Title</h1><ac:structured-macro ac:name='code'><ac:plain-text-body>"
        "<![CDATA[if a < b: pass]]></ac:plain-text-body></ac:structured-macro>"
    )
    fake.routes[("GET", "/rest/api/content/7")] = {
        "id": "7",
        "title": "T",
        "space": {"key": "DOC"},
        "body": {"storage": {"value": storage}},
    }
    result = await call(ro_config, fake, "confluence_get_page", {"page_id": "7"})
    body = payload(result)["body"]
    assert "# Title" in body
    assert "if a < b: pass" in body


async def test_redirect_is_not_followed(ro_config, fake):
    fake.routes[("GET", ISSUE)] = httpx.Response(302, headers={"Location": "https://sso.test"})
    result = await call(ro_config, fake, "jira_get_issue", {"issue_key": "OPS-1"})
    assert result.isError
    assert "redirect" in result.content[0].text
    assert len(fake.requests) == 1
