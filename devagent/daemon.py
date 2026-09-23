"""The always-on daemon: wires everything together and runs the loops concurrently."""
from __future__ import annotations

import asyncio
import json
import logging
import signal

from . import store as S
from .config import HOME_DIR, Config, get_secret
from .engine import Engine
from .gitops import GitOps
from .meetings import Meetings
from .notify import ConsoleNotifier
from .pipeline import Pipeline

log = logging.getLogger("devagent")
CONTROL_PORT = 8765


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = S.Store(HOME_DIR / "state.db")
        self.engine = Engine(cfg, self.store)
        self.git = GitOps(cfg)
        token = get_secret("telegram_bot_token")
        if token:
            from .bridges.telegram_bot import TelegramBridge

            self.notifier = TelegramBridge(cfg, self.store, token)
        else:
            log.warning("No telegram_bot_token — notifications go to the log only")
            self.notifier = ConsoleNotifier()
        from .bridges.prs import PRRouter

        self.teams = self.teams_auth = None
        if cfg.get("teams.enabled"):
            from .bridges.teams import GraphAuth, Teams

            self.teams_auth = GraphAuth(cfg)
            self.teams = Teams(cfg, self.teams_auth)
        self.havana = None
        if cfg.get("havana.enabled"):
            from .bridges.havana import Havana

            self.havana = Havana(cfg)
        self.pipeline = Pipeline(cfg, self.store, self.engine, self.git, self.notifier,
                                 prs=PRRouter(cfg), teams=self.teams, havana=self.havana)
        self.meetings = Meetings(cfg, self.store, self.engine, self.notifier, self.pipeline)
        if hasattr(self.notifier, "attach"):
            self.notifier.attach(self.pipeline, self.meetings, self.teams, self.teams_auth)

    async def control_server(self) -> None:
        """Tiny localhost API so a hotkey / Shortcut can do: curl localhost:8765/rec/start"""
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            line = (await reader.readline()).decode(errors="ignore")
            parts = line.split()
            path = parts[1] if len(parts) > 1 else "/"
            if path.startswith("/rec/start"):
                title = path.partition("?title=")[2].replace("+", " ") or "Call"
                body = await self.meetings.start("adhoc", title)
            elif path.startswith("/rec/stop"):
                body = await self.meetings.stop()
            elif path.startswith("/status"):
                body = json.dumps({"recording": self.meetings.rec.recording,
                                   "tasks": [{k: t[k] for k in ("id", "title", "status")}
                                             for t in self.store.active_tasks()]})
            else:
                body = "dev-agent: /rec/start?title=X  /rec/stop  /status"
            data = body.encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                         + str(len(data)).encode() + b"\r\nConnection: close\r\n\r\n" + data)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", CONTROL_PORT)
        async with server:
            await server.serve_forever()

    async def havana_loop(self) -> None:
        while True:
            try:
                n = await self.havana.sync_into(self.pipeline)
                if n:
                    await self.notifier.text(f"📥 {n} new task(s) from Havana")
            except NotImplementedError:
                return
            except Exception:  # noqa: BLE001
                log.exception("havana sync failed")
            await asyncio.sleep(300)

    async def run(self) -> None:
        if hasattr(self.notifier, "start"):
            await self.notifier.start()
        loops = [self.pipeline.run_forever(), self.meetings.run_forever(), self.control_server()]
        if self.teams and self.cfg.get("teams.triage.enabled", True):
            from .bridges.teams import TeamsWatcher

            loops.append(TeamsWatcher(self.cfg, self.teams, self.engine, self.store, self.notifier,
                                      self.pipeline).run_forever())
        if self.havana:
            loops.append(self.havana_loop())
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        tasks = [asyncio.create_task(c) for c in loops]
        log.info("dev-agent running (%d loops)", len(tasks))
        await stop.wait()
        log.info("shutting down…")
        if self.meetings.rec.recording:
            await self.meetings.stop()
        for t in tasks:
            t.cancel()
        if hasattr(self.notifier, "stop"):
            await self.notifier.stop()
