"""Configuration loaded from environment variables and command line flags."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

JIRA_PROJECT_RE = re.compile(r"[A-Z][A-Z0-9_]*")
# Personal spaces look like "~username".
CONFLUENCE_SPACE_RE = re.compile(r"~?[A-Za-z0-9._@-]+")


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class ServiceConfig:
    url: str
    scopes: tuple[str, ...]  # Jira project keys or Confluence space keys
    token: str | None = field(default=None, repr=False)
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ca_bundle: str | None = None
    read_write: bool = False


@dataclass(frozen=True)
class Config:
    jira: ServiceConfig | None
    confluence: ServiceConfig | None
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    timeout: float = 30.0


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _scopes(raw: str, pattern: re.Pattern[str], variable: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    for value in values:
        if not pattern.fullmatch(value):
            raise ConfigError(f"{variable}: invalid key {value!r}")
    return values


def _service(
    env: Mapping[str, str],
    prefix: str,
    scope_variable: str,
    scope_pattern: re.Pattern[str],
    override: str | None,
    rw_flag: bool,
) -> ServiceConfig | None:
    mode = env.get(f"{prefix}_MODE", "").strip().lower() or "ro"
    if mode not in {"ro", "rw"}:
        raise ConfigError(f"{prefix}_MODE must be 'ro' or 'rw'")

    url = env.get(f"{prefix}_URL", "").strip().rstrip("/")
    if not url:
        if override or rw_flag:
            raise ConfigError(f"{prefix}_URL is required when {prefix} options are given")
        return None

    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ConfigError(f"{prefix}_URL must be a full http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError(f"{prefix}_URL must not contain credentials, a query or a fragment")
    if parts.scheme == "http" and not _flag(env.get("ATLASSIAN_ALLOW_HTTP")):
        raise ConfigError(
            f"{prefix}_URL uses plain http; set ATLASSIAN_ALLOW_HTTP=1 if this is intentional"
        )

    scopes = _scopes(override or env.get(scope_variable, ""), scope_pattern, scope_variable)
    if not scopes:
        raise ConfigError(f"{scope_variable} is required: the server never works unscoped")

    token = env.get(f"{prefix}_TOKEN") or None
    username = env.get(f"{prefix}_USERNAME") or None
    password = env.get(f"{prefix}_PASSWORD") or None
    if not token and not (username and password):
        raise ConfigError(f"Set {prefix}_TOKEN, or both {prefix}_USERNAME and {prefix}_PASSWORD")

    return ServiceConfig(
        url=url,
        scopes=scopes,
        token=token,
        username=username,
        password=password,
        ca_bundle=env.get(f"{prefix}_CA_BUNDLE") or None,
        read_write=rw_flag or mode == "rw",
    )


def load_config(
    env: Mapping[str, str],
    *,
    jira_rw: bool = False,
    confluence_rw: bool = False,
    jira_projects: str | None = None,
    confluence_spaces: str | None = None,
) -> Config:
    """Build the configuration.

    Read-write mode is enabled per service, and only when asked for explicitly.
    """
    jira = _service(env, "JIRA", "JIRA_PROJECTS", JIRA_PROJECT_RE, jira_projects, jira_rw)
    confluence = _service(
        env,
        "CONFLUENCE",
        "CONFLUENCE_SPACES",
        CONFLUENCE_SPACE_RE,
        confluence_spaces,
        confluence_rw,
    )
    if jira is None and confluence is None:
        raise ConfigError("Nothing to serve: set JIRA_URL and/or CONFLUENCE_URL")

    try:
        max_bytes = int(env.get("ATLASSIAN_MAX_ATTACHMENT_BYTES") or DEFAULT_MAX_ATTACHMENT_BYTES)
        timeout = float(env.get("ATLASSIAN_TIMEOUT") or 30)
    except ValueError as exc:
        raise ConfigError(f"Invalid numeric setting: {exc}") from None

    return Config(
        jira=jira,
        confluence=confluence,
        max_attachment_bytes=max_bytes,
        timeout=timeout,
    )
