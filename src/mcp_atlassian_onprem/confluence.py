"""Confluence Server / Data Center operations, restricted to the configured spaces."""

from __future__ import annotations

import html
import re
from typing import Any

from markdownify import markdownify

from .http import AtlassianError, RestClient
from .query import quote_text, scoped_cql

MAX_RESULTS = 100

_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def storage_to_markdown(storage: str) -> str:
    """Convert Confluence storage format to Markdown.

    Storage format is used on purpose: macros are not rendered, so an "include
    page" macro cannot pull in content from another space.
    """
    # Code macros keep their content in CDATA, which an HTML parser would mangle.
    prepared = _CDATA.sub(lambda m: f"<pre>{html.escape(m.group(1))}</pre>", storage)
    return markdownify(prepared, heading_style="ATX").strip()


def _page_id(value: str) -> str:
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise AtlassianError("Page id must be a number")
    return value


class ConfluenceService:
    def __init__(self, client: RestClient, spaces: tuple[str, ...]) -> None:
        self.client = client
        self.spaces = spaces

    def _in_scope(self, content: dict) -> bool:
        return (content.get("space") or {}).get("key") in self.spaces

    async def _fetch(self, page_id: str, expand: str = "space") -> dict:
        content = await self.client.get_json(
            f"/rest/api/content/{_page_id(page_id)}", {"expand": expand}
        )
        if not self._in_scope(content):
            raise AtlassianError(
                f"The page is outside the allowed spaces: {', '.join(self.spaces)}"
            )
        return content

    def _summary(self, content: dict) -> dict:
        version = content.get("version") or {}
        webui = (content.get("_links") or {}).get("webui")
        return {
            "id": content.get("id"),
            "type": content.get("type"),
            "title": content.get("title"),
            "space": (content.get("space") or {}).get("key"),
            "version": version.get("number"),
            "updated": version.get("when"),
            "updated_by": (version.get("by") or {}).get("displayName"),
            "url": f"{self.client.base_url}{webui}" if webui else None,
        }

    async def search(self, text: str = "", cql: str = "", limit: int = 25, start: int = 0) -> dict:
        if text.strip():
            if cql.strip():
                raise AtlassianError("Use either text or cql, not both")
            cql = f"text ~ {quote_text(text.strip())}"
        data = await self.client.get_json(
            "/rest/api/content/search",
            {
                "cql": scoped_cql(cql, self.spaces),
                "expand": "space,version",
                "limit": max(1, min(limit, MAX_RESULTS)),
                "start": max(0, start),
            },
        )
        return {
            "start": data.get("start"),
            "has_more": "next" in (data.get("_links") or {}),
            "results": [self._summary(c) for c in data.get("results", []) if self._in_scope(c)],
        }

    async def get_page(
        self, page_id: str, offset: int = 0, max_chars: int = 30000, raw: bool = False
    ) -> dict:
        page = await self._fetch(page_id, "space,version,body.storage,ancestors,metadata.labels")
        storage = ((page.get("body") or {}).get("storage") or {}).get("value", "")
        body = storage if raw else storage_to_markdown(storage)
        offset = max(0, offset)
        end = offset + max(1, min(max_chars, 100_000))
        labels = ((page.get("metadata") or {}).get("labels") or {}).get("results", [])
        return {
            **self._summary(page),
            "ancestors": [
                {"id": a.get("id"), "title": a.get("title")} for a in page.get("ancestors") or []
            ],
            "labels": [label.get("name") for label in labels],
            "body": body[offset:end],
            "total_chars": len(body),
            "next_offset": end if end < len(body) else None,
        }

    async def _children(
        self, page_id: str, kind: str, expand: str, limit: int, start: int, **extra: str
    ) -> tuple[list[dict], bool]:
        page = await self._fetch(page_id)
        data = await self.client.get_json(
            f"/rest/api/content/{page['id']}/child/{kind}",
            {
                "expand": expand,
                "limit": max(1, min(limit, MAX_RESULTS)),
                "start": max(0, start),
                **extra,
            },
        )
        return data.get("results", []), "next" in (data.get("_links") or {})

    async def get_children(self, page_id: str, limit: int = 50, start: int = 0) -> dict:
        results, more = await self._children(page_id, "page", "space,version", limit, start)
        return {
            "has_more": more,
            "pages": [self._summary(c) for c in results if self._in_scope(c)],
        }

    async def get_comments(self, page_id: str, limit: int = 50, start: int = 0) -> dict:
        results, more = await self._children(
            page_id, "comment", "body.storage,version", limit, start, depth="all"
        )
        return {
            "has_more": more,
            "comments": [
                {
                    "id": c.get("id"),
                    "author": ((c.get("version") or {}).get("by") or {}).get("displayName"),
                    "updated": (c.get("version") or {}).get("when"),
                    "body": storage_to_markdown(
                        ((c.get("body") or {}).get("storage") or {}).get("value", "")
                    ),
                }
                for c in results
            ],
        }

    # --- read-write mode -----------------------------------------------------
    # Request bodies are built here field by field. Space, status and restrictions
    # are never taken from the caller for an existing page.

    async def create_page(
        self, title: str, body: str, parent_id: str | None = None, space: str | None = None
    ) -> dict:
        if not title.strip():
            raise AtlassianError("Title is empty")
        if space and space not in self.spaces:
            raise AtlassianError(
                f"Space {space!r} is outside the allowed spaces: {', '.join(self.spaces)}"
            )
        parent = await self._fetch(parent_id) if parent_id else None
        if parent:
            parent_space = parent["space"]["key"]
            if parent.get("type") != "page":
                raise AtlassianError("The parent must be a page")
            if space and space != parent_space:
                raise AtlassianError(f"The parent page is in space {parent_space}, not {space}")
            space = parent_space
        if not space:
            if len(self.spaces) != 1:
                raise AtlassianError(f"Choose a space: {', '.join(self.spaces)}")
            space = self.spaces[0]

        payload: dict[str, Any] = {
            "type": "page",
            "title": title.strip(),
            "space": {"key": space},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        if parent:
            payload["ancestors"] = [{"id": parent["id"]}]
        created = await self.client.send_json("POST", "/rest/api/content", payload)
        return self._summary(created or {})

    async def update_page(
        self,
        page_id: str,
        version: int,
        title: str | None = None,
        body: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        message: str | None = None,
    ) -> dict:
        replacing = old_text is not None or new_text is not None
        if replacing and (not old_text or new_text is None):
            raise AtlassianError("Give both old_text (not empty) and new_text")
        if replacing and body is not None:
            raise AtlassianError("Use either body or old_text/new_text, not both")
        if body is None and not replacing and title is None:
            raise AtlassianError("Nothing to change")
        if title is not None and not title.strip():
            raise AtlassianError("Title is empty")

        page = await self._fetch(page_id, "space,version,body.storage")
        if page.get("type") not in {"page", "blogpost"}:
            raise AtlassianError("Only pages and blog posts can be edited")
        current = page["version"]["number"]
        if version != current:
            raise AtlassianError(
                f"The page is at version {current}, not {version}. Read it again before editing."
            )

        storage = page["body"]["storage"]["value"]
        if replacing:
            found = storage.count(old_text)
            if found != 1:
                raise AtlassianError(
                    f"old_text must occur exactly once in the storage format, found {found}"
                )
            storage = storage.replace(old_text, new_text)
        elif body is not None:
            storage = body

        payload: dict[str, Any] = {
            "id": page["id"],
            "type": page["type"],
            "title": title.strip() if title is not None else page["title"],
            "version": {"number": current + 1},
            "body": {"storage": {"value": storage, "representation": "storage"}},
        }
        if message:
            payload["version"]["message"] = message
        updated = await self.client.send_json("PUT", f"/rest/api/content/{page['id']}", payload)
        return self._summary({"space": page["space"], **(updated or {})})

    # --- attachments ---------------------------------------------------------

    async def list_attachments(self, page_id: str, limit: int = 50, start: int = 0) -> dict:
        results, more = await self._children(page_id, "attachment", "version", limit, start)
        return {
            "has_more": more,
            "attachments": [
                {
                    "id": a.get("id"),
                    "title": a.get("title"),
                    "media_type": (a.get("metadata") or {}).get("mediaType"),
                    "size": (a.get("extensions") or {}).get("fileSize"),
                }
                for a in results
            ],
        }
