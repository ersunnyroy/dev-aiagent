"""Microsoft Teams via Microsoft Graph (delegated, signed in as you).

* post_pr_message – posts the PR request to the `v4-app` group chat with real @mentions
* TeamsWatcher     – polls your chats, triages new messages with Claude (Haiku), and pings
                     your phone only for urgent / needs-reply ones
Fallback for posting: a Power Automate "Workflows" webhook (secret: teams_webhook_url).
"""
from __future__ import annotations

import asyncio
import html as htmllib
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import HOME_DIR, Config, get_secret

log = logging.getLogger("devagent.teams")
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["User.Read", "Chat.ReadWrite", "ChatMessage.Send"]
CACHE = HOME_DIR / "msal_cache.bin"


class TeamsError(RuntimeError):
    pass


class GraphAuth:
    def __init__(self, cfg: Config):
        import msal

        self.cache = msal.SerializableTokenCache()
        if CACHE.exists():
            self.cache.deserialize(CACHE.read_text())
        self.app = msal.PublicClientApplication(
            cfg.get("teams.client_id"),
            authority=f"https://login.microsoftonline.com/{cfg.get('teams.tenant', 'organizations')}",
            token_cache=self.cache)

    def _save(self) -> None:
        if self.cache.has_state_changed:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(self.cache.serialize())
            CACHE.chmod(0o600)

    def token(self) -> str:
        accounts = self.app.get_accounts()
        res = self.app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        if not res or "access_token" not in res:
            raise TeamsError("Teams not signed in — run `devagent teams login` (or /teamslogin on Telegram).")
        self._save()
        return res["access_token"]

    def device_login(self, show: Callable[[str], None]) -> str:
        flow = self.app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise TeamsError(f"device flow failed: {flow}")
        show(flow["message"])
        res = self.app.acquire_token_by_device_flow(flow)  # blocks until you finish in the browser
        if "access_token" not in res:
            raise TeamsError(res.get("error_description", "login failed"))
        self._save()
        return res.get("id_token_claims", {}).get("name", "you")


def to_teams_html(text_with_at_tags: str) -> str:
    """Escape everything except our <at id="n">Name</at> tags, keep line breaks."""
    parts = re.split(r'(<at id="\d+">[^<]*</at>)', text_with_at_tags)
    out = [p if p.startswith("<at ") else htmllib.escape(p) for p in parts]
    return "".join(out).replace("\n", "<br>")


def strip_html(s: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


class Teams:
    def __init__(self, cfg: Config, auth: GraphAuth | None = None,
                 client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.auth = auth or GraphAuth(cfg)
        self.client = client or httpx.AsyncClient(timeout=30)
        self._me: dict | None = None
        self._chat_ids: dict[str, str] = {}

    async def _req(self, method: str, path: str, **kw: Any) -> dict:
        url = path if path.startswith("http") else GRAPH + path
        token = await asyncio.to_thread(self.auth.token)
        r = await self.client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.status_code == 429:
            await asyncio.sleep(int(r.headers.get("Retry-After", "10")))
            return await self._req(method, path, **kw)
        if r.status_code >= 300:
            raise TeamsError(f"Graph {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    async def me(self) -> dict:
        if not self._me:
            self._me = await self._req("GET", "/me?$select=id,displayName,mail")
        return self._me

    async def find_chat(self, topic: str) -> str:
        if topic in self._chat_ids:
            return self._chat_ids[topic]
        url: str | None = "/me/chats?$top=50&$select=id,topic,chatType"
        while url:
            data = await self._req("GET", url)
            for c in data.get("value", []):
                if (c.get("topic") or "").strip().lower() == topic.strip().lower():
                    self._chat_ids[topic] = c["id"]
                    return c["id"]
            url = data.get("@odata.nextLink")
        raise TeamsError(f"No Teams chat named '{topic}' found in your chats")

    async def members(self, chat_id: str) -> list[dict]:
        return (await self._req("GET", f"/chats/{chat_id}/members")).get("value", [])

    async def post_pr_message(self, html_text: str, mention_names: list[str], plain: str = "") -> None:
        topic = self.cfg.get("teams.pr_chat_topic")
        try:
            chat_id = await self.find_chat(topic)
            members = {m.get("displayName", "").lower(): m for m in await self.members(chat_id)}
            mentions, body = [], html_text
            for i, name in enumerate(mention_names):
                m = members.get(name.lower())
                if not m:  # not found: plain text name instead of a broken mention
                    body = body.replace(f'<at id="{i}">{name}</at>', name)
                    continue
                mentions.append({"id": i, "mentionText": name, "mentioned": {"user": {
                    "id": m["userId"], "displayName": m["displayName"], "userIdentityType": "aadUser"}}})
            await self._req("POST", f"/chats/{chat_id}/messages", json={
                "body": {"contentType": "html", "content": to_teams_html(body)}, "mentions": mentions})
        except TeamsError:
            hook = get_secret("teams_webhook_url")
            if not hook:
                raise
            log.warning("Graph post failed, using webhook fallback")
            r = await self.client.post(hook, json={"text": plain or strip_html(html_text)})
            if r.status_code >= 300:
                raise TeamsError(f"webhook failed: {r.status_code} {r.text[:200]}")

    async def reply(self, chat_id: str, text: str) -> None:
        await self._req("POST", f"/chats/{chat_id}/messages",
                        json={"body": {"contentType": "html", "content": to_teams_html(text)}})

    async def new_messages(self, since: datetime) -> list[dict]:
        """Messages from others in your chats since `since` (1:1, group, meeting chats)."""
        me = await self.me()
        out: list[dict] = []
        data = await self._req("GET", "/me/chats?$expand=lastMessagePreview&$top=50"
                                      "&$orderby=lastMessagePreview/createdDateTime desc")
        for c in data.get("value", []):
            prev = c.get("lastMessagePreview") or {}
            ts = prev.get("createdDateTime")
            if not ts or _dt(ts) <= since:
                continue
            msgs = await self._req("GET", f"/chats/{c['id']}/messages?$top=15")
            for m in msgs.get("value", []):
                sender = ((m.get("from") or {}).get("user") or {})
                if m.get("messageType") != "message" or _dt(m["createdDateTime"]) <= since:
                    continue
                if sender.get("id") == me["id"]:
                    continue
                mentions_me = any(((x.get("mentioned") or {}).get("user") or {}).get("id") == me["id"]
                                  for x in m.get("mentions") or [])
                out.append({"chat_id": c["id"], "chat": c.get("topic") or c.get("chatType"),
                            "chat_type": c.get("chatType"), "id": m["id"],
                            "from": sender.get("displayName", "?"), "at": m["createdDateTime"],
                            "text": strip_html((m.get("body") or {}).get("content", ""))[:2000],
                            "mentions_me": mentions_me, "url": m.get("webUrl", "")})
        return sorted(out, key=lambda x: x["at"])


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["urgent", "needs_reply", "fyi", "ignore"]},
        "why": {"type": "string"},
        "suggested_reply": {"type": "string"},
        "is_task": {"type": "boolean"},
        "task_title": {"type": "string"},
    },
    "required": ["category", "why"],
}


