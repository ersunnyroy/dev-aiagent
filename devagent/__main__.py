"""CLI: devagent run | doctor | secrets | teams | task | transcribe | pr-message"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import CONFIG_PATH, HOME_DIR, SECRET_NAMES, get_secret, load_config, set_secret


def setup_logging(verbose: bool = False) -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(HOME_DIR / "devagent.log", maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler(sys.stderr)]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_run(args) -> None:
    from .daemon import Daemon

    asyncio.run(Daemon(load_config()).run())


def _ok(flag: bool, label: str, hint: str = "") -> bool:
    print(f"  {'✅' if flag else '❌'} {label}" + (f"  → {hint}" if hint and not flag else ""))
    return flag


def cmd_doctor(args) -> None:
    cfg = load_config()
    print("Tools")
    for tool, hint in [("git", "xcode-select --install"), ("ffmpeg", "brew install ffmpeg"),
                       (cfg.get("meetings.whisper.binary", "whisper-cli"), "brew install whisper-cpp"),
                       ("node", "brew install node"), ("php", "XAMPP php on PATH"),
                       ("composer", "brew install composer"), ("gh", "brew install gh (GitHub repos only)")]:
        _ok(bool(shutil.which(tool)), tool, hint)
    model = Path(cfg.get("meetings.whisper.model", "")).expanduser()
    _ok(model.exists(), f"whisper model {model.name}", "./scripts/get-whisper-model.sh")
    print("Claude")
    has_key = bool(get_secret("anthropic_api_key"))
    has_login = (Path.home() / ".claude").exists() or (Path.home() / ".claude.json").exists()
    _ok(has_key or has_login, "Claude auth (API key or `claude login`)",
        "devagent secrets set anthropic_api_key  — or run `claude login` once")
    print("Secrets")
    for s in SECRET_NAMES:
        optional = s in ("anthropic_api_key", "teams_webhook_url", "havana_token", "bitbucket_email")
        _ok(bool(get_secret(s)) or optional, s + (" (optional)" if optional else ""),
            f"devagent secrets set {s}")
    print("Repos")
    for r in cfg.repos:
        p = cfg.repos_root / r.name
        _ok((p / ".git").exists(), f"{r.name} ({r.provider}:{r.remote})", f"clone into {p}")
    print("Telegram")
    _ok(bool(cfg.get("telegram.allowed_chat_id")), "paired chat", "start the daemon and send /start to your bot")
    print("Audio")
    if sys.platform == "darwin" and shutil.which("ffmpeg"):
        out = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                             capture_output=True, text=True).stderr
        dev = cfg.get("meetings.audio_device")
        _ok(dev in out, f"input device '{dev}'", "create the Aggregate Device in Audio MIDI Setup (README)")
    print("Teams")
    if cfg.get("teams.enabled"):
        _ok((HOME_DIR / "msal_cache.bin").exists(), "signed in", "devagent teams login")


def cmd_secrets(args) -> None:
    if args.action == "list":
        for s in SECRET_NAMES:
            print(f"  {s}: {'set' if get_secret(s) else '-'}")
        return
    if args.name not in SECRET_NAMES:
        sys.exit(f"Unknown secret. One of: {', '.join(SECRET_NAMES)}")
    val = getpass.getpass(f"{args.name}: ")
    set_secret(args.name, val.strip())
    print("Saved to Keychain (service 'devagent').")


def cmd_teams(args) -> None:
    cfg = load_config()
    from .bridges.teams import GraphAuth, Teams

    auth = GraphAuth(cfg)
    if args.action == "login":
        name = auth.device_login(print)
        print(f"Signed in as {name}")
    elif args.action == "check":
        async def go():
            t = Teams(cfg, auth)
            me = await t.me()
            cid = await t.find_chat(cfg.get("teams.pr_chat_topic"))
            members = [m.get("displayName") for m in await t.members(cid)]
            print(f"Me: {me['displayName']}\nPR chat id: {cid}\nMembers: {', '.join(members)}")
            missing = [n for n in cfg.get("teams.pr_reviewers", []) if n not in members]
            if missing:
                print(f"⚠️ reviewers not found in chat (check spelling): {missing}")
        asyncio.run(go())


def cmd_task(args) -> None:
    from . import store as S

    st = S.Store(HOME_DIR / "state.db")
    tid = st.add_task(args.title, description=args.description or "", source="cli",
                      priority=args.priority, status=S.NEW)
    print(f"Task #{tid} queued (the running daemon picks it up within 30s).")


def cmd_transcribe(args) -> None:
    """Process an existing recording or transcript file (handy for testing the meeting flow)."""
    from .daemon import Daemon

    async def go():
        d = Daemon(load_config())
        if hasattr(d.notifier, "app"):
            await d.notifier.app.initialize()
        p = Path(args.file)
        mid = d.store.add_meeting(args.kind, args.title or args.kind, "" if p.suffix == ".txt" else str(p))
        await d.meetings.process(mid, transcript=p.read_text() if p.suffix == ".txt" else None)
        print(d.store.meeting(mid)["minutes"])
    asyncio.run(go())


def cmd_pr_message(args) -> None:
    from .daemon import Daemon

    d = Daemon(load_config())
    print(d.pipeline.teams_message(args.task_ids))


def main() -> None:
    ap = argparse.ArgumentParser(prog="devagent", description="Suni's personal AI dev agent")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the daemon").set_defaults(fn=cmd_run)
    sub.add_parser("doctor", help="check setup").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("secrets", help="store secrets in Keychain")
    s.add_argument("action", choices=["set", "list"])
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=cmd_secrets)
    t = sub.add_parser("teams", help="Teams sign-in / check")
    t.add_argument("action", choices=["login", "check"])
    t.set_defaults(fn=cmd_teams)
    k = sub.add_parser("task", help="add a task")
    k.add_argument("title")
    k.add_argument("-d", "--description")
    k.add_argument("-p", "--priority", type=int, default=3)
    k.set_defaults(fn=cmd_task)
    tr = sub.add_parser("transcribe", help="process a .wav or .txt transcript as a meeting")
    tr.add_argument("file")
    tr.add_argument("--kind", default="priorities", choices=["priorities", "standup", "adhoc"])
    tr.add_argument("--title", default="")
    tr.set_defaults(fn=cmd_transcribe)
    pm = sub.add_parser("pr-message", help="preview the Teams PR message for task ids")
    pm.add_argument("task_ids", type=int, nargs="+")
    pm.set_defaults(fn=cmd_pr_message)
    args = ap.parse_args()
    setup_logging(args.verbose)
    if args.cmd not in ("secrets",) and not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH}. Run ./install.sh first.")
    args.fn(args)


if __name__ == "__main__":
    main()
