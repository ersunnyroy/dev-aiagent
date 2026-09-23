"""Offline tests: guardrails, store, git worktree flow, full pipeline with a fake Claude engine."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from devagent import store as S
from devagent.config import Config
from devagent.engine import RunResult, check_bash, check_write
from devagent.gitops import GitOps
from devagent.notify import ConsoleNotifier
from devagent.pipeline import Pipeline


# ─────────────── guardrails ───────────────
@pytest.mark.parametrize("cmd", [
    "git push origin feature/x", "git commit -m wip", "git -C viam commit -m x", "git -c a=b push",
    "cd viam && git --no-pager commit -am x", "git merge main", "git reset --hard HEAD~1",
    "git checkout main", "sudo rm x", "php artisan migrate:fresh", "rm -rf ~",
    "mysql -e 'DROP DATABASE prod'", "cat ~/.ssh/id_rsa", "gh pr merge 12",
    "curl -X POST https://api.bitbucket.org/2.0/x"])
def test_blocked_commands(cmd):
    assert check_bash(cmd)


@pytest.mark.parametrize("cmd", [
    "php artisan test", "npx playwright test", "git diff origin/main", "git status",
    "php artisan migrate:fresh --env=testing", "composer install", "npm run build", "rm -rf node_modules"])
def test_allowed_commands(cmd):
    assert check_bash(cmd) is None


def test_write_guard(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    assert check_write(str(root / "app/Foo.php"), [root]) is None
    assert check_write(str(root / ".env.testing"), [root]) is None
    assert check_write(str(root / ".env"), [root])
    assert check_write(str(root / "api/.env.production"), [root])
    assert check_write(str(tmp_path / "elsewhere.php"), [root])


# ─────────────── fixtures ───────────────
def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def world(tmp_path):
    """A bare 'origin', a clone under repos_root, and a config pointing at them."""
    origin = tmp_path / "origin" / "viam.git"
    origin.mkdir(parents=True)
    git("init", "--bare", "-b", "main", cwd=origin)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-b", "main", cwd=seed)
    (seed / "README.md").write_text("hello\n")
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "origin", "main", cwd=seed)
    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    git("clone", str(origin), "viam", cwd=repos_root)
    cfg = Config({
        "workspace": {"repos_root": str(repos_root), "work_dir": str(tmp_path / "work"),
                      "branch_prefix": "feature/", "base_branch": "main"},
        "repos": [{"name": "viam", "provider": "bitbucket", "remote": "qualibar/viam",
                   "stack": "Laravel API", "test": "test -f LOGIN.md"}],
        "git": {"author_name": "Suni", "author_email": "suni@example.com"},
        "teams": {"pr_reviewers": ["Puneet Chelani", "Virendra Pratap Singh", "Mithun M P"],
                  "confirm_before_post": True, "pr_chat_topic": "v4-app"},
        "engine": {"models": {"implement": "sonnet", "review": "opus", "planner": "sonnet"}},
    })
    return cfg, origin, repos_root, tmp_path


async def test_worktree_branch_commit_push(world):
    cfg, origin, repos_root, tmp = world
    g = GitOps(cfg)
    task = {"id": 7, "title": "Login with foreign_id and password"}
    tdir, branch, paths = await g.prepare(task, ["viam"])
    assert branch == "feature/login-with-foreign-id-and-password"
    wt = paths["viam"]
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt,
                          capture_output=True, text=True).stdout.strip()
    assert head == branch
    (wt / "LOGIN.md").write_text("feature\n")
    stat, patch = await g.diff(wt)
    assert "LOGIN.md" in stat and "+feature" in patch
    assert await g.commit_and_push(wt, branch, "feat: login") is True
    remote_branches = subprocess.run(["git", "branch"], cwd=origin, capture_output=True, text=True).stdout
    assert "login-with-foreign-id" in remote_branches
    # clone's own checkout stays on main and untouched
    assert not (repos_root / "viam" / "LOGIN.md").exists()


async def test_refuses_to_push_main(world):
    cfg, *_ = world
    with pytest.raises(Exception):
        await GitOps(cfg).commit_and_push(cfg.repos_root / "viam", "main", "x")


# ─────────────── fake engine ───────────────
class FakeEngine:
    def __init__(self):
        self.calls: list[str] = []
        self.review_round = 0

    async def run_agent(self, *, role, prompt, cwd, schema=None, **kw):
        self.calls.append(role)
        if schema and "questions" in schema["properties"]:
            return RunResult(ok=True, data={
                "repos": ["viam", "not-a-repo"], "complexity": "M", "change_type": "feat", "ui": False,
                "pr_topic": "allow the users to login using foreign_id and password",
                "plan": ["Add foreign_id login"], "test_plan": ["feature test"],
                "questions": [{"kind": "business", "text": "Case-sensitive foreign_id?",
                               "options": ["Yes", "No"]}]})
        if schema and "verdict" in schema["properties"]:
            self.review_round += 1
            if self.review_round == 1:
                return RunResult(ok=True, data={"verdict": "changes_needed", "summary": "missing test",
                                                "issues": [{"severity": "major", "problem": "no test"}]})
            return RunResult(ok=True, data={"verdict": "approve", "summary": "lgtm", "issues": []})
        (Path(cwd) / "viam" / "LOGIN.md").write_text("done\n")
        return RunResult(ok=True, text="Implemented + tested", session_id="sess-1")

    async def ask_json(self, *a, **k):
        raise AssertionError("not used")


class FakePRs:
    def __init__(self):
        self.n = 248

    async def create(self, repo, branch, title, description, cwd=None):
        self.n += 1
        return f"https://bitbucket.org/{repo.remote}/pull-requests/{self.n}/diff"


async def test_full_pipeline(world):
    cfg, origin, repos_root, tmp = world
    st = S.Store(tmp / "state.db")
    note = ConsoleNotifier()
    eng = FakeEngine()
    p = Pipeline(cfg, st, eng, GitOps(cfg), note, prs=FakePRs())

    tid = p.add_task("Login with foreign_id and password", "Users can log in with foreign_id")
    await p.analyze(st.task(tid))
    t = st.task(tid)
    assert t["status"] == S.NEEDS_INFO and t["repos"] == ["viam"]
    q = st.open_questions()[0]
    await p.answer(q["id"], "No")
    assert st.task(tid)["status"] == S.READY

    await p.implement(st.task(tid))
    t = st.task(tid)
    assert t["status"] == S.AWAITING_APPROVAL, t["error"]
    # implement → review1 (changes) → fix → review2 (approve)
    assert eng.calls == ["planner", "implement", "review", "implement", "review"]
    assert ("review", tid) in note.sent

    await p.approve(tid)
    t = st.task(tid)
    assert t["status"] == S.PR_OPEN
    assert t["pr_links"][0]["url"].endswith("/pull-requests/249/diff")
    confirm = [s for s in note.sent if s[0] == "confirm"]
    assert confirm and confirm[0][1] == f"teams:{tid}"

    msg = p.teams_message([tid])
    assert msg.startswith("Hi\nPuneet Chelani Virendra Pratap Singh Mithun M P\n")
    assert "related to allow the users to login using foreign_id and password :" in msg
    assert "1. https://bitbucket.org/qualibar/viam/pull-requests/249/diff" in msg
    html = p.teams_message([tid], html=True)
    assert '<at id="0">Puneet Chelani</at>' in html


async def test_changes_requested_loop(world):
    cfg, origin, repos_root, tmp = world
    st = S.Store(tmp / "state.db")
    eng = FakeEngine()
    eng.review_round = 5  # reviewers approve straight away
    p = Pipeline(cfg, st, eng, GitOps(cfg), ConsoleNotifier(), prs=FakePRs())
    tid = st.add_task("Fix export", status=S.READY, repos=["viam"],
                      analysis={"plan": [], "test_plan": [], "complexity": "S"})
    await p.implement(st.task(tid))
    assert st.task(tid)["status"] == S.AWAITING_APPROVAL
    await p.request_changes(tid, "Rename the column to policy_no")
    assert st.task(tid)["status"] == S.CHANGES_REQUESTED
    eng.calls.clear()
    await p.implement(st.task(tid))
    assert eng.calls == ["implement", "review"]      # one review pass after feedback
    assert st.task(tid)["status"] == S.AWAITING_APPROVAL


def test_claim_is_atomic(tmp_path):
    st = S.Store(tmp_path / "s.db")
    tid = st.add_task("x", status=S.NEW)
    assert st.claim(tid, S.NEW, S.ANALYZING)
    assert not st.claim(tid, S.NEW, S.ANALYZING)


async def test_junk_files_not_committed(world):
    cfg, origin, repos_root, tmp = world
    g = GitOps(cfg)
    task = {"id": 9, "title": "Junk check"}
    _, branch, paths = await g.prepare(task, ["viam"])
    wt = paths["viam"]
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (wt / "app.php").write_text("<?php\n")
    await g.commit_and_push(wt, branch, "feat: junk")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=wt,
                           capture_output=True, text=True).stdout.split()
    assert files == ["app.php"]
