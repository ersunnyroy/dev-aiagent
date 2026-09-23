"""Task pipeline: analyze → clarify → implement → test → 2× review → your approval → PR → Teams."""
from __future__ import annotations

import asyncio
import logging
from importlib import resources
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from . import store as S
from .config import Config
from .engine import Engine
from .gitops import GitError, GitOps, sh
from .notify import Notifier

log = logging.getLogger("devagent.pipeline")

jinja = Environment(loader=PackageLoader("devagent", "templates"),
                    autoescape=select_autoescape(enabled_extensions=("html",)),
                    trim_blocks=False, keep_trailing_newline=True)


def prompt(name: str) -> str:
    return resources.files("devagent").joinpath(f"prompts/{name}.md").read_text()


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repos": {"type": "array", "items": {"type": "string"}},
        "complexity": {"type": "string", "enum": ["S", "M", "L"]},
        "change_type": {"type": "string", "enum": ["feat", "fix", "refactor", "chore", "perf", "test"]},
        "ui": {"type": "boolean"},
        "pr_topic": {"type": "string", "description": "phrase completing 'PRs related to …', e.g. 'allow users to login using foreign_id and password'"},
        "plan": {"type": "array", "items": {"type": "string"}},
        "test_plan": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["business", "technical"]},
                           "text": {"type": "string"},
                           "options": {"type": "array", "items": {"type": "string"}}},
            "required": ["kind", "text"]}},
    },
    "required": ["repos", "complexity", "change_type", "ui", "pr_topic", "plan", "test_plan", "questions"],
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "changes_needed"]},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"severity": {"type": "string", "enum": ["blocking", "major", "minor", "nit"]},
                           "repo": {"type": "string"}, "file": {"type": "string"},
                           "line": {"type": "integer"}, "problem": {"type": "string"},
                           "fix": {"type": "string"}},
            "required": ["severity", "problem"]}},
    },
    "required": ["verdict", "summary", "issues"],
}


