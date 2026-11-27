"""Time tracking tools, through a real MCP client session against a fake Jira."""

import re

import httpx
import pytest
from test_server import ISSUE, call, issue, payload

pytestmark = pytest.mark.anyio

WORKLOG = ISSUE + "/worklog"
ENTRY = WORKLOG + "/5"
ME = {"key": "JIRAUSER1", "name": "user-1"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def entry(author=ME, **extra):
    return {
        "id": "5",
        "author": author,
        "comment": "old",
        "started": "2026-01-30T09:00:00.000+0100",
        "timeSpent": "1h",
        "timeSpentSeconds": 3600,
        **extra,
    }


def routes(fake, current=None):
    fake.routes[("GET", ISSUE)] = issue()
    fake.routes[("GET", "/jira/rest/api/2/myself")] = ME
    fake.routes[("GET", ENTRY)] = current or entry()
    fake.routes[("PUT", ENTRY)] = entry()
    fake.routes[("POST", WORKLOG)] = httpx.Response(201, json=entry())


def write_params(fake):
    return [dict(r.url.params) for r in fake.requests if r.method != "GET"]


async def add(config, fake, **arguments):
    return await call(config, fake, "jira_add_worklog", {"issue_key": "OPS-1", **arguments})


async def update(config, fake, **arguments):
    arguments = {"issue_key": "OPS-1", "worklog_id": "5", **arguments}
    return await call(config, fake, "jira_update_worklog", arguments)


async def test_add_only_time(rw_config, fake):
    routes(fake)
    result = await add(rw_config, fake, time_spent=" 1H 30m ")
    assert payload(result)["worklog"]["id"] == "5"
    assert fake.writes == [("POST", WORKLOG, {"timeSpent": "1h 30m"})]
    assert write_params(fake) == [{"adjustEstimate": "auto"}]


async def test_add_with_all_options(rw_config, fake):
    routes(fake)
    result = await add(
        rw_config,
        fake,
        time_spent="2h",
        comment="done",
        started="2026-01-31T08:15:00+02:00",
        remaining_estimate="0h",
        visibility_role="Developers",
    )
    assert not result.isError
    assert fake.writes[0][2] == {
        "timeSpent": "2h",
        "comment": "done",
        "started": "2026-01-31T08:15:00.000+0200",
        "visibility": {"type": "role", "value": "Developers"},
    }
    assert write_params(fake) == [{"adjustEstimate": "new", "newEstimate": "0h"}]


@pytest.mark.parametrize(
    "value, expected",
    [("2026-01-31", "2026-01-31T12:00:00"), ("2026-07-01T07:30", "2026-07-01T07:30:00")],
)
async def test_started_without_zone_gets_local_zone(rw_config, fake, value, expected):
    routes(fake)
    await add(rw_config, fake, time_spent="1h", started=value)
    sent = fake.writes[0][2]["started"]
    assert re.fullmatch(re.escape(expected) + r"\.000[+-][0-9]{4}", sent)


@pytest.mark.parametrize(
    "arguments",
    [
        {"time_spent": "90"},
        {"time_spent": ""},
        {"time_spent": "0m"},
        {"time_spent": "-1h"},
        {"time_spent": "1h30m"},
        {"time_spent": "two hours"},
        {"time_spent": "1h", "started": "yesterday"},
        {"time_spent": "1h", "started": "2026-02-30"},
        {"time_spent": "1h", "started": "20260131"},
        {"time_spent": "1h", "remaining_estimate": "manual"},
        {"time_spent": "1h", "visibility_role": "a", "visibility_group": "b"},
    ],
)
async def test_invalid_values_never_reach_server(rw_config, fake, arguments):
    routes(fake)
    result = await add(rw_config, fake, **arguments)
    assert result.isError
    assert fake.requests == []


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("jira_get_worklogs", {}),
        ("jira_add_worklog", {"time_spent": "1h"}),
        ("jira_update_worklog", {"worklog_id": "5", "time_spent": "1h"}),
    ],
)
async def test_foreign_and_moved_issues_are_refused(rw_config, fake, tool, arguments):
    routes(fake)
    result = await call(rw_config, fake, tool, {"issue_key": "SECRET-1", **arguments})
    assert result.isError
    assert fake.requests == []
    # An old OPS key that now resolves to an issue in another project.
    fake.routes[("GET", ISSUE)] = issue("SECRET-7")
    result = await call(rw_config, fake, tool, {"issue_key": "OPS-1", **arguments})
    assert result.isError
    assert len(fake.requests) == 1


async def test_get_worklogs_pages_locally(rw_config, fake):
    routes(fake)
    entries = [entry(id=str(n)) for n in range(3)]
    fake.routes[("GET", WORKLOG)] = {"total": 3, "worklogs": entries}
    result = await call(
        rw_config, fake, "jira_get_worklogs", {"issue_key": "OPS-1", "limit": 1, "start": 1}
    )
    data = payload(result)
    assert data["total"] == 3
    assert [w["id"] for w in data["worklogs"]] == ["1"]
    assert data["worklogs"][0]["author"]["username"] == "user-1"
    assert "warning" not in data


async def test_get_worklogs_reports_partial_answer(rw_config, fake):
    routes(fake)
    fake.routes[("GET", WORKLOG)] = {"total": 40, "worklogs": [entry()]}
    result = await call(rw_config, fake, "jira_get_worklogs", {"issue_key": "OPS-1"})
    assert "1 of 40" in payload(result)["warning"]


async def test_update_time_keeps_the_rest(rw_config, fake):
    visibility = {"type": "group", "value": "staff"}
    routes(fake, entry(visibility=visibility))
    result = await update(rw_config, fake, time_spent="3h", remaining_estimate="leave")
    assert not result.isError
    expected = {
        "comment": "old",
        "started": "2026-01-30T09:00:00.000+0100",
        "visibility": visibility,
        "timeSpent": "3h",
    }
    assert fake.writes == [("PUT", ENTRY, expected)]
    assert write_params(fake) == [{"adjustEstimate": "leave"}]


async def test_update_comment_keeps_time(rw_config, fake):
    routes(fake)
    await update(rw_config, fake, comment="")
    assert fake.writes[0][2] == {
        "comment": "",
        "started": "2026-01-30T09:00:00.000+0100",
        "timeSpentSeconds": 3600,
    }


@pytest.mark.parametrize(
    "author",
    [
        {"key": "JIRAUSER2", "name": "user-2"},
        # A renamed account can leave the same name on a different user key.
        {"key": "JIRAUSER2", "name": "user-1"},
        {"name": "user-2"},
        {},
    ],
)
async def test_foreign_worklog_is_not_edited(rw_config, fake, author):
    routes(fake, entry(author=author))
    result = await update(rw_config, fake, time_spent="3h")
    assert result.isError
    assert "another user" in result.content[0].text
    assert fake.writes == []


async def test_own_worklog_matched_by_name_without_key(rw_config, fake):
    routes(fake, entry(author={"name": "user-1"}))
    result = await update(rw_config, fake, time_spent="3h")
    assert not result.isError
    assert len(fake.writes) == 1


@pytest.mark.parametrize(
    "arguments",
    [{}, {"worklog_id": "../5", "time_spent": "1h"}, {"time_spent": "0h"}],
)
async def test_invalid_update_never_reaches_server(rw_config, fake, arguments):
    routes(fake)
    result = await update(rw_config, fake, **arguments)
    assert result.isError
    assert fake.requests == []


async def test_worklog_tools_missing_in_read_only(ro_config, fake):
    result = await add(ro_config, fake, time_spent="1h")
    assert result.isError
    assert fake.requests == []
