"""Command line entry point (stdio transport)."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .config import ConfigError, load_config
from .server import build_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-atlassian-onprem",
        description="MCP server for on-prem Jira and Confluence. Read-only by default.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--jira-rw",
        action="store_true",
        help="enable Jira issue creation and ticket operations (comments, status, ...)",
    )
    parser.add_argument(
        "--confluence-rw",
        action="store_true",
        help="enable Confluence page creation and editing",
    )
    parser.add_argument("--jira-project", help="allowed project keys, comma separated")
    parser.add_argument("--confluence-space", help="allowed space keys, comma separated")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    try:
        config = load_config(
            os.environ,
            jira_rw=args.jira_rw,
            confluence_rw=args.confluence_rw,
            jira_projects=args.jira_project,
            confluence_spaces=args.confluence_space,
        )
        server = build_server(config)
    except (ConfigError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    modes = ", ".join(
        f"{name}: {'read-write' if service.read_write else 'read-only'}"
        for name, service in (("Jira", config.jira), ("Confluence", config.confluence))
        if service
    )
    print(f"mcp-atlassian-onprem {__version__} starting ({modes})", file=sys.stderr)
    server.run()


if __name__ == "__main__":
    main()