class Pipeline:
    def __init__(self, cfg: Config, store: S.Store, engine: Engine, git: GitOps,
                 notifier: Notifier, prs=None, teams=None, havana=None):
        self.cfg, self.store, self.engine, self.git = cfg, store, engine, git
        self.notify = notifier
        self.prs = prs
        self.teams = teams
        self.havana = havana
        self._running: set[int] = set()
        self._wake = asyncio.Event()

    # ─────────────────────────── intake ───────────────────────────
    def add_task(self, title: str, description: str = "", source: str = "telegram",
                 priority: int = 3, **extra: Any) -> int:
        tid = self.store.add_task(title, description=description, source=source,
                                  priority=priority, status=S.NEW, **extra)
        self.poke()
        return tid

    def poke(self) -> None:
        self._wake.set()

    def _repo_catalog(self) -> str:
        lines = []
        for r in self.cfg.repos:
            lines.append(f"- {r.name}: {r.stack or r.provider}"
                         f"{' (UI, ' + r.dev_url + ')' if r.ui else ''} — test: {r.test or 'n/a'}")
        return "\n".join(lines)

    def _qa(self, task_id: int) -> str:
        qs = self.store.questions(task_id)
        return "\n".join(f"- [{q['kind']}] Q: {q['text']}\n  A (Suni): {q['answer']}"
                         for q in qs if q.get("answer")) or "(none)"

    # ─────────────────────────── analyze ───────────────────────────
    async def analyze(self, task: dict) -> None:
        if not self.store.claim(task["id"], S.NEW, S.ANALYZING):
            return
        await self.notify.text(f"🔎 Analyzing task #{task['id']}: <b>{esc(task['title'])}</b>")
        p = (f"TASK #{task['id']}: {task['title']}\n\nDETAILS:\n{task['description'] or '(none)'}\n\n"
             f"REPOSITORIES (folders under {self.cfg.repos_root}):\n{self._repo_catalog()}\n\n"
             f"Earlier answers from Suni:\n{self._qa(task['id'])}")
        r = await self.engine.run_agent(
            role="planner", prompt=p, system_append=prompt("analyze"), cwd=self.cfg.repos_root,
            allowed_roots=[], task_id=task["id"], read_only=True, schema=ANALYSIS_SCHEMA)
        a = r.data if isinstance(r.data, dict) else None
        if not r.ok or not a:
            self.store.update_task(task["id"], status=S.FAILED, error=f"analysis failed: {r.text[:500]}")
            await self.notify.text(f"❌ Analysis failed for #{task['id']}: {esc(r.text[:300])}")
            return
        valid = [x for x in a.get("repos", []) if self.cfg.repo(x)]
        a["repos"] = valid
        self.store.update_task(task["id"], analysis=a, repos=valid, complexity=a.get("complexity", ""),
                               branch=task.get("branch") or self.git.branch_for(task))
        if not valid:
            self.store.add_question(task["id"], "Which repositories does this task touch?", "technical",
                                    [r.name for r in self.cfg.repos][:6])
        for q in a.get("questions", []):
            self.store.add_question(task["id"], q["text"], q.get("kind", "technical"), q.get("options"))
        await self._ask_or_ready(task["id"])

    async def _ask_or_ready(self, task_id: int) -> None:
        task = self.store.task(task_id)
        pending = self.store.questions(task_id, unanswered=True)
        if pending:
            self.store.update_task(task_id, status=S.NEEDS_INFO)
            for q in pending:
                if not q.get("tg_message_id"):
                    mid = await self.notify.question(q, task)
                    if mid:
                        self.store.set_question_msg(q["id"], mid)
            return
        a = task["analysis"] or {}
        plan = "\n".join(f"• {esc(s)}" for s in a.get("plan", [])[:8])
        self.store.update_task(task_id, status=S.READY)
        await self.notify.text(
            f"✅ Task #{task_id} ready — {a.get('complexity', '?')} · repos: "
            f"{', '.join(task['repos'])}\n{plan}")
        self.poke()

    async def answer(self, qid: int, answer: str) -> dict | None:
        q = self.store.question(qid)
        if not q:
            return None
        self.store.answer_question(qid, answer)
        task = self.store.task(q["task_id"])
        if q["text"].startswith("Which repositories"):
            names = [n.strip() for n in answer.replace(";", ",").split(",") if self.cfg.repo(n.strip())]
            if names:
                self.store.update_task(task["id"], repos=names)
        if task and task["status"] == S.NEEDS_INFO and not self.store.questions(task["id"], unanswered=True):
            await self._ask_or_ready(task["id"])
        return q

    # ─────────────────────────── implement ───────────────────────────
    def _role_for(self, task: dict) -> str:
        return "implement_complex" if task.get("complexity") == "L" or len(task["repos"]) > 2 else "implement"

    async def implement(self, task: dict) -> None:
        from_status = task["status"]
        if not self.store.claim(task["id"], from_status, S.IMPLEMENTING):
            return
        tid = task["id"]
        try:
            tdir, branch, paths = await self.git.prepare(task, task["repos"])
        except GitError as e:
            self.store.update_task(tid, status=S.FAILED, error=str(e))
            await self.notify.text(f"❌ #{tid} git setup failed: {esc(str(e)[:400])}")
            return
        artifacts = tdir / "_artifacts"
        (artifacts / "screenshots").mkdir(parents=True, exist_ok=True)
        self.store.update_task(tid, branch=branch, work_path=str(tdir))
        task = self.store.task(tid)
        a = task["analysis"] or {}
        feedback = task.get("feedback") if from_status == S.CHANGES_REQUESTED else ""
        await self.notify.text(
            f"🛠 {'Applying your feedback on' if feedback else 'Implementing'} #{tid} on "
            f"<code>{esc(branch)}</code> ({', '.join(task['repos'])}) with {self.cfg.model(self._role_for(task))}")

        repo_info = "\n".join(
            f"- {n}/ ({self.cfg.repo(n).stack}) test: `{self.cfg.repo(n).test or 'n/a'}`"
            f"{' dev url: ' + self.cfg.repo(n).dev_url if self.cfg.repo(n).dev_url else ''}"
            for n in task["repos"])
        if feedback:
            p = (f"Suni reviewed your work on task #{tid} and requested changes:\n\n{feedback}\n\n"
                 "Apply them, update/extend the tests and screenshots, re-run the tests, and report again.")
        else:
            p = (f"TASK #{tid}: {task['title']}\n\nDETAILS:\n{task['description'] or '(none)'}\n\n"
                 f"PLAN:\n" + "\n".join(f"- {s}" for s in a.get("plan", [])) +
                 "\n\nTEST PLAN:\n" + "\n".join(f"- {s}" for s in a.get("test_plan", [])) +
                 f"\n\nSUNI'S ANSWERS:\n{self._qa(tid)}\n\nREPOS IN THIS WORKSPACE:\n{repo_info}\n\n"
                 f"UI flows involved: {'yes — take screenshots' if a.get('ui') else 'no'}")
        system = prompt("implement").format(artifacts=artifacts)
        role = self._role_for(task)
        r = await self.engine.run_agent(role=role, prompt=p, system_append=system, cwd=tdir,
                                        allowed_roots=[tdir], task_id=tid,
                                        resume=task.get("session_id") or None)
        if not r.ok:
            self.store.update_task(tid, status=S.FAILED, error=r.text[:1000], session_id=r.session_id)
            await self.notify.text(f"❌ #{tid} implementation stopped: {esc(r.text[:400])}\n"
                                   f"Use /retry {tid} to continue.")
            return
        session, report = r.session_id, r.text

        # independent review passes (2 for new work, 1 after your feedback)
        passes = 1 if feedback else 2
        review_notes: list[str] = []
        for i in range(1, passes + 1):
            rv = await self.review(task, tdir, i)
            blocking = [x for x in rv.get("issues", []) if x.get("severity") in ("blocking", "major")]
            review_notes.append(f"pass {i}: {rv.get('verdict')} ({len(blocking)} major)")
            if rv.get("verdict") == "approve" and not blocking:
                continue
            issues = "\n".join(f"- [{x.get('severity')}] {x.get('repo', '')}/{x.get('file', '')}:"
                               f"{x.get('line', '')} {x['problem']} → {x.get('fix', '')}"
                               for x in rv.get("issues", []))
            fix = await self.engine.run_agent(
                role=role, prompt=f"An independent reviewer found these issues (review pass {i}):\n"
                                  f"{issues}\n\nFix every blocking/major issue (and minor ones if cheap), "
                                  "re-run tests, update screenshots, and report.",
                system_append=system, cwd=tdir, allowed_roots=[tdir], task_id=tid, resume=session)
            if fix.ok:
                session, report = fix.session_id or session, fix.text
            review_notes[-1] += " → fixed" if fix.ok else " → fix failed"

        tests = await self.run_tests(task, tdir)
        self.store.update_task(tid, session_id=session, status=S.AWAITING_APPROVAL, feedback="")
        await self.send_for_review(self.store.task(tid), tdir, report, tests, "; ".join(review_notes))

    async def review(self, task: dict, tdir: Path, n: int) -> dict:
        a = task["analysis"] or {}
        p = (f"Review pass {n} for TASK #{task['id']}: {task['title']}\n\n{task['description']}\n\n"
             f"Planned approach:\n" + "\n".join(f"- {s}" for s in a.get("plan", [])) +
             f"\n\nSuni's answers:\n{self._qa(task['id'])}\n\nRepos: {', '.join(task['repos'])}. "
             f"Screenshots should be in {tdir / '_artifacts' / 'screenshots'}.")
        r = await self.engine.run_agent(role="review", prompt=p, system_append=prompt("review"),
                                        cwd=tdir, allowed_roots=[], task_id=task["id"],
                                        read_only=True, schema=REVIEW_SCHEMA)
        return r.data if isinstance(r.data, dict) else {"verdict": "changes_needed", "summary": r.text,
                                                         "issues": []}

    async def run_tests(self, task: dict, tdir: Path) -> str:
        """Re-run each repo's test command ourselves — never trust the model's claim alone."""
        out = []
        for name in task["repos"]:
            repo = self.cfg.repo(name)
            if not repo or not repo.test:
                continue
            r = await sh(repo.test, cwd=tdir / name, check=False, timeout=1800)
            tail = (r.out + r.err).strip().splitlines()[-3:]
            out.append(f"{name}: {'✅ pass' if r.code == 0 else '❌ FAIL'} — {' | '.join(tail)[:200]}")
        return "\n".join(out) or "no test commands configured"

    async def send_for_review(self, task: dict, tdir: Path, report: str, tests: str, reviews: str) -> None:
        stats, patches = [], []
        for name in task["repos"]:
            st, patch = await self.git.diff(tdir / name)
            if st.strip():
                stats.append(f"{name}:\n{st.strip().splitlines()[-1]}")
                patches.append(f"### {name}\n{patch}")
        diff_file = tdir / "_artifacts" / f"task-{task['id']}.diff"
        diff_file.write_text("\n\n".join(patches) or "(no changes)")
        shots = sorted((tdir / "_artifacts" / "screenshots").glob("*.png"))
        html = jinja.get_template("telegram_review.j2").render(
            t=task, stat=esc("\n".join(stats) or "no changes"), tests=esc(tests), reviews=esc(reviews),
            cost=self.store.task_cost(task["id"]), report=esc(report[:1500]))
        await self.notify.review(task, html, shots, diff_file)

    # ─────────────────────────── your decisions ───────────────────────────
    async def request_changes(self, task_id: int, feedback: str) -> None:
        t = self.store.task(task_id)
        if not t or t["status"] not in (S.AWAITING_APPROVAL, S.PR_OPEN):
            await self.notify.text(f"Task #{task_id} is not waiting for review.")
            return
        prev = t.get("feedback") or ""
        self.store.update_task(task_id, status=S.CHANGES_REQUESTED,
                               feedback=(prev + "\n\n" + feedback).strip())
        await self.notify.text(f"✏️ Got it — reworking #{task_id}.")
        self.poke()

    async def approve(self, task_id: int) -> None:
        if not self.store.claim(task_id, S.AWAITING_APPROVAL, S.PUBLISHING):
            await self.notify.text(f"Task #{task_id} is not awaiting approval.")
            return
        task = self.store.task(task_id)
        tdir = Path(task["work_path"])
        a = task["analysis"] or {}
        msg = f"{a.get('change_type', 'feat')}: {task['title']}"
        links: list[dict] = []
        try:
            for name in task["repos"]:
                repo = self.cfg.repo(name)
                pushed = await self.git.commit_and_push(tdir / name, task["branch"], msg)
                if not pushed:
                    continue
                url = await self.prs.create(repo, task["branch"], task["title"],
                                            self._pr_body(task), cwd=tdir / name)
                links.append({"repo": name, "url": url})
        except Exception as e:  # noqa: BLE001
            self.store.update_task(task_id, status=S.AWAITING_APPROVAL, error=str(e), pr_links=links)
            await self.notify.text(f"❌ Publishing #{task_id} failed: {esc(str(e)[:400])}\n"
                                   f"Fix and tap Approve again (already-pushed repos are skipped safely).")
            return
        self.store.update_task(task_id, status=S.PR_OPEN, pr_links=links)
        if self.havana:
            await self.havana.update_status(task, "in_review", links)
        await self.notify.text("🚀 PRs created for #%d:\n%s" % (task_id, "\n".join(
            f"{i}. {l['url']}" for i, l in enumerate(links, 1)) or "(no changes to publish)"))
        if links:
            await self.propose_teams_post(task_id)

    def _pr_body(self, task: dict) -> str:
        a = task["analysis"] or {}
        parts = [f"## {task['title']}", task["description"] or "",
                 "### Changes", *[f"- {s}" for s in a.get("plan", [])],
                 "### Testing", *[f"- {s}" for s in a.get("test_plan", [])]]
        qa = self._qa(task["id"])
        if qa != "(none)":
            parts += ["### Clarifications", qa]
        return "\n".join(parts)

    def teams_message(self, task_ids: list[int], html: bool = False) -> str:
        tasks = [self.store.task(i) for i in task_ids]
        prs = [p for t in tasks for p in (t.get("pr_links") or [])]
        topics = [(t["analysis"] or {}).get("pr_topic") or t["title"] for t in tasks]
        names = self.cfg.get("teams.pr_reviewers", [])
        mentions = " ".join(f'<at id="{i}">{n}</at>' for i, n in enumerate(names)) if html else " ".join(names)
        return jinja.get_template("pr_message.j2").render(
            mentions=mentions, summary=" and ".join(topics), prs=prs)

    async def propose_teams_post(self, task_id: int) -> None:
        text = self.teams_message([task_id])
        if self.cfg.get("teams.confirm_before_post", True):
            await self.notify.confirm(f"teams:{task_id}",
                                      f"📣 Post this to <b>{esc(self.cfg.get('teams.pr_chat_topic', ''))}</b>?"
                                      f"\n\n<pre>{esc(text)}</pre>",
                                      [("Post ✅", "post"), ("Skip", "skip")])
        else:
            await self.post_to_teams(task_id)

    async def post_to_teams(self, task_id: int) -> None:
        if not self.teams:
            await self.notify.text("Teams isn't configured — copy the message above manually.")
            return
        try:
            await self.teams.post_pr_message(self.teams_message([task_id], html=True),
                                             self.cfg.get("teams.pr_reviewers", []),
                                             plain=self.teams_message([task_id]))
            await self.notify.text(f"📣 Posted PRs for #{task_id} to Teams.")
        except Exception as e:  # noqa: BLE001
            await self.notify.text(f"❌ Teams post failed: {esc(str(e)[:300])}")

    # ─────────────────────────── worker loop ───────────────────────────
    async def run_forever(self) -> None:
        # anything interrupted by a restart goes back to its previous safe state
        for t in self.store.tasks([S.ANALYZING]):
            self.store.update_task(t["id"], status=S.NEW)
        for t in self.store.tasks([S.IMPLEMENTING]):
            self.store.update_task(t["id"], status=S.READY)
        for t in self.store.tasks([S.PUBLISHING]):
            self.store.update_task(t["id"], status=S.AWAITING_APPROVAL)
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> None:
        for t in self.store.tasks([S.NEW]):
            if t["id"] not in self._running:
                self._spawn(t, self.analyze)
        limit = int(self.cfg.get("engine.parallel_tasks", 1))
        busy = len(self.store.tasks([S.IMPLEMENTING]))
        for t in self.store.tasks([S.CHANGES_REQUESTED, S.READY]):
            if busy >= limit:
                break
            if t["id"] not in self._running:
                busy += 1
                self._spawn(t, self.implement)

    def _spawn(self, task: dict, fn) -> None:
        self._running.add(task["id"])

        async def run():
            try:
                await fn(task)
            except Exception as e:  # noqa: BLE001
                log.exception("task %s failed", task["id"])
                self.store.update_task(task["id"], status=S.FAILED, error=str(e)[:1000])
                await self.notify.text(f"❌ Task #{task['id']} crashed: {esc(str(e)[:300])}")
            finally:
                self._running.discard(task["id"])
                self.poke()

        asyncio.create_task(run())
