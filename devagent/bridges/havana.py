"""Havana portal — PROVISION ONLY. Wire up once the API docs are available.

Expected contract (adjust to the real API):
  fetch_assigned()  -> list of {"id", "title", "description", "priority"} assigned to Suni
  update_status(task, status, pr_links) -> push status/PR links back to the Havana ticket
The daemon only constructs this when `havana.enabled: true`.
"""
from __future__ import annotations

import logging

import httpx

from ..config import Config, get_secret

log = logging.getLogger("devagent.havana")


class Havana:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = (cfg.get("havana.base_url") or "").rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {get_secret('havana_token') or ''}"}

    async def fetch_assigned(self) -> list[dict]:
        # TODO(havana): replace with the real endpoint, e.g. GET {base}/api/tasks?assignee=me
        raise NotImplementedError("Havana integration pending API details")

    async def update_status(self, task: dict, status: str, pr_links: list[dict]) -> None:
        if not task.get("source") == "havana" or not self.base:
            return
        # TODO(havana): e.g. PATCH {base}/api/tasks/{task['source_ref']} {"status": status, "links": …}
        log.info("havana update skipped (not implemented): %s → %s", task.get("source_ref"), status)

    async def sync_into(self, pipeline) -> int:
        """Import new Havana tasks into the pipeline (dedupe by source_ref)."""
        items = await self.fetch_assigned()
        known = {t.get("source_ref") for t in pipeline.store.tasks() if t.get("source") == "havana"}
        n = 0
        for it in items:
            if str(it["id"]) in known:
                continue
            pipeline.add_task(it["title"], it.get("description", ""), source="havana",
                              priority=int(it.get("priority", 3)), source_ref=str(it["id"]))
            n += 1
        return n

    async def aclose(self) -> None:
        pass


__all__ = ["Havana", "httpx"]
