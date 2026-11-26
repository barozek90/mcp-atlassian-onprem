import json

import httpx
import pytest

from mcp_atlassian_onprem.config import load_config

ENV = {
    "JIRA_URL": "https://jira.example.test/jira",
    "JIRA_TOKEN": "jira-pat",
    "JIRA_PROJECTS": "OPS",
    "CONFLUENCE_URL": "https://wiki.example.test",
    "CONFLUENCE_TOKEN": "wiki-pat",
    "CONFLUENCE_SPACES": "DOC",
}


class FakeServer:
    """Records requests and answers from a {(method, path): response} table."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], object] = {}
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"errorMessages": ["no route"]})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    @property
    def writes(self):
        return [
            (r.method, r.url.path, json.loads(r.content))
            for r in self.requests
            if r.method != "GET"
        ]


@pytest.fixture
def fake():
    return FakeServer()


@pytest.fixture
def env():
    return dict(ENV)


@pytest.fixture
def ro_config():
    return load_config(ENV)


@pytest.fixture
def rw_config():
    return load_config(ENV, jira_rw=True, confluence_rw=True)
