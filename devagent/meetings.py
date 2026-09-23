"""Meeting flow: record → transcribe (Whisper, Hindi/English) → minutes + day plan / standup diff
→ proposals on your phone → tasks. Also the auto-record scheduler.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader

from . import store as S
from .capture.recorder import Recorder, RecorderError, active_call_apps, transcribe
from .config import Config

log = logging.getLogger("devagent.meetings")
jinja = Environment(loader=PackageLoader("devagent", "templates"))


def _p(name: str) -> str:
    return resources.files("devagent").joinpath(f"prompts/{name}.md").read_text()


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


MINUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {"type": "object", "properties": {
            "owner": {"type": "string"}, "item": {"type": "string"},
            "priority": {"type": "integer"}, "due": {"type": "string"}},
            "required": ["owner", "item", "priority"]}},
        "suni_tasks": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "description": {"type": "string"},
            "priority": {"type": "integer"}, "repos": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "description", "priority"]}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "day_plan": {"type": "array", "items": {"type": "object", "properties": {
            "time": {"type": "string"}, "task": {"type": "string"}, "goal": {"type": "string"}},
            "required": ["time", "task", "goal"]}},
    },
    "required": ["summary", "decisions", "action_items", "suni_tasks", "open_questions", "day_plan"],
}

STANDUP_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "say_in_standup": {"type": "array", "items": {"type": "string"}},
        "changes": {"type": "array", "items": {"type": "object", "properties": {
            "op": {"type": "string", "enum": ["add", "reprioritize", "update", "cancel"]},
            "task_id": {"type": "integer"}, "title": {"type": "string"},
            "description": {"type": "string"}, "priority": {"type": "integer"},
            "note": {"type": "string"}, "reason": {"type": "string"}}, "required": ["op"]}},
    },
    "required": ["summary", "say_in_standup", "changes"],
}


class Meetings:
    def __init__(self, cfg: Config, store: S.Store, engine, notifier, pipeline):
        self.cfg, self.store, self.engine = cfg, store, engine
        self.notify, self.pipeline = notifier, pipeline
        self.rec = Recorder(cfg)
        self.meeting_id: int | None = None
        self.auto_started = False
        self.tz = ZoneInfo(cfg.get("me.timezone", "Asia/Kolkata"))

    # ── manual control (Telegram /rec, CLI) ──
    async def start(self, kind: str = "adhoc", title: str = "", auto: bool = False) -> str:
        if self.rec.recording:
            return "Already recording."
        try:
            path = await self.rec.start(kind, title)
        except RecorderError as e:
            await self.notify.text(f"❌ {esc(e)}")
            return str(e)
        self.meeting_id = self.store.add_meeting(kind, title or kind, str(path))
        self.auto_started = auto
        await self.notify.text(f"🔴 Recording{' (auto)' if auto else ''}: {esc(title or kind)}")
        return "Recording started."

    async def stop(self) -> str:
        path, meta = await self.rec.stop()
        if not path:
            return "Not recording."
        mid = self.meeting_id
        self.meeting_id, self.auto_started = None, False
        self.store.update_meeting(mid, ended_at=S.now(), status="transcribing")
        await self.notify.text("⏹ Recording stopped — transcribing…")
        asyncio.create_task(self._process_safe(mid))
        return "Stopped; processing."

    async def _process_safe(self, mid: int) -> None:
        try:
            await self.process(mid)
        except Exception as e:  # noqa: BLE001
            log.exception("meeting processing failed")
            self.store.update_meeting(mid, status="failed")
            await self.notify.text(f"❌ Meeting processing failed: {esc(str(e)[:300])}")

    # ── processing ──
    async def process(self, mid: int, transcript: str | None = None) -> None:
        m = self.store.meeting(mid)
        wav = Path(m["audio_path"]) if m.get("audio_path") else None
        if transcript is None:
            transcript = await transcribe(self.cfg, wav)  # type: ignore[arg-type]
        self.store.update_meeting(mid, transcript=transcript, status="summarizing")
        if wav and wav.exists() and not self.cfg.get("meetings.keep_audio", False):
            wav.unlink()
        if len(transcript.split()) < 15:
            self.store.update_meeting(mid, status="done")
            await self.notify.text("ℹ️ Recording had almost no speech — nothing to summarize.")
            return
        if m["kind"] == "standup":
            await self._standup(mid, transcript)
        else:
            await self._minutes(mid, m, transcript)

    async def _minutes(self, mid: int, m: dict, transcript: str) -> None:
        tasks = "\n".join(f"#{t['id']} P{t['priority']} {t['title']} [{t['status']}]"
                          for t in self.store.active_tasks()) or "(none)"
        repos = ", ".join(r.name for r in self.cfg.repos)
        r = await self.engine.ask_json(
            "planner", _p("minutes"),
            f"Meeting: {m['title']} ({m['kind']}) on {datetime.now(self.tz):%A %d %b %Y}\n"
            f"Known repos: {repos}\nSuni's open tasks already tracked:\n{tasks}\n\n"
            f"TRANSCRIPT:\n{transcript[:150_000]}", MINUTES_SCHEMA)
        if not r.data:
            raise RuntimeError(f"minutes generation failed: {r.text[:200]}")
        d = r.data
        md = jinja.get_template("mom.md.j2").render(title=m["title"], date=f"{datetime.now(self.tz):%d %b %Y}", m=d)
        out = Path(self.cfg.get("meetings.recordings_dir", "~/.devagent/recordings")).expanduser() / \
            f"MoM-{datetime.now(self.tz):%Y%m%d-%H%M}-{m['kind']}.md"
        out.write_text(md)
        (out.with_suffix(".transcript.txt")).write_text(transcript)
        self.store.update_meeting(mid, minutes=md, plan=d, status="done")
        summary = "\n".join(f"• {esc(s)}" for s in d["summary"][:6])
        plan = "\n".join(f"<code>{esc(b['time'])}</code> {esc(b['task'])}" for b in d["day_plan"][:10])
        await self.notify.text(f"📝 <b>MoM — {esc(m['title'])}</b>\n{summary}\n\n<b>Day plan</b>\n{plan}")
        await self.notify.file(out, "Minutes of meeting")
        if d["suni_tasks"]:
            self.store.kv_set(f"proposal:{mid}", json.dumps(d["suni_tasks"]))
            lst = "\n".join(f"{i}. P{t['priority']} <b>{esc(t['title'])}</b>"
                            for i, t in enumerate(d["suni_tasks"], 1))
            qs = "\n".join(f"❓ {esc(q)}" for q in d["open_questions"][:5])
            await self.notify.confirm(
                f"meet:{mid}", f"🗂 Tasks for you from this meeting:\n{lst}\n{qs}",
                [("Add all & start ▶️", "all"), ("Pick by number", "pick"), ("Ignore", "none")])

    async def _standup(self, mid: int, transcript: str) -> None:
        tasks = "\n".join(f"#{t['id']} P{t['priority']} {t['title']} [{t['status']}]"
                          for t in self.store.active_tasks()) or "(none)"
        r = await self.engine.ask_json("planner", _p("standup"),
                                       f"CURRENT TASKS:\n{tasks}\n\nSTANDUP TRANSCRIPT:\n{transcript[:100_000]}",
                                       STANDUP_SCHEMA)
        if not r.data:
            raise RuntimeError("standup analysis failed")
        d = r.data
        self.store.update_meeting(mid, minutes=d["summary"], plan=d, status="done")
        ch = d["changes"]
        self.store.kv_set(f"standup:{mid}", json.dumps(ch))
        lines = []
        for c in ch:
            if c["op"] == "add":
                lines.append(f"➕ {esc(c.get('title'))} (P{c.get('priority', 3)})")
            elif c["op"] == "reprioritize":
                lines.append(f"↕️ #{c.get('task_id')} → P{c.get('priority')}")
            elif c["op"] == "update":
                lines.append(f"✏️ #{c.get('task_id')}: {esc(c.get('note'))}")
            elif c["op"] == "cancel":
                lines.append(f"✖️ #{c.get('task_id')}: {esc(c.get('reason'))}")
        await self.notify.text(f"🧍 <b>Standup</b>\n{esc(d['summary'])}")
        if ch:
            await self.notify.confirm(f"stand:{mid}", "Proposed task-list changes:\n" + "\n".join(lines),
                                      [("Apply ✅", "apply"), ("Ignore", "none")])

    def apply_meeting_tasks(self, mid: int, picks: list[int] | None = None) -> list[int]:
        items = json.loads(self.store.kv_get(f"proposal:{mid}") or "[]")
        ids = []
        for i, t in enumerate(items, 1):
            if picks and i not in picks:
                continue
            desc = t["description"] + (f"\n\nLikely repos: {', '.join(t['repos'])}" if t.get("repos") else "")
            ids.append(self.pipeline.add_task(t["title"], desc, source="meeting",
                                              priority=int(t.get("priority", 3)), meeting_id=mid))
        self.store.kv_set(f"proposal:{mid}", "[]")
        return ids

    def apply_standup(self, mid: int) -> int:
        n = 0
        for c in json.loads(self.store.kv_get(f"standup:{mid}") or "[]"):
            tid = c.get("task_id")
            t = self.store.task(tid) if tid else None
            if c["op"] == "add":
                self.pipeline.add_task(c.get("title", "Untitled"), c.get("description", ""), source="meeting",
                                       priority=int(c.get("priority", 3)), meeting_id=mid)
            elif t and c["op"] == "reprioritize":
                self.store.update_task(tid, priority=int(c.get("priority", t["priority"])))
            elif t and c["op"] == "update":
                self.store.update_task(tid, description=(t["description"] + "\n\nUpdate from standup: "
                                                         + c.get("note", "")).strip())
            elif t and c["op"] == "cancel" and t["status"] in S.ACTIVE_STATES:
                self.store.update_task(tid, status=S.CANCELLED, error=c.get("reason", ""))
            else:
                continue
            n += 1
        self.store.kv_set(f"standup:{mid}", "[]")
        return n

    # ── scheduler: arm windows for 8 AM / 1 PM and auto-record any call ──
    async def run_forever(self) -> None:
        apps = self.cfg.get("meetings.call_apps", ["MSTeams"])
        on_streak = off_streak = 0
        while True:
            try:
                window = self._current_window()
                in_call = bool(await active_call_apps(apps))
                on_streak = on_streak + 1 if in_call else 0
                off_streak = 0 if in_call else off_streak + 1
                if not self.rec.recording and on_streak >= 2 and (
                        window or self.cfg.get("meetings.auto_record_any_call", True)):
                    kind, title = (window["kind"], window["name"]) if window else ("adhoc", "Call")
                    await self.start(kind, title, auto=True)
                elif self.rec.recording and self.auto_started and off_streak >= 6:
                    await self.stop()  # ~60 s of silence from call apps
            except Exception:  # noqa: BLE001
                log.exception("meeting scheduler tick failed")
            await asyncio.sleep(10)

    def _current_window(self) -> dict | None:
        now = datetime.now(self.tz)
        if now.weekday() not in self.cfg.get("meetings.weekdays", [0, 1, 2, 3, 4]):
            return None
        for w in self.cfg.get("meetings.schedule", []):
            hh, mm = map(int, w["at"].split(":"))
            start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if start <= now <= start + timedelta(minutes=int(w.get("window_minutes", 45))):
                return w
        return None
