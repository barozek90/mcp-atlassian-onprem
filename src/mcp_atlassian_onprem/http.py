"""The only place that talks to the network.

Every request is checked against an explicit allowlist of (method, path) rules.
Anything not listed here cannot be sent, whatever the calling code asks for.
"""

from __future__ import annotations

import re
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from . import __version__
from .config import ServiceConfig


class AtlassianError(Exception):
    """An error that is safe to show to the MCP client."""

    def __init__(
        self, message: str, *, status: int | None = None, details: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.details = details or []


class ForbiddenOperation(AtlassianError):
    """The request is not on the allowlist."""


@dataclass(frozen=True)
class Rule:
    method: str
    pattern: str

    def matches(self, method: str, path: str) -> bool:
        return self.method == method and re.fullmatch(self.pattern, path) is not None


_ISSUE = r"[A-Z][A-Z0-9_]*-[0-9]+"
_ID = r"[0-9]+"
_PROJECT = r"[A-Z][A-Z0-9_]*"

JIRA_READ_RULES = (
    Rule("GET", r"/rest/api/2/search"),
    Rule("GET", rf"/rest/api/2/issue/{_ISSUE}"),
    Rule("GET", rf"/rest/api/2/issue/{_ISSUE}/comment"),
)

# Everything that --rw adds. Ticket work only: no administration, no account settings.
JIRA_WRITE_RULES = (
    Rule("GET", rf"/rest/api/2/issue/{_ISSUE}/transitions"),
    Rule("GET", r"/rest/api/2/myself"),
    Rule("GET", r"/rest/api/2/user/assignable/search"),
    Rule("GET", rf"/secure/attachment/{_ID}/[^/]+"),
    Rule("POST", rf"/rest/api/2/issue/{_ISSUE}/comment"),
    Rule("POST", rf"/rest/api/2/issue/{_ISSUE}/transitions"),
    Rule("PUT", rf"/rest/api/2/issue/{_ISSUE}/assignee"),
    # Issue creation. The second form of createmeta is for Jira older than 8.4.
    Rule("GET", rf"/rest/api/2/issue/createmeta/{_PROJECT}/issuetypes(/{_ID})?"),
    Rule("GET", r"/rest/api/2/issue/createmeta"),
    Rule("POST", r"/rest/api/2/issue"),
    # Editing fields. Which fields may be set is checked in jira_edit.py.
    Rule("GET", rf"/rest/api/2/issue/{_ISSUE}/editmeta"),
    Rule("PUT", rf"/rest/api/2/issue/{_ISSUE}"),
    # Sprints: moving issues only, no creating, changing or deleting sprints or boards.
    Rule("GET", r"/rest/agile/1.0/board"),
    Rule("GET", rf"/rest/agile/1.0/board/{_ID}/sprint"),
    Rule("POST", rf"/rest/agile/1.0/sprint/{_ID}/issue"),
    Rule("POST", r"/rest/agile/1.0/backlog/issue"),
    # Issue links. Deleting a link is the only DELETE the server can send.
    Rule("GET", r"/rest/api/2/issueLinkType"),
    Rule("POST", r"/rest/api/2/issueLink"),
    Rule("DELETE", rf"/rest/api/2/issueLink/{_ID}"),
)

CONFLUENCE_READ_RULES = (
    Rule("GET", r"/rest/api/content/search"),
    Rule("GET", rf"/rest/api/content/{_ID}"),
    Rule("GET", rf"/rest/api/content/{_ID}/child/(page|comment|attachment)"),
)

# Everything that --rw adds. Creating and editing content only: no deleting, no space,
# permission or user administration, no account settings.
CONFLUENCE_WRITE_RULES = (
    Rule("POST", r"/rest/api/content"),
    Rule("PUT", rf"/rest/api/content/{_ID}"),
)


def jira_rules(read_write: bool) -> tuple[Rule, ...]:
    return JIRA_READ_RULES + (JIRA_WRITE_RULES if read_write else ())


def confluence_rules(read_write: bool) -> tuple[Rule, ...]:
    return CONFLUENCE_READ_RULES + (CONFLUENCE_WRITE_RULES if read_write else ())


class RestClient:
    def __init__(
        self,
        service: str,
        config: ServiceConfig,
        rules: Iterable[Rule],
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service = service
        self.base_url = config.url
        self._rules = tuple(rules)

        headers = {
            "Accept": "application/json",
            "User-Agent": f"mcp-atlassian-onprem/{__version__}",
            # Needed for non-browser writes; harmless for reads.
            "X-Atlassian-Token": "no-check",
        }
        auth = None
        if config.token:
            headers["Authorization"] = f"Bearer {config.token}"
        else:
            auth = httpx.BasicAuth(config.username or "", config.password or "")

        verify: ssl.SSLContext | bool = True
        if config.ca_bundle:
            verify = ssl.create_default_context(cafile=config.ca_bundle)

        self._client = httpx.AsyncClient(
            headers=headers,
            auth=auth,
            verify=verify,
            timeout=timeout,
            # A redirect could lead outside the allowlist or to another host.
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check(self, method: str, path: str) -> None:
        decoded = unquote(path)
        segments = decoded.split("/")
        unsafe = (
            not path.startswith("/")
            or any(char in path for char in "?#\\")
            or any(segment in {".", ".."} for segment in segments)
            # An encoded slash would add path segments after the check.
            or len(segments) != len(path.split("/"))
        )
        if unsafe or not any(rule.matches(method, path) for rule in self._rules):
            raise ForbiddenOperation(
                f"{self.service}: {method} {path} is not allowed by this server"
            )

    def path_from_url(self, url: str) -> str:
        """Turn an absolute URL returned by the server into a path below the base URL."""
        base, target = urlsplit(self.base_url), urlsplit(url)
        prefix = base.path + "/"
        if (target.scheme, target.netloc) != (
            base.scheme,
            base.netloc,
        ) or not target.path.startswith(prefix):
            raise ForbiddenOperation(
                f"{self.service}: refusing a URL outside the configured base URL"
            )
        return target.path[len(base.path) :]

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._json(await self._send("GET", path, params=params))

    async def send_json(self, method: str, path: str, body: dict[str, Any]) -> Any:
        response = await self._send(method, path, json=body)
        return self._json(response) if response.content else None

    async def delete(self, path: str) -> None:
        await self._send("DELETE", path)

    async def download(self, path: str, max_bytes: int) -> bytes:
        self._check("GET", path)
        data = bytearray()
        try:
            async with self._client.stream(
                "GET", self.base_url + path, headers={"Accept": "*/*"}
            ) as response:
                if not response.is_success:
                    await response.aread()
                    raise self._error(response)
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise AtlassianError(f"Attachment is larger than {max_bytes} bytes")
        except httpx.HTTPError as exc:
            raise AtlassianError(f"{self.service}: request failed: {exc!r}") from None
        return bytes(data)

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._check(method, path)
        try:
            response = await self._client.request(method, self.base_url + path, **kwargs)
        except httpx.HTTPError as exc:
            raise AtlassianError(f"{self.service}: request failed: {exc!r}") from None
        if not response.is_success:
            raise self._error(response)
        return response

    def _json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise AtlassianError(
                f"{self.service}: expected JSON but got something else "
                "(check the URL and whether SSO intercepts REST calls)"
            ) from None

    def _error(self, response: httpx.Response) -> AtlassianError:
        status = response.status_code
        if 300 <= status < 400:
            return AtlassianError(
                f"{self.service}: server answered with a redirect ({status}), which is not "
                "followed. Check the base URL and authentication."
            )
        details: list[str] = []
        try:
            body = response.json()
            details += body.get("errorMessages") or []
            details += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
            if body.get("message"):
                details.append(body["message"])
        except (ValueError, AttributeError, TypeError):
            pass
        hint = {401: "authentication failed", 403: "permission denied", 404: "not found"}.get(
            status, "request rejected"
        )
        suffix = f": {'; '.join(map(str, details))[:500]}" if details else ""
        return AtlassianError(
            f"{self.service}: HTTP {status}, {hint}{suffix}",
            status=status,
            details=[str(item) for item in details],
        )
