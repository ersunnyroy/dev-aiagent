"""Offline tests for Teams formatting, Bitbucket PR API calls, meetings and call detection."""
from __future__ import annotations

import json

import httpx
import pytest

from devagent import store as S
from devagent.bridges.prs import BitbucketPRs
from devagent.bridges.teams import to_teams_html
from devagent.capture.recorder import _apps_from_assertions
from devagent.config import Config, Repo
from devagent.engine import RunResult
from devagent.meetings import Meetings
from devagent.notify import ConsoleNotifier


def test_teams_html_keeps_mentions_and_escapes_rest():
    out = to_teams_html('Hi\n<at id="0">Puneet Chelani</at> <script>x</script>')
    assert '<at id="0">Puneet Chelani</at>' in out
    assert "&lt;script&gt;" in out and "<br>" in out


async def test_bitbucket_create_pr(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(201, json={"links": {"html": {
            "href": "https://bitbucket.org/qualibar/viam/pull-requests/249"}}})

    monkeypatch.setenv("DEVAGENT_BITBUCKET_API_TOKEN", "tok")
    monkeypatch.setenv("DEVAGENT_BITBUCKET_EMAIL", "me@x.com")
    monkeypatch.setattr("devagent.config.get_secret",
                        lambda n: {"bitbucket_api_token": "tok", "bitbucket_email": "me@x.com"}.get(n))
    monkeypatch.setattr("devagent.bridges.prs.get_secret",
                        lambda n: {"bitbucket_api_token": "tok", "bitbucket_email": "me@x.com"}.get(n))
    cfg = Config({"workspace": {"base_branch": "main"}})
    bb = BitbucketPRs(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    url = await bb.create(Repo(name="viam", remote="qualibar/viam"), "feature/x", "Title", "Body")
    assert url == "https://bitbucket.org/qualibar/viam/pull-requests/249/diff"
    assert seen["url"].endswith("/repositories/qualibar/viam/pullrequests")
    assert seen["body"]["source"]["branch"]["name"] == "feature/x"
    assert seen["body"]["destination"]["branch"]["name"] == "main"
    assert seen["auth"].startswith("Basic ")


async def test_call_detection_parsing(monkeypatch):
    sample = """
   pid 312(coreaudiod): [0x00001] 00:05:10 PreventUserIdleSleep named: "com.apple.audio.BuiltInMic.context.preventuseridlesleep"
        Created for PID: 4242.
   pid 4242(MSTeams): [0x00002] 00:05:00 PreventUserIdleDisplaySleep named: "Microsoft Teams Call in progress"
"""

    class P:
        out = "4242 /Applications/Microsoft Teams.app/Contents/MacOS/MSTeams\n"

    async def fake_sh(*a, **k):
        return P()

    monkeypatch.setattr("devagent.capture.recorder.sh", fake_sh)
    assert await _apps_from_assertions(sample, ["MSTeams", "zoom.us"]) == ["MSTeams"]
    assert await _apps_from_assertions("nothing here", ["MSTeams"]) == []


class FakeEngine:
    def __init__(self, data):
        self.data = data

    async def ask_json(self, role, system, prompt, schema, task_id=None):
        assert "TRANSCRIPT" in prompt
        return RunResult(ok=True, data=self.data)


class FakePipeline:
    def __init__(self, store):
        self.store = store

    def add_task(self, title, description="", source="", priority=3, **kw):
        return self.store.add_task(title, description=description, source=source,
                                   priority=priority, status=S.NEW, **kw)


async def test_priorities_meeting_to_tasks(tmp_path):
    cfg = Config({"meetings": {"recordings_dir": str(tmp_path)}, "repos": [{"name": "viam"}]})
    st = S.Store(tmp_path / "s.db")
    minutes = {
        "summary": ["Sandip set priorities"], "decisions": ["Ship login today"],
        "action_items": [{"owner": "Suni", "item": "foreign_id login", "priority": 1}],
        "suni_tasks": [{"title": "Login via foreign_id", "description": "…", "priority": 1, "repos": ["viam"]},
                       {"title": "Fix CSV export", "description": "…", "priority": 2}],
        "open_questions": ["Case-sensitive?"],
        "day_plan": [{"time": "09:00", "task": "Login via foreign_id", "goal": "API done"}]}
    note = ConsoleNotifier()
    m = Meetings(cfg, st, FakeEngine(minutes), note, FakePipeline(st))
    mid = st.add_meeting("priorities", "8 AM priorities", "")
    transcript = "Sandip: Suni aaj foreign id login wala kaam priority pe karo, then CSV export fix. " * 3
    await m.process(mid, transcript=transcript)
    assert st.meeting(mid)["status"] == "done"
    assert any(k == "confirm" and v == f"meet:{mid}" for k, v in note.sent)
    assert any(k == "file" for k, _ in note.sent)
    ids = m.apply_meeting_tasks(mid, picks=[1])
    assert [st.task(i)["title"] for i in ids] == ["Login via foreign_id"]


async def test_standup_changes(tmp_path):
    cfg = Config({"meetings": {"recordings_dir": str(tmp_path)}})
    st = S.Store(tmp_path / "s.db")
    t1 = st.add_task("Login via foreign_id", status=S.READY, priority=2)
    t2 = st.add_task("Old thing", status=S.READY)
    diff = {"summary": "Sangita moved login up", "say_in_standup": ["Working on login"],
            "changes": [{"op": "reprioritize", "task_id": t1, "priority": 1},
                        {"op": "cancel", "task_id": t2, "reason": "dropped"},
                        {"op": "add", "title": "Add audit log", "priority": 3}]}
    m = Meetings(cfg, st, FakeEngine(diff), ConsoleNotifier(), FakePipeline(st))
    mid = st.add_meeting("standup", "Standup", "")
    await m.process(mid, transcript="Sangita: Suni login ko top priority do, old thing drop karo, add audit log " * 2)
    assert m.apply_standup(mid) == 3
    assert st.task(t1)["priority"] == 1 and st.task(t2)["status"] == S.CANCELLED
    assert any(t["title"] == "Add audit log" for t in st.tasks())
