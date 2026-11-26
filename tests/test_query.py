import pytest

from mcp_atlassian_onprem.http import AtlassianError
from mcp_atlassian_onprem.query import quote_text, scoped_cql, scoped_jql


def test_empty_jql_is_scoped():
    assert scoped_jql("", ("OPS",)) == 'project in ("OPS") ORDER BY updated DESC'


def test_order_by_stays_outside_parentheses():
    assert scoped_jql("a = 1 OR b = 2 order by created", ("OPS", "DEV")) == (
        'project in ("OPS", "DEV") AND (a = 1 OR b = 2) order by created'
    )


def test_order_by_inside_quotes_is_text():
    assert scoped_jql('summary ~ "order by (x"', ("OPS",)) == (
        'project in ("OPS") AND (summary ~ "order by (x") ORDER BY updated DESC'
    )


@pytest.mark.parametrize(
    "query",
    [
        "status = Open) OR (project = SECRET",
        'summary ~ "x) OR (project = SECRET',
        "a = 1 \\) OR (b = 2",
        "(a = 1",
    ],
)
def test_escape_attempts_rejected(query):
    with pytest.raises(AtlassianError):
        scoped_jql(query, ("OPS",))


def test_cql_is_scoped():
    assert scoped_cql('title ~ "x"', ("DOC",)) == (
        'space in ("DOC") AND type in (page, blogpost) AND (title ~ "x")'
    )


def test_quote_text_cannot_break_out():
    assert quote_text('a" OR space = "X\\') == '"a\\" OR space = \\"X\\\\"'
