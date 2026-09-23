"""Notifier interface — how the agent talks to you. Telegram implements it; tests use a fake."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Notifier(Protocol):
    async def text(self, message: str, html: bool = True) -> None: ...

    async def question(self, q: dict, task: dict) -> int | None:
        """Send a clarifying question; returns the message id used to match replies."""

    async def review(self, task: dict, summary_html: str, screenshots: list[Path],
                     diff_file: Path | None) -> None: ...

    async def confirm(self, key: str, message: str, buttons: list[tuple[str, str]]) -> None:
        """Send a message with inline buttons; callback data = f"{key}|{value}"."""

    async def file(self, path: Path, caption: str = "") -> None: ...


class ConsoleNotifier:
    """Used when Telegram isn't configured (and in tests)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def text(self, message: str, html: bool = True) -> None:
        self.sent.append(("text", message))
        print(f"[notify] {message}")

    async def question(self, q: dict, task: dict) -> int | None:
        self.sent.append(("question", q))
        print(f"[question #{q['id']} task {task['id']}] {q['text']} {q.get('options')}")
        return None

    async def review(self, task, summary_html, screenshots, diff_file) -> None:
        self.sent.append(("review", task["id"]))
        print(f"[review task {task['id']}] {len(screenshots)} screenshots\n{summary_html}")

    async def confirm(self, key, message, buttons) -> None:
        self.sent.append(("confirm", key))
        print(f"[confirm {key}] {message} {buttons}")

    async def file(self, path, caption="") -> None:
        self.sent.append(("file", str(path)))
        print(f"[file] {path} {caption}")
