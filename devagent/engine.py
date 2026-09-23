"""Claude engine: wraps the Claude Agent SDK with safety guardrails.

Two entry points:
  * ask_json()  – one-shot structured answer, no tools (planner, triage, analysis summaries)
  * run_agent() – full coding agent inside a task workspace, guarded by PreToolUse hooks
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    query,
)

from .config import Config, get_secret

log = logging.getLogger("devagent.engine")

# ── guardrails ───────────────────────────────────────────────────────────────
# The orchestrator (not the model) commits, pushes and opens PRs, after YOU approve.
G = r"\bgit(?:\s+(?:-C|-c)\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+"  # git + global opts (-C dir, -c k=v)
DENY_BASH = [
    (G + r"push\b", "Pushing is done by the orchestrator after approval."),
    (G + r"commit\b", "Leave changes uncommitted; the orchestrator commits after approval."),
    (G + r"(merge|rebase)\b", "Merging/rebasing is not allowed for the agent."),
    (G + r"reset\s+--hard\b", "Destructive git reset is not allowed."),
    (G + r"(checkout|switch)\s+(main|master|develop)\b", "Stay on the task branch."),
    (G + r"branch\s+-[dD]\b", "Deleting branches is not allowed."),
    (G + r"worktree\b", "Worktrees are managed by the orchestrator."),
    (G + r"clean\s+-[a-z]*f", "git clean is not allowed."),
    (G + r"config\b", "Do not change git config."),
    (r"\bgh\s+pr\s+(merge|close)\b", "Merging PRs is not allowed."),
    (r"\bsudo\b", "sudo is not allowed."),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)(\s|$)", "Refusing to delete home/root."),
    (r"artisan\s+(migrate:fresh|migrate:reset|migrate:refresh|db:wipe)(?!.*--env=testing)",
     "Destructive migrations only allowed with --env=testing."),
    (r"\b(DROP\s+(DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\b", "Destructive SQL is not allowed."),
    (r"security\s+find-(generic|internet)-password", "Keychain access is not allowed."),
    (r"(\.ssh/|id_rsa|id_ed25519)", "SSH keys are off limits."),
    (r"\b(curl|wget|http)\b.*\b(bitbucket\.org|api\.github\.com|graph\.microsoft\.com)",
     "External APIs are called by the orchestrator only."),
]
_DENY = [(re.compile(p, re.I), why) for p, why in DENY_BASH]
_ENV_FILE = re.compile(r"(^|/)\.env(\.(?!example$|testing$)[\w.-]+)?$")
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def check_bash(command: str) -> str | None:
    for rx, why in _DENY:
        if rx.search(command):
            return why
    return None


def check_write(path: str, allowed_roots: list[Path]) -> str | None:
    p = Path(path).expanduser()
    if _ENV_FILE.search(str(p)):
        return f"Editing {p.name} is not allowed (secrets/config). Use .env.testing or .env.example."
    try:
        rp = p.resolve()
    except OSError:
        rp = p
    if not any(str(rp).startswith(str(r.resolve())) for r in allowed_roots):
        return f"Writes are limited to the task workspace ({', '.join(map(str, allowed_roots))})."
    return None


def make_guard(allowed_roots: list[Path]):
    async def guard(input_data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        name = input_data.get("tool_name", "")
        tin = input_data.get("tool_input", {}) or {}
        reason = None
        if name == "Bash":
            reason = check_bash(tin.get("command", ""))
        elif name in WRITE_TOOLS:
            reason = check_write(tin.get("file_path") or tin.get("notebook_path") or "", allowed_roots)
        if reason:
            log.warning("guard denied %s: %s", name, reason)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
        return {}
    return guard


# ── results ──────────────────────────────────────────────────────────────────
@dataclass
class RunResult:
    ok: bool
    text: str = ""
    data: Any = None
    session_id: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    errors: list[str] = field(default_factory=list)


def _env() -> dict[str, str]:
    env: dict[str, str] = {}
    key = get_secret("anthropic_api_key")
    if key:
        env["ANTHROPIC_API_KEY"] = key
    return env


def _parse_json(text: str) -> Any:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start > 0:
        text = text[start:]
    return json.loads(text)


async def _collect(prompt: str, options: ClaudeAgentOptions) -> RunResult:
    texts: list[str] = []
    res = RunResult(ok=False)
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            texts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
        elif isinstance(msg, ResultMessage):
            res.ok = not msg.is_error
            res.session_id = msg.session_id
            res.cost_usd = msg.total_cost_usd or 0.0
            res.turns = msg.num_turns
            res.text = msg.result or (texts[-1] if texts else "")
            res.data = msg.structured_output
            res.errors = list(msg.errors or [])
    if not res.text and texts:
        res.text = texts[-1]
    return res


class Engine:
    def __init__(self, cfg: Config, store=None):
        self.cfg = cfg
        self.store = store

    def _log(self, task_id, role, model, r: RunResult):
        if self.store:
            self.store.log_run(task_id, role, model, r.cost_usd, r.turns, r.ok, r.text[:500])

    async def ask_json(self, role: str, system: str, prompt: str, schema: dict[str, Any],
                       task_id: int | None = None) -> RunResult:
        """Single structured answer with no tools (cheap, safe)."""
        model = self.cfg.model(role)
        opts = ClaudeAgentOptions(
            model=model, system_prompt=system, tools=[], max_turns=3,
            output_format={"type": "json_schema", "schema": schema},
            setting_sources=[], env=_env(), effort=self.cfg.effort(role),
        )
        r = await _collect(prompt, opts)
        if r.data is None and r.text:
            try:
                r.data = _parse_json(r.text)
            except (json.JSONDecodeError, ValueError):
                r.ok = False
                r.errors.append("could not parse JSON")
        self._log(task_id, role, model, r)
        return r

    async def run_agent(self, *, role: str, prompt: str, system_append: str, cwd: Path,
                        allowed_roots: list[Path], task_id: int | None = None,
                        resume: str | None = None, read_only: bool = False,
                        schema: dict[str, Any] | None = None,
                        agents: dict[str, AgentDefinition] | None = None,
                        extra_dirs: list[Path] | None = None) -> RunResult:
        model = self.cfg.model(role)
        disallowed = ["WebSearch"] + (sorted(WRITE_TOOLS) if read_only else [])
        opts = ClaudeAgentOptions(
            model=model,
            system_prompt={"type": "preset", "preset": "claude_code", "append": system_append},
            cwd=str(cwd),
            add_dirs=[str(d) for d in (extra_dirs or [])],
            permission_mode="bypassPermissions",      # guard hook below is the policy
            disallowed_tools=disallowed,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[make_guard(allowed_roots)])]},
            setting_sources=["project"],              # loads each repo's CLAUDE.md
            max_turns=int(self.cfg.get("engine.max_turns_per_run", 150)),
            max_budget_usd=float(self.cfg.get("engine.max_budget_usd_per_task", 15)),
            effort=self.cfg.effort(role),
            resume=resume,
            agents=agents,
            env=_env(),
            output_format={"type": "json_schema", "schema": schema} if schema else None,
        )
        try:
            r = await _collect(prompt, opts)
        except Exception as e:  # CLI crash, auth problem, budget hit…
            log.exception("agent run failed")
            r = RunResult(ok=False, text=str(e), errors=[str(e)])
        if schema and r.data is None and r.text:
            try:
                r.data = _parse_json(r.text)
            except (json.JSONDecodeError, ValueError):
                pass
        self._log(task_id, role, model, r)
        return r
