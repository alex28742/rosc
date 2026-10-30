"""Agent prompt/report sync configuration.

A tracked example (``docs/agent_sync.example.yml``) plus a gitignored local file
(``docs/agent_sync.local.yml``) holding the real, personal ROSC-only Drive
folder IDs. No personal Drive ID is ever hardcoded in tracked source. Environment
variables override the file for CI/one-off use.

The OAuth *client* (``credentials.json``) may be shared with any future
documentation-mirror transport, but the token is scope-bound and therefore agent
sync keeps its own token file by default -- re-authorizing agent sync must never
silently change another mechanism's scope.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "docs" / "agent_sync.local.yml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "docs" / "agent_sync.example.yml"
DEFAULT_CREDENTIALS_PATH = REPO_ROOT / "credentials.json"
DEFAULT_TOKEN_PATH = REPO_ROOT / "token.agent-sync.json"
DEFAULT_PROMPTS_FOLDER_NAME = "Agent Prompts / Drafts"
DEFAULT_REPORTS_FOLDER_NAME = "Agent Reports / Archive"

_KNOWN_KEYS = {
    "prompts_folder_id",
    "reports_folder_id",
    "credentials_path",
    "token_path",
    "prompts_folder_name",
    "reports_folder_name",
}

# Config key -> environment variable. Environment wins over the file so a
# machine/CI can override without editing tracked or local config.
_ENV_OVERRIDES = {
    "prompts_folder_id": "AGENT_SYNC_PROMPTS_FOLDER_ID",
    "reports_folder_id": "AGENT_SYNC_REPORTS_FOLDER_ID",
    "credentials_path": "AGENT_SYNC_CREDENTIALS_PATH",
    "token_path": "AGENT_SYNC_TOKEN_PATH",
    "prompts_folder_name": "AGENT_SYNC_PROMPTS_FOLDER_NAME",
    "reports_folder_name": "AGENT_SYNC_REPORTS_FOLDER_NAME",
}


@dataclass(frozen=True)
class SyncConfig:
    prompts_folder_id: str
    reports_folder_id: str
    credentials_path: Path
    token_path: Path
    prompts_folder_name: str
    reports_folder_name: str
    config_path: Path

    @property
    def has_prompts_folder(self) -> bool:
        return bool(self.prompts_folder_id)

    @property
    def has_reports_folder(self) -> bool:
        return bool(self.reports_folder_id)


def _parse_flat_yaml(text: str) -> dict[str, str]:
    """Parse the small flat ``key: value`` shape this config uses.

    Deliberately not a general YAML parser (and does not require PyYAML).
    """

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in _KNOWN_KEYS:
            values[key] = value
    return values


def resolve_config_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return EXAMPLE_CONFIG_PATH


def _resolve_path(value: str, default: Path) -> Path:
    """Interpret a relative configured path against the repository root."""

    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def load_config(config_path: Path | None = None) -> SyncConfig:
    """Resolve the effective sync configuration.

    Precedence per key: environment variable > config file > default. A
    blank/missing folder ID stays blank and is never guessed.
    """

    resolved = resolve_config_path(config_path)
    text = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
    values = _parse_flat_yaml(text)

    def _value(key: str, default: str = "") -> str:
        env = os.environ.get(_ENV_OVERRIDES[key])
        if env:
            return env
        return values.get(key, "") or default

    return SyncConfig(
        prompts_folder_id=_value("prompts_folder_id"),
        reports_folder_id=_value("reports_folder_id"),
        credentials_path=_resolve_path(
            _value("credentials_path", ""), DEFAULT_CREDENTIALS_PATH
        ),
        token_path=_resolve_path(_value("token_path", ""), DEFAULT_TOKEN_PATH),
        prompts_folder_name=_value("prompts_folder_name", DEFAULT_PROMPTS_FOLDER_NAME),
        reports_folder_name=_value("reports_folder_name", DEFAULT_REPORTS_FOLDER_NAME),
        config_path=resolved,
    )

