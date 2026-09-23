"""Telegram bot = your phone remote. Questions, approvals, screenshots, Teams alerts, recording."""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
from pathlib import Path

from telegram import (ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
                      Update)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .. import store as S
from ..config import Config

log = logging.getLogger("devagent.telegram")
MAX = 4000

HELP = """<b>dev-agent commands</b>
/task &lt;title&gt; — new task (next lines = details)
/tasks — active tasks   ·   /status — what's running
/plan — today's day plan   ·   /cost — spend today
/rec [title] — start recording a call   ·   /stop — stop &amp; summarise
/approve &lt;id&gt;   ·   /changes &lt;id&gt; &lt;feedback&gt;
/retry &lt;id&gt;   ·   /cancel &lt;id&gt;   ·   /prio &lt;id&gt; &lt;1-5&gt;
/teamslogin — sign in to Microsoft Teams
Reply to a ❓ question to answer it. Reply to a review to request changes.
Any other text → I'll offer to create a task."""


def _chunks(s: str, n: int = MAX):
    for i in range(0, len(s), n):
        yield s[i:i + n]


class TelegramBridge:
    """Implements the Notifier protocol and hosts the bot."""

    def __init__(self, cfg: Config, store: S.Store, token: str):
        self.cfg, self.store = cfg, store
        self.app = Application.builder().token(token).build()
        self.pipeline = None
        self.meetings = None
        self.teams = None
        self.teams_auth = None
        chat = cfg.get("telegram.allowed_chat_id")
        self.chat_id: int | None = int(chat) if chat else None
        self._register()

    def attach(self, pipeline, meetings=None, teams=None, teams_auth=None) -> None:
        self.pipeline, self.meetings, self.teams, self.teams_auth = pipeline, meetings, teams, teams_auth

    # ─────────────── Notifier protocol ───────────────
    async def text(self, message: str, html_mode: bool = True, **kw) -> None:
        if not self.chat_id:
            log.info("[no telegram chat yet] %s", message)
            return
        for part in _chunks(message):
            try:
                await self.app.bot.send_message(self.chat_id, part,
                                                parse_mode=ParseMode.HTML if html_mode else None,
                                                disable_web_page_preview=True, **kw)
            except TelegramError as e:  # malformed HTML → send plain
                log.warning("telegram send failed (%s); retrying as plain text", e)
                await self.app.bot.send_message(self.chat_id, html.unescape(part))

    async def question(self, q: dict, task: dict) -> int | None:
        if not self.chat_id:
            return None
        icon = "💼" if q.get("kind") == "business" else "🔧"
        opts = q.get("options") or []
        kb = [[InlineKeyboardButton(o[:60], callback_data=f"q:{q['id']}:{i}")] for i, o in enumerate(opts[:4])]
        msg = await self.app.bot.send_message(
            self.chat_id,
            f"❓ <b>Task #{task['id']}</b> {html.escape(task['title'])}\n{icon} "
            f"<i>{q.get('kind', 'technical').title()}</i>: {html.escape(q['text'])}\n\n"
            f"<i>Tap an option or reply to this message.</i>",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        return msg.message_id

    async def review(self, task: dict, summary_html: str, screenshots: list[Path],
                     diff_file: Path | None) -> None:
        if not self.chat_id:
            return
        shots = screenshots[:30]
        for i in range(0, len(shots), 10):
            group = [InputMediaPhoto(p.read_bytes(), caption=p.stem[:1000]) for p in shots[i:i + 10]]
            try:
                await self.app.bot.send_media_group(self.chat_id, group)
            except TelegramError as e:
                log.warning("screenshot upload failed: %s", e)
        if len(screenshots) > 30:
            await self.text(f"(+{len(screenshots) - 30} more screenshots in {screenshots[0].parent})")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Approve ✅ → PR", callback_data=f"rev:{task['id']}:approve")],
            [InlineKeyboardButton("Request changes ✏️", callback_data=f"rev:{task['id']}:changes"),
             InlineKeyboardButton("Diff 📄", callback_data=f"rev:{task['id']}:diff")]])
        body = summary_html if len(summary_html) < MAX else summary_html[:MAX - 20] + "…"
        try:
            msg = await self.app.bot.send_message(self.chat_id, body, parse_mode=ParseMode.HTML,
                                                  reply_markup=kb, disable_web_page_preview=True)
        except TelegramError:
            msg = await self.app.bot.send_message(self.chat_id, html.unescape(body), reply_markup=kb)
        self.store.kv_set(f"fb_msg:{msg.message_id}", str(task["id"]))
        if diff_file:
            self.store.kv_set(f"diff:{task['id']}", str(diff_file))

    async def confirm(self, key: str, message: str, buttons: list[tuple[str, str]]) -> None:
        if not self.chat_id:
            return
        tok = secrets.token_hex(4)                  # callback_data is limited to 64 bytes
        self.store.kv_set(f"cb:{tok}", key)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"c:{tok}:{val}")]
                                   for label, val in buttons])
        try:
            await self.app.bot.send_message(self.chat_id, message[:MAX], parse_mode=ParseMode.HTML,
                                            reply_markup=kb, disable_web_page_preview=True)
        except TelegramError:
            await self.app.bot.send_message(self.chat_id, html.unescape(message[:MAX]), reply_markup=kb)

    async def file(self, path: Path, caption: str = "") -> None:
        if self.chat_id and path.exists():
            with path.open("rb") as f:
                await self.app.bot.send_document(self.chat_id, f, filename=path.name, caption=caption[:1000])

    # ─────────────── bot plumbing ───────────────
    def _register(self) -> None:
        a = self.app
        a.add_handler(CommandHandler("start", self.cmd_start))
        a.add_handler(CommandHandler("help", self.cmd_help))
        for name in ["task", "tasks", "status", "plan", "cost", "rec", "stop", "approve", "changes",
                     "retry", "cancel", "prio", "teamslogin"]:
            a.add_handler(CommandHandler(name, self._guard(getattr(self, f"cmd_{name}"))))
        a.add_handler(CallbackQueryHandler(self._guard(self.on_button)))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._guard(self.on_text)))

    def _guard(self, fn):
        async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not self.chat_id or update.effective_chat.id != self.chat_id:
                log.warning("ignored message from chat %s", update.effective_chat.id)
                return
            try:
                await fn(update, ctx)
            except Exception as e:  # noqa: BLE001
                log.exception("handler failed")
                await self.text(f"⚠️ {html.escape(str(e)[:300])}")
        return wrapped

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=False)
        if self.chat_id:
            await self.text("🤖 dev-agent is online. /help for commands.")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def _reply(self, update: Update, text: str) -> None:
        await update.effective_message.reply_text(text[:MAX], parse_mode=ParseMode.HTML,
                                                  disable_web_page_preview=True)

    # ─────────────── commands ───────────────
    async def cmd_start(self, update: Update, ctx) -> None:
        cid = update.effective_chat.id
        if self.chat_id is None:
            self.chat_id = cid
            self.cfg.data.setdefault("telegram", {})["allowed_chat_id"] = cid
            self.cfg.save()
            await self._reply(update, "✅ Paired. Only this chat can control the agent.\n\n" + HELP)
        elif cid == self.chat_id:
            await self._reply(update, HELP)

    async def cmd_help(self, update: Update, ctx) -> None:
        if update.effective_chat.id == self.chat_id:
            await self._reply(update, HELP)

    async def cmd_task(self, update: Update, ctx) -> None:
        raw = update.effective_message.text.partition(" ")[2].strip()
        if not raw:
            await self._reply(update, "Usage: /task Title\nDetails on the following lines…")
            return
        title, _, desc = raw.partition("\n")
        tid = self.pipeline.add_task(title.strip(), desc.strip(), source="telegram")
        await self._reply(update, f"🆕 Task #{tid} queued — I'll analyse it and ask if anything is unclear.")

    async def cmd_tasks(self, update: Update, ctx) -> None:
        ts = self.store.active_tasks() + self.store.tasks([S.PR_OPEN])
        if not ts:
            await self._reply(update, "No active tasks.")
            return
        icon = {S.NEW: "🆕", S.ANALYZING: "🔎", S.NEEDS_INFO: "❓", S.READY: "⏳", S.IMPLEMENTING: "🛠",
                S.AWAITING_APPROVAL: "👀", S.CHANGES_REQUESTED: "✏️", S.PUBLISHING: "🚀", S.PR_OPEN: "🔀",
                S.PAUSED: "⏸"}
        lines = [f"{icon.get(t['status'], '•')} #{t['id']} P{t['priority']} {html.escape(t['title'])} "
                 f"<i>{t['status']}</i>" for t in ts]
        await self._reply(update, "\n".join(lines))

    async def cmd_status(self, update: Update, ctx) -> None:
        running = self.store.tasks([S.ANALYZING, S.IMPLEMENTING, S.PUBLISHING])
        rec = "🔴 recording" if self.meetings and self.meetings.rec.recording else "not recording"
        today = S.now()[:10]
        await self._reply(update, (
            f"Working on: {', '.join('#' + str(t['id']) for t in running) or 'nothing'}\n"
            f"Waiting for you: {len(self.store.tasks([S.NEEDS_INFO, S.AWAITING_APPROVAL]))} task(s), "
            f"{len(self.store.open_questions())} question(s)\n"
            f"Meetings: {rec}\nSpend today: ${self.store.cost_since(today):.2f}"))

    async def cmd_plan(self, update: Update, ctx) -> None:
        m = self.store.last_meeting("priorities", S.now()[:10])
        if not m or not isinstance(m.get("plan"), dict):
            await self._reply(update, "No priorities call processed today yet.")
            return
        plan = "\n".join(f"<code>{html.escape(b['time'])}</code> {html.escape(b['task'])} — "
                         f"{html.escape(b['goal'])}" for b in m["plan"].get("day_plan", []))
        await self._reply(update, f"📅 <b>Today</b>\n{plan}")

    async def cmd_cost(self, update: Update, ctx) -> None:
        today = S.now()[:10]
        await self._reply(update, f"💸 Today: ${self.store.cost_since(today):.2f}")

    async def cmd_rec(self, update: Update, ctx) -> None:
        title = " ".join(ctx.args) or "Call"
        await self._reply(update, await self.meetings.start("adhoc", title))

    async def cmd_stop(self, update: Update, ctx) -> None:
        await self._reply(update, await self.meetings.stop())

    def _id(self, ctx) -> int | None:
        try:
            return int(ctx.args[0].lstrip("#"))
        except (IndexError, ValueError):
            return None

    async def cmd_approve(self, update: Update, ctx) -> None:
        tid = self._id(ctx)
        if tid:
            await self.pipeline.approve(tid)

    async def cmd_changes(self, update: Update, ctx) -> None:
        tid = self._id(ctx)
        fb = update.effective_message.text.split(None, 2)[2] if len(ctx.args) > 1 else ""
        if tid and fb:
            await self.pipeline.request_changes(tid, fb)
        else:
            await self._reply(update, "Usage: /changes &lt;id&gt; what to change")

    async def cmd_retry(self, update: Update, ctx) -> None:
        tid = self._id(ctx)
        t = self.store.task(tid) if tid else None
        if not t:
            return
        new = S.READY if t.get("analysis") and t["repos"] else S.NEW
        self.store.update_task(tid, status=new, error="")
        self.pipeline.poke()
        await self._reply(update, f"🔁 #{tid} → {new}")

    async def cmd_cancel(self, update: Update, ctx) -> None:
        tid = self._id(ctx)
        if tid:
            self.store.update_task(tid, status=S.CANCELLED)
            await self._reply(update, f"✖️ #{tid} cancelled (branch/worktree kept).")

    async def cmd_prio(self, update: Update, ctx) -> None:
        tid = self._id(ctx)
        if tid and len(ctx.args) > 1 and ctx.args[1].isdigit():
            self.store.update_task(tid, priority=int(ctx.args[1]))
            self.pipeline.poke()
            await self._reply(update, f"↕️ #{tid} → P{ctx.args[1]}")

    async def cmd_teamslogin(self, update: Update, ctx) -> None:
        if not self.teams_auth:
            await self._reply(update, "Teams is disabled in config.")
            return
        loop = asyncio.get_running_loop()

        def show(msg: str) -> None:
            asyncio.run_coroutine_threadsafe(self.text("🔐 " + html.escape(msg)), loop)

        name = await asyncio.to_thread(self.teams_auth.device_login, show)
        await self.text(f"✅ Teams signed in as {html.escape(name)}")

    # ─────────────── buttons & replies ───────────────
    async def on_button(self, update: Update, ctx) -> None:
        cq = update.callback_query
        await cq.answer()
        data = cq.data or ""
        if data.startswith("q:"):
            _, qid, idx = data.split(":")
            q = self.store.question(int(qid))
            if q and q.get("answer") is None:
                ans = q["options"][int(idx)]
                await self.pipeline.answer(int(qid), ans)
                await cq.edit_message_reply_markup(None)
                await self.text(f"👍 Q{qid}: {html.escape(ans)}")
        elif data.startswith("rev:"):
            _, tid, action = data.split(":")
            tid = int(tid)
            if action == "approve":
                await cq.edit_message_reply_markup(None)
                await self.text(f"🚀 Publishing #{tid}: commit → push → PR…")
                await self.pipeline.approve(tid)
            elif action == "changes":
                m = await self.app.bot.send_message(
                    self.chat_id, f"✏️ Reply with your feedback for #{tid} (code snippets welcome):",
                    reply_markup=ForceReply(selective=True))
                self.store.kv_set(f"fb_msg:{m.message_id}", str(tid))
            elif action == "diff":
                p = self.store.kv_get(f"diff:{tid}")
                if p:
                    await self.file(Path(p), f"Task #{tid} diff")
        elif data.startswith("c:"):
            _, tok, val = data.split(":", 2)
            key = self.store.kv_get(f"cb:{tok}")
            if key:
                await cq.edit_message_reply_markup(None)
                await self.on_confirm(key, val)

    async def on_confirm(self, key: str, val: str) -> None:
        kind, _, ref = key.partition(":")
        if kind == "teams":
            if val == "post":
                await self.pipeline.post_to_teams(int(ref))
        elif kind == "meet":
            mid = int(ref)
            if val == "all":
                ids = self.meetings.apply_meeting_tasks(mid)
                await self.text(f"🗂 Added tasks {', '.join(f'#{i}' for i in ids)} — analysing now.")
            elif val == "pick":
                m = await self.app.bot.send_message(self.chat_id, "Reply with task numbers, e.g. 1,3",
                                                    reply_markup=ForceReply(selective=True))
                self.store.kv_set(f"pick_msg:{m.message_id}", str(mid))
        elif kind == "stand":
            if val == "apply":
                n = self.meetings.apply_standup(int(ref))
                await self.text(f"✅ Applied {n} change(s). /tasks to see the list.")
        elif kind == "tm":  # Teams message alert
            if val == "reply":
                chat_id = ref.rsplit(":", 1)[0]
                reply = self.store.kv_get(key) or ""
                await self.teams.reply(chat_id, reply)
                await self.text("✅ Reply sent on Teams.")
            elif val == "task":
                title = self.store.kv_get(key + ":task") or "Task from Teams"
                tid = self.pipeline.add_task(title, "", source="teams")
                await self.text(f"🆕 Task #{tid} created.")
        elif kind == "newtask":
            if val == "yes":
                text = self.store.kv_get(key) or ""
                title, _, desc = text.partition("\n")
                tid = self.pipeline.add_task(title[:120], desc, source="telegram")
                await self.text(f"🆕 Task #{tid} queued.")

    async def on_text(self, update: Update, ctx) -> None:
        msg = update.effective_message
        text = msg.text or ""
        rt = msg.reply_to_message
        if rt:
            q = self.store.question_by_tg(rt.message_id)
            if q:
                await self.pipeline.answer(q["id"], text)
                await msg.reply_text(f"👍 Answer saved for Q{q['id']}.")
                return
            tid = self.store.kv_get(f"fb_msg:{rt.message_id}")
            if tid:
                await self.pipeline.request_changes(int(tid), text)
                return
            mid = self.store.kv_get(f"pick_msg:{rt.message_id}")
            if mid:
                picks = [int(x) for x in text.replace(" ", "").split(",") if x.isdigit()]
                ids = self.meetings.apply_meeting_tasks(int(mid), picks)
                await msg.reply_text(f"🗂 Added {', '.join(f'#{i}' for i in ids) or 'nothing'}.")
                return
        # a single open question? treat the text as its answer
        open_q = self.store.open_questions()
        if len(open_q) == 1 and not text.lower().startswith(("new ", "task")):
            await self.pipeline.answer(open_q[0]["id"], text)
            await msg.reply_text(f"👍 Took that as the answer to Q{open_q[0]['id']}. "
                                 "Reply directly to a question if you meant something else.")
            return
        key = f"newtask:{msg.message_id}"
        self.store.kv_set(key, text)
        await self.confirm(key, f"Create a task from this?\n<i>{html.escape(text[:300])}</i>",
                           [("Yes, create task", "yes"), ("No", "no")])
