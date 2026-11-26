"""Wrap user supplied JQL / CQL so that it stays inside the configured scope.

This is the first line of defence. Callers must also drop any result whose
project or space is outside the scope.
"""

from __future__ import annotations

import re

from .http import AtlassianError

_ORDER_BY = re.compile(r"order\s+by\b", re.IGNORECASE)


def split_order_by(query: str) -> tuple[str, str]:
    """Split a query into (filter, ORDER BY clause) and reject unbalanced input.

    Quotes and parentheses must balance, so that the filter cannot close the
    parenthesis we wrap it in.
    """
    depth = 0
    quote = ""
    order_at: int | None = None
    index = 0
    while index < len(query):
        char = query[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char == "\\":
            raise AtlassianError("Backslash is only allowed inside quoted strings")
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise AtlassianError("Unbalanced parentheses in query")
        elif (
            order_at is None
            and depth == 0
            and (index == 0 or not (query[index - 1].isalnum() or query[index - 1] == "_"))
            and _ORDER_BY.match(query, index)
        ):
            order_at = index
        index += 1
    if quote or depth:
        raise AtlassianError("Unbalanced quotes or parentheses in query")
    if order_at is None:
        return query.strip(), ""
    return query[:order_at].strip(), query[order_at:].strip()


def _quoted_list(values: tuple[str, ...]) -> str:
    return ", ".join(f'"{value}"' for value in values)


def scoped_jql(jql: str, projects: tuple[str, ...]) -> str:
    where, order = split_order_by(jql)
    result = f"project in ({_quoted_list(projects)})"
    if where:
        result += f" AND ({where})"
    return f"{result} {order or 'ORDER BY updated DESC'}"


def scoped_cql(cql: str, spaces: tuple[str, ...]) -> str:
    where, order = split_order_by(cql)
    result = f"space in ({_quoted_list(spaces)}) AND type in (page, blogpost)"
    if where:
        result += f" AND ({where})"
    return f"{result} {order}".strip()


def quote_text(text: str) -> str:
    """Quote free text for use as a JQL / CQL string literal."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
