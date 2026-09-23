"""Git operations: branch + worktree per task per repo, diffs, commit & push."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config, Repo

log = logging.getLogger("devagent.git")


class GitError(RuntimeError):
    pass


@dataclass
class Proc:
    code: int
    out: str
    err: str


async def sh(cmd: list[str] | str, cwd: Path | None = None, check: bool = True,
             timeout: float = 1800, env: dict[str, str] | None = None) -> Proc:
    import os

    full_env = {**os.environ, **(env or {})}
    if isinstance(cmd, str):
        p = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, env=full_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    else:
        p = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=full_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise GitError(f"timeout after {timeout}s: {cmd}")
    res = Proc(p.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
    if check and res.code != 0:
        raise GitError(f"{cmd} (in {cwd}) failed [{res.code}]: {res.err.strip() or res.out.strip()}")
    return res


def slugify(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "task"


JUNK = """# never commit these, even if a repo's .gitignore forgets them
.DS_Store
__pycache__/
*.pyc
.pytest_cache/
node_modules/
playwright-report/
test-results/
blob-report/
.phpunit.result.cache
.phpunit.cache/
*.log
.claude/settings.local.json
"""


class GitOps:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exclude_file = cfg.work_dir / ".devagent-gitexclude"
        self.exclude_file.write_text(JUNK)

    def _x(self) -> list[str]:
        return ["-c", f"core.excludesFile={self.exclude_file}"]

    # ── paths ──
    def clone_path(self, repo: Repo) -> Path:
        return self.cfg.repos_root / repo.name

    def task_dir(self, task: dict) -> Path:
        return self.cfg.work_dir / f"{task['id']}-{slugify(task['title'], 30)}"

    def branch_for(self, task: dict) -> str:
        prefix = self.cfg.get("workspace.branch_prefix", "feature/")
        return f"{prefix}{slugify(task['title'])}"

    def _identity(self) -> list[str]:
        name, email = self.cfg.get("git.author_name"), self.cfg.get("git.author_email")
        args: list[str] = []
        if name:
            args += ["-c", f"user.name={name}"]
        if email:
            args += ["-c", f"user.email={email}"]
        return args

    # ── prepare ──
    async def prepare_repo(self, task: dict, repo: Repo, branch: str) -> Path:
        clone = self.clone_path(repo)
        if not (clone / ".git").exists():
            raise GitError(f"Repo clone not found at {clone}. Clone it there or fix workspace.repos_root.")
        base = self.cfg.base_branch
        wt = self.task_dir(task) / repo.name
        # Step 1+2: fetch, and pull main into your clone when it's safe to do so
        await sh(["git", "fetch", "origin", "--prune"], cwd=clone)
        if self.cfg.get("workspace.pull_main_in_clone", True):
            cur = (await sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone)).out.strip()
            dirty = (await sh(["git", "status", "--porcelain"], cwd=clone)).out.strip()
            if cur == base and not dirty:
                await sh(["git", "pull", "--ff-only", "origin", base], cwd=clone)
            else:
                log.info("%s: clone on %s%s — skipped pull, branching from origin/%s",
                         repo.name, cur, " (dirty)" if dirty else "", base)
        if wt.exists():
            return wt  # resuming an existing task
        wt.parent.mkdir(parents=True, exist_ok=True)
        # Step 1: new branch from origin/main, in its own worktree
        exists = (await sh(["git", "rev-parse", "--verify", "--quiet", branch], cwd=clone, check=False)).code == 0
        if exists:
            await sh(["git", "worktree", "add", str(wt), branch], cwd=clone)
        else:
            await sh(["git", "worktree", "add", "-b", branch, str(wt), f"origin/{base}"], cwd=clone)
        for f in repo.copy_files:
            src = clone / f
            if src.exists():
                (wt / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, wt / f)
        for cmd in repo.setup:
            r = await sh(cmd, cwd=wt, check=False, timeout=1800)
            if r.code != 0:
                log.warning("%s setup '%s' failed: %s", repo.name, cmd, r.err[-500:])
        return wt

    async def prepare(self, task: dict, repo_names: list[str]) -> tuple[Path, str, dict[str, Path]]:
        branch = task.get("branch") or self.branch_for(task)
        paths: dict[str, Path] = {}
        for name in repo_names:
            repo = self.cfg.repo(name)
            if not repo:
                raise GitError(f"Unknown repo '{name}' (add it to config.yaml)")
            paths[name] = await self.prepare_repo(task, repo, branch)
        return self.task_dir(task), branch, paths

    # ── inspect ──
    async def changed(self, wt: Path) -> bool:
        return bool((await sh(["git", *self._x(), "status", "--porcelain"], cwd=wt)).out.strip())

    async def diff(self, wt: Path) -> tuple[str, str]:
        """(stat, patch) against the base, including new untracked files."""
        await sh(["git", *self._x(), "add", "-N", "."], cwd=wt, check=False)
        base = f"origin/{self.cfg.base_branch}"
        stat = (await sh(["git", "diff", "--stat", base], cwd=wt, check=False)).out
        patch = (await sh(["git", "diff", base], cwd=wt, check=False)).out
        return stat, patch

    # ── publish ──
    async def commit_and_push(self, wt: Path, branch: str, message: str) -> bool:
        if branch in self.cfg.get("git.never_push_branches", ["main", "master"]):
            raise GitError(f"Refusing to push protected branch {branch}")
        cur = (await sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)).out.strip()
        if cur != branch:
            raise GitError(f"{wt} is on {cur}, expected {branch}")
        if await self.changed(wt):
            await sh(["git", *self._x(), "add", "-A"], cwd=wt)
            trailer = self.cfg.get("git.commit_trailer")
            msg = f"{message}\n\n{trailer}" if trailer else message
            await sh(["git", *self._identity(), "commit", "-m", msg], cwd=wt)
        ahead = (await sh(["git", "rev-list", "--count", f"origin/{self.cfg.base_branch}..HEAD"],
                          cwd=wt)).out.strip()
        if ahead == "0":
            return False  # nothing to publish in this repo
        await sh(["git", "push", "-u", "origin", branch], cwd=wt)
        return True

    async def remove_worktrees(self, task: dict) -> None:
        tdir = self.task_dir(task)
        for name in task.get("repos") or []:
            repo = self.cfg.repo(name)
            if repo and (tdir / name).exists():
                await sh(["git", "worktree", "remove", "--force", str(tdir / name)],
                         cwd=self.clone_path(repo), check=False)
