"""SQLite state: tasks, clarifying questions, agent runs (cost), meetings, key/value."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  source TEXT DEFAULT 'manual',          -- manual | telegram | meeting | teams | havana
  source_ref TEXT DEFAULT '',
  priority INTEGER DEFAULT 3,            -- 1 = highest
  status TEXT DEFAULT 'new',
  complexity TEXT DEFAULT '',
  repos TEXT DEFAULT '[]',
  analysis TEXT DEFAULT '{}',
  branch TEXT DEFAULT '',
  work_path TEXT DEFAULT '',
  session_id TEXT DEFAULT '',
  pr_links TEXT DEFAULT '[]',
  feedback TEXT DEFAULT '',
  error TEXT DEFAULT '',
  meeting_id INTEGER,
  day TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  kind TEXT DEFAULT 'technical',         -- business | technical
  text TEXT NOT NULL,
  options TEXT DEFAULT '[]',
  answer TEXT,
  tg_message_id INTEGER,
  created_at TEXT, answered_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  role TEXT, model TEXT,
  cost_usd REAL DEFAULT 0, turns INTEGER DEFAULT 0,
  ok INTEGER DEFAULT 1, summary TEXT DEFAULT '',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS meetings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, title TEXT,
  started_at TEXT, ended_at TEXT,
  audio_path TEXT DEFAULT '', transcript TEXT DEFAULT '',
  minutes TEXT DEFAULT '', plan TEXT DEFAULT '{}',
  status TEXT DEFAULT 'recording'
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

# task lifecycle
NEW, ANALYZING, NEEDS_INFO, READY, IMPLEMENTING, AWAITING_APPROVAL, CHANGES_REQUESTED, \
    PUBLISHING, PR_OPEN, DONE, FAILED, PAUSED, CANCELLED = (
        "new", "analyzing", "needs_info", "ready", "implementing", "awaiting_approval",
        "changes_requested", "publishing", "pr_open", "done", "failed", "paused", "cancelled")
ACTIVE_STATES = {NEW, ANALYZING, NEEDS_INFO, READY, IMPLEMENTING, AWAITING_APPROVAL,
                 CHANGES_REQUESTED, PUBLISHING, PAUSED}
JSON_FIELDS = {"repos", "analysis", "pr_links", "options", "plan"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_FIELDS & d.keys():
        try:
            d[k] = json.loads(d[k]) if d[k] else ([] if k in {"repos", "pr_links", "options"} else {})
        except (TypeError, json.JSONDecodeError):
            pass
    return d


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ── helpers ──
    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, tuple(args))
            self.db.commit()
            return cur

    @staticmethod
    def _enc(fields: dict[str, Any]) -> dict[str, Any]:
        return {k: (json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and not isinstance(v, str) else v)
                for k, v in fields.items()}

    # ── tasks ──
    def add_task(self, title: str, **fields: Any) -> int:
        fields = self._enc({"title": title, "created_at": now(), "updated_at": now(), **fields})
        cols = ",".join(fields)
        q = ",".join("?" for _ in fields)
        return self._exec(f"INSERT INTO tasks ({cols}) VALUES ({q})", fields.values()).lastrowid

    def update_task(self, task_id: int, **fields: Any) -> None:
        fields = self._enc({**fields, "updated_at": now()})
        sets = ",".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE tasks SET {sets} WHERE id=?", [*fields.values(), task_id])

    def task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            return _row(self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def tasks(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if statuses:
                st = list(statuses)
                rows = self.db.execute(
                    f"SELECT * FROM tasks WHERE status IN ({','.join('?' for _ in st)}) "
                    "ORDER BY priority, id", st).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM tasks ORDER BY priority, id").fetchall()
        return [_row(r) for r in rows]  # type: ignore

    def active_tasks(self) -> list[dict[str, Any]]:
        return self.tasks(ACTIVE_STATES)

    def next_task(self, status: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(self.db.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY priority, id LIMIT 1", (status,)).fetchone())

    def claim(self, task_id: int, from_status: str, to_status: str) -> bool:
        """Atomic status transition so two workers never grab the same task."""
        cur = self._exec("UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status=?",
                         (to_status, now(), task_id, from_status))
        return cur.rowcount == 1

    # ── questions ──
    def add_question(self, task_id: int, text: str, kind: str = "technical",
                     options: list[str] | None = None) -> int:
        return self._exec(
            "INSERT INTO questions (task_id, kind, text, options, created_at) VALUES (?,?,?,?,?)",
            (task_id, kind, text, json.dumps(options or []), now())).lastrowid

    def question(self, qid: int) -> dict[str, Any] | None:
        with self._lock:
            return _row(self.db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone())

    def question_by_tg(self, message_id: int) -> dict[str, Any] | None:
        with self._lock:
            return _row(self.db.execute(
                "SELECT * FROM questions WHERE tg_message_id=?", (message_id,)).fetchone())

    def questions(self, task_id: int, unanswered: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM questions WHERE task_id=?" + (" AND answer IS NULL" if unanswered else "")
        with self._lock:
            return [_row(r) for r in self.db.execute(sql + " ORDER BY id", (task_id,)).fetchall()]  # type: ignore

    def open_questions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_row(r) for r in self.db.execute(  # type: ignore
                "SELECT * FROM questions WHERE answer IS NULL ORDER BY id").fetchall()]

    def answer_question(self, qid: int, answer: str) -> None:
        self._exec("UPDATE questions SET answer=?, answered_at=? WHERE id=?", (answer, now(), qid))

    def set_question_msg(self, qid: int, message_id: int) -> None:
        self._exec("UPDATE questions SET tg_message_id=? WHERE id=?", (message_id, qid))

    # ── runs / cost ──
    def log_run(self, task_id: int | None, role: str, model: str, cost: float, turns: int,
                ok: bool, summary: str = "") -> None:
        self._exec("INSERT INTO runs (task_id, role, model, cost_usd, turns, ok, summary, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (task_id, role, model, cost or 0, turns or 0, int(ok),
                                                 summary[:2000], now()))

    def cost_since(self, iso_date: str) -> float:
        with self._lock:
            r = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE created_at>=?",
                                (iso_date,)).fetchone()
        return float(r[0])

    def task_cost(self, task_id: int) -> float:
        with self._lock:
            r = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE task_id=?",
                                (task_id,)).fetchone()
        return float(r[0])

    # ── meetings ──
    def add_meeting(self, kind: str, title: str, audio_path: str) -> int:
        return self._exec("INSERT INTO meetings (kind, title, started_at, audio_path) VALUES (?,?,?,?)",
                          (kind, title, now(), audio_path)).lastrowid

    def update_meeting(self, mid: int, **fields: Any) -> None:
        fields = self._enc(fields)
        sets = ",".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE meetings SET {sets} WHERE id=?", [*fields.values(), mid])

    def meeting(self, mid: int) -> dict[str, Any] | None:
        with self._lock:
            return _row(self.db.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone())

    def last_meeting(self, kind: str, since_iso: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(self.db.execute(
                "SELECT * FROM meetings WHERE kind=? AND started_at>=? AND status='done' "
                "ORDER BY id DESC LIMIT 1", (kind, since_iso)).fetchone())

    # ── kv ──
    def kv_get(self, k: str, default: str | None = None) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def kv_set(self, k: str, v: str) -> None:
        self._exec("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
