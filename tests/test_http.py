import pytest

from mcp_atlassian_onprem.http import (
    ForbiddenOperation,
    RestClient,
    confluence_rules,
    jira_rules,
)


def client(config, fake, read_write):
    return RestClient("Jira", config.jira, jira_rules(read_write), transport=fake.transport)


WRITE_REQUESTS = [
    ("POST", "/rest/api/2/issue/OPS-1/comment"),
    ("POST", "/rest/api/2/issue/OPS-1/transitions"),
    ("PUT", "/rest/api/2/issue/OPS-1/assignee"),
    ("GET", "/secure/attachment/10/log.txt"),
    ("POST", "/rest/api/2/issue"),
    ("GET", "/rest/api/2/issue/createmeta/OPS/issuetypes"),
    ("GET", "/rest/api/2/issue/createmeta/OPS/issuetypes/3"),
    ("GET", "/rest/api/2/issue/OPS-1/editmeta"),
    ("PUT", "/rest/api/2/issue/OPS-1"),
    ("GET", "/rest/agile/1.0/board"),
    ("GET", "/rest/agile/1.0/board/4/sprint"),
    ("POST", "/rest/agile/1.0/sprint/9/issue"),
    ("POST", "/rest/agile/1.0/backlog/issue"),
    ("GET", "/rest/api/2/issueLinkType"),
    ("POST", "/rest/api/2/issueLink"),
    ("DELETE", "/rest/api/2/issueLink/12"),
    ("GET", "/rest/api/2/issue/OPS-1/worklog"),
    ("GET", "/rest/api/2/issue/OPS-1/worklog/5"),
    ("POST", "/rest/api/2/issue/OPS-1/worklog"),
    ("PUT", "/rest/api/2/issue/OPS-1/worklog/5"),
]

ADMIN_REQUESTS = [
    ("PUT", "/rest/api/2/myself"),
    ("PUT", "/rest/api/2/myself/password"),
    ("PUT", "/rest/api/2/mypreferences"),
    ("POST", "/rest/api/2/user"),
    ("DELETE", "/rest/api/2/user"),
    ("POST", "/rest/api/2/project"),
    ("PUT", "/rest/api/2/project/OPS"),
    ("POST", "/rest/api/2/group/user"),
    ("POST", "/rest/pat/latest/tokens"),
    ("GET", "/rest/api/2/serverInfo"),
    ("DELETE", "/rest/api/2/issue/OPS-1"),
    ("POST", "/rest/api/2/issueLinkType"),
    ("DELETE", "/rest/api/2/issueLinkType/12"),
    ("POST", "/rest/agile/1.0/sprint"),
    ("POST", "/rest/agile/1.0/sprint/9"),
    ("PUT", "/rest/agile/1.0/sprint/9"),
    ("DELETE", "/rest/agile/1.0/sprint/9"),
    ("POST", "/rest/agile/1.0/board"),
    ("DELETE", "/rest/agile/1.0/board/4"),
    ("DELETE", "/rest/api/2/issue/OPS-1/worklog/5"),
    ("PUT", "/rest/api/2/issue/OPS-1/worklog"),
    ("POST", "/rest/api/2/issue/OPS-1/worklog/5"),
    ("POST", "/rest/api/2/issue/bulk"),
    ("GET", "/secure/admin/ViewApplicationProperties.jspa"),
]


@pytest.mark.parametrize("method, path", WRITE_REQUESTS)
def test_write_requests_blocked_in_read_only(ro_config, fake, method, path):
    with pytest.raises(ForbiddenOperation):
        client(ro_config, fake, False)._check(method, path)


@pytest.mark.parametrize("method, path", WRITE_REQUESTS)
def test_write_requests_allowed_in_read_write(rw_config, fake, method, path):
    client(rw_config, fake, True)._check(method, path)


@pytest.mark.parametrize("method, path", ADMIN_REQUESTS)
def test_administration_blocked_even_in_read_write(rw_config, fake, method, path):
    with pytest.raises(ForbiddenOperation):
        client(rw_config, fake, True)._check(method, path)


def confluence_client(config, fake, read_write):
    return RestClient(
        "Confluence", config.confluence, confluence_rules(read_write), transport=fake.transport
    )


@pytest.mark.parametrize(
    "method, path", [("POST", "/rest/api/content"), ("PUT", "/rest/api/content/7")]
)
def test_confluence_writes_need_read_write(rw_config, fake, method, path):
    with pytest.raises(ForbiddenOperation):
        confluence_client(rw_config, fake, False)._check(method, path)
    confluence_client(rw_config, fake, True)._check(method, path)


@pytest.mark.parametrize(
    "method, path",
    [
        ("DELETE", "/rest/api/content/7"),
        ("POST", "/rest/api/space"),
        ("PUT", "/rest/api/space/DOC"),
        ("DELETE", "/rest/api/space/DOC"),
        ("PUT", "/rest/api/content/7/restriction"),
        ("PUT", "/rest/experimental/content/7/restriction"),
        ("POST", "/rest/api/admin/user"),
        ("PUT", "/rest/api/admin/user/someone/password"),
        ("POST", "/rest/api/user/current/password"),
        ("PUT", "/rest/api/user/someone/group/confluence-administrators"),
        ("POST", "/rest/pat/latest/tokens"),
        ("POST", "/rpc/json-rpc/confluenceservice-v2"),
        ("POST", "/users/dochangemypassword.action"),
    ],
)
def test_confluence_administration_blocked(rw_config, fake, method, path):
    with pytest.raises(ForbiddenOperation):
        confluence_client(rw_config, fake, True)._check(method, path)


@pytest.mark.parametrize(
    "path",
    [
        "/secure/attachment/10/..",
        "/secure/attachment/10/%2e%2e",
        "/secure/attachment/10/a%2F..%2Fadmin",
        "/secure/attachment/10/a?x=1",
        "/rest/api/2/issue/OPS-1/../../user",
    ],
)
def test_path_tricks_blocked(rw_config, fake, path):
    with pytest.raises(ForbiddenOperation):
        client(rw_config, fake, True)._check("GET", path)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.test/jira/secure/attachment/1/a",
        "http://jira.example.test/jira/secure/attachment/1/a",
        "https://jira.example.test/other/secure/attachment/1/a",
    ],
)
def test_token_never_sent_outside_base_url(rw_config, fake, url):
    with pytest.raises(ForbiddenOperation):
        client(rw_config, fake, True).path_from_url(url)