class TeamsWatcher:
    def __init__(self, cfg: Config, teams: Teams, engine, store, notifier, pipeline):
        self.cfg, self.teams, self.engine, self.store = cfg, teams, engine, store
        self.notify, self.pipeline = notifier, pipeline

    def _in_hours(self) -> bool:
        start, end = self.cfg.get("teams.triage.work_hours", ["00:00", "23:59"])
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(self.cfg.get("me.timezone", "Asia/Kolkata"))).strftime("%H:%M")
        return start <= now <= end

    async def run_forever(self) -> None:
        poll = int(self.cfg.get("teams.triage.poll_seconds", 90))
        while True:
            try:
                if self._in_hours():
                    await self.check()
            except Exception as e:  # noqa: BLE001
                log.warning("teams poll failed: %s", e)
            await asyncio.sleep(poll)

    async def check(self) -> None:
        last = self.store.kv_get("teams_last_seen")
        since = _dt(last) if last else datetime.now(timezone.utc) - timedelta(minutes=10)
        msgs = await self.teams.new_messages(since)
        if not msgs:
            return
        self.store.kv_set("teams_last_seen", msgs[-1]["at"])
        tasks = "\n".join(f"#{t['id']} {t['title']} [{t['status']}]" for t in self.store.active_tasks())
        people = "\n".join(f"- {p['name']}: {p['role']}" for p in self.cfg.get("people", []))
        vip = [n.lower() for n in self.cfg.get("teams.triage.always_notify_from", [])]
        for m in msgs:
            r = await self.engine.ask_json(
                "triage", "\n".join([_TRIAGE_PROMPT, "People:", people, "Suni's current tasks:", tasks]),
                f"Chat: {m['chat']} ({m['chat_type']})\nFrom: {m['from']}\n"
                f"Mentions Suni: {m['mentions_me']}\nMessage:\n{m['text']}", TRIAGE_SCHEMA)
            d = r.data or {"category": "needs_reply", "why": "triage failed"}
            important = d["category"] in ("urgent", "needs_reply") or (
                m["from"].split()[0].lower() in vip and d["category"] != "ignore")
            if not important:
                continue
            key = f"tm:{m['chat_id']}:{m['id']}"
            self.store.kv_set(key, d.get("suggested_reply", ""))
            icon = "🚨" if d["category"] == "urgent" else "💬"
            buttons = []
            if d.get("suggested_reply"):
                buttons.append(("Send suggested reply", "reply"))
            if d.get("is_task"):
                self.store.kv_set(key + ":task", d.get("task_title") or m["text"][:70])
                buttons.append(("➕ Make it a task", "task"))
            buttons.append(("Dismiss", "dismiss"))
            await self.notify.confirm(
                key,
                f"{icon} <b>{htmllib.escape(m['from'])}</b> in <i>{htmllib.escape(str(m['chat']))}</i>\n"
                f"{htmllib.escape(m['text'][:800])}\n\n<i>{htmllib.escape(d.get('why', ''))}</i>"
                + (f"\n\nSuggested reply:\n<code>{htmllib.escape(d['suggested_reply'])}</code>"
                   if d.get("suggested_reply") else ""),
                buttons)


def _load_triage_prompt() -> str:
    from importlib import resources

    return resources.files("devagent").joinpath("prompts/triage.md").read_text()


_TRIAGE_PROMPT = _load_triage_prompt()
