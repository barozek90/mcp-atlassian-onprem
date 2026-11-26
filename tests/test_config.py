import pytest

from mcp_atlassian_onprem.config import ConfigError, load_config


def modes(config):
    return config.jira.read_write, config.confluence.read_write


def test_read_only_by_default(env):
    assert modes(load_config(env)) == (False, False)


@pytest.mark.parametrize(
    "kwargs, variables, expected",
    [
        ({"jira_rw": True}, {}, (True, False)),
        ({"confluence_rw": True}, {}, (False, True)),
        ({}, {"JIRA_MODE": "rw"}, (True, False)),
        ({}, {"CONFLUENCE_MODE": "rw"}, (False, True)),
    ],
)
def test_read_write_is_per_service(env, kwargs, variables, expected):
    assert modes(load_config({**env, **variables}, **kwargs)) == expected


@pytest.mark.parametrize("variable", ["JIRA_MODE", "CONFLUENCE_MODE"])
def test_unknown_mode_rejected(env, variable):
    env[variable] = "write"
    with pytest.raises(ConfigError):
        load_config(env)


def test_read_write_for_unconfigured_service_rejected(env):
    del env["JIRA_URL"]
    with pytest.raises(ConfigError):
        load_config(env, jira_rw=True)


@pytest.mark.parametrize("variable", ["JIRA_PROJECTS", "CONFLUENCE_SPACES"])
def test_scope_is_mandatory(env, variable):
    del env[variable]
    with pytest.raises(ConfigError, match=variable):
        load_config(env)


@pytest.mark.parametrize("value", ['OPS") OR (project = "X', "ops", "OPS;X"])
def test_invalid_project_key(env, value):
    env["JIRA_PROJECTS"] = value
    with pytest.raises(ConfigError):
        load_config(env)


def test_command_line_overrides_scope(env):
    config = load_config(env, jira_projects="AAA, BBB", confluence_spaces="~user-1")
    assert config.jira.scopes == ("AAA", "BBB")
    assert config.confluence.scopes == ("~user-1",)


def test_one_service_is_enough(env):
    for name in [n for n in env if n.startswith("CONFLUENCE_")]:
        del env[name]
    assert load_config(env).confluence is None


def test_plain_http_needs_opt_in(env):
    env["JIRA_URL"] = "http://jira.example.test"
    with pytest.raises(ConfigError, match="plain http"):
        load_config(env)
    env["ATLASSIAN_ALLOW_HTTP"] = "1"
    assert load_config(env).jira.url == "http://jira.example.test"


def test_secrets_not_in_repr(env):
    assert "jira-pat" not in repr(load_config(env))
