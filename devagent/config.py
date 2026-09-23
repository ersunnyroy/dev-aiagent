"""Configuration loading + secret lookup (Keychain first, env vars second)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HOME_DIR = Path(os.environ.get("DEVAGENT_HOME", "~/.devagent")).expanduser()
CONFIG_PATH = HOME_DIR / "config.yaml"
KEYRING_SERVICE = "devagent"

SECRET_NAMES = [
    "telegram_bot_token",
    "anthropic_api_key",
    "bitbucket_email",
    "bitbucket_api_token",
    "teams_webhook_url",
    "havana_token",
]


def _expand(p: str | None) -> Path | None:
    return Path(p).expanduser() if p else None


@dataclass
class Repo:
    name: str
    provider: str = "bitbucket"
    remote: str = ""
    stack: str = ""
    setup: list[str] = field(default_factory=list)
    copy_files: list[str] = field(default_factory=list)
    test: str = ""
    ui: bool = False
    dev_url: str = ""

    @property
    def workspace(self) -> str:
        return self.remote.split("/")[0] if "/" in self.remote else ""

    @property
    def slug(self) -> str:
        return self.remote.split("/")[-1] if self.remote else self.name


class Config:
    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self.data = data
        self.path = path
        self.repos = [Repo(**r) for r in data.get("repos", [])]

    # generic dotted access: cfg.get("teams.pr_chat_topic")
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)

    @property
    def repos_root(self) -> Path:
        return _expand(self.get("workspace.repos_root", "~/Projects/work"))  # type: ignore

    @property
    def work_dir(self) -> Path:
        p = _expand(self.get("workspace.work_dir", str(HOME_DIR / "work")))
        p.mkdir(parents=True, exist_ok=True)  # type: ignore
        return p  # type: ignore

    @property
    def base_branch(self) -> str:
        return self.get("workspace.base_branch", "main")

    def model(self, role: str) -> str:
        return self.get(f"engine.models.{role}") or self.get("engine.models.implement")

    def effort(self, role: str) -> str | None:
        return self.get(f"engine.effort.{role}")

    def save(self) -> None:
        if self.path:
            self.path.write_text(yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True))


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Run ./install.sh or copy config.example.yaml there."
        )
    return Config(yaml.safe_load(path.read_text()) or {}, path)


def get_secret(name: str) -> str | None:
    """Keychain (via keyring) → env var DEVAGENT_<NAME> → env var <NAME>."""
    try:
        import keyring

        val = keyring.get_password(KEYRING_SERVICE, name)
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(f"DEVAGENT_{name.upper()}") or os.environ.get(name.upper())


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, name, value)
