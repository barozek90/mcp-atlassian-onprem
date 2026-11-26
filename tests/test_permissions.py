"""The example Claude Code permissions must match what the tools really do."""

import json
from pathlib import Path

import pytest

from mcp_atlassian_onprem.config import load_config
from mcp_atlassian_onprem.server import build_server

EXAMPLE = Path(__file__).parent.parent / "examples" / "claude-settings.json"
PREFIX = "mcp__atlassian__"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_only_read_only_tools_are_allowed_without_asking(env, fake):
    config = load_config(env, jira_rw=True, confluence_rw=True)
    tools = await build_server(config, transport=fake.transport).list_tools()
    permissions = json.loads(EXAMPLE.read_text())["permissions"]

    read_only = {PREFIX + t.name for t in tools if t.annotations.readOnlyHint}
    writing = {PREFIX + t.name for t in tools if not t.annotations.readOnlyHint}
    assert sorted(permissions["allow"]) == sorted(read_only)
    assert sorted(permissions["ask"]) == sorted(writing)
