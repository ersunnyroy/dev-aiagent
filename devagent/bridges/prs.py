"""Pull-request providers: Bitbucket Cloud (work repos) and GitHub (via gh CLI)."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from ..config import Config, Repo, get_secret
from ..gitops import sh

log = logging.getLogger("devagent.prs")


class PRError(RuntimeError):
    pass


class BitbucketPRs:
    API = "https://api.bitbucket.org/2.0"

    def __init__(self, cfg: Config, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client

    def _auth(self) -> dict:
        email, token = get_secret("bitbucket_email"), get_secret("bitbucket_api_token")
        if not token:
            raise PRError("Bitbucket token missing: devagent secrets set bitbucket_api_token")
        # Atlassian API token → basic auth with email; repo/workspace access token → bearer
        if email:
            return {"auth": (email, token)}
        return {"headers": {"Authorization": f"Bearer {token}"}}

    async def create(self, repo: Repo, branch: str, title: str, description: str) -> str:
        base = self.cfg.base_branch
        body = {
            "title": title[:255],
            "description": description,
            "source": {"branch": {"name": branch}},
            "destination": {"branch": {"name": base}},
            "close_source_branch": bool(self.cfg.get("bitbucket.close_source_branch", True)),
            "reviewers": [{"uuid": u} for u in self.cfg.get("bitbucket.default_reviewers", []) or []],
        }
        url = f"{self.API}/repositories/{repo.workspace}/{repo.slug}/pullrequests"
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            r = await client.post(url, json=body, **self._auth())
            if r.status_code == 400 and "already" in r.text.lower():
                return await self._existing(client, repo, branch)
            if r.status_code >= 300:
                raise PRError(f"Bitbucket {repo.remote}: {r.status_code} {r.text[:300]}")
            data = r.json()
            return self._diff_url(data)
        finally:
            if not self._client:
                await client.aclose()

    async def _existing(self, client: httpx.AsyncClient, repo: Repo, branch: str) -> str:
        url = f"{self.API}/repositories/{repo.workspace}/{repo.slug}/pullrequests"
        r = await client.get(url, params={"q": f'source.branch.name="{branch}" AND state="OPEN"'},
                             **self._auth())
        vals = r.json().get("values", []) if r.status_code < 300 else []
        if not vals:
            raise PRError(f"PR exists for {branch} but could not be found")
        return self._diff_url(vals[0])

    @staticmethod
    def _diff_url(pr: dict) -> str:
        html = pr["links"]["html"]["href"]          # …/pull-requests/249
        return html.rstrip("/") + "/diff"           # match Suni's Teams format


class GitHubPRs:
    """Uses the gh CLI; set GH_TOKEN/`gh auth login` for the ersunnyroy account."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def create(self, repo: Repo, branch: str, title: str, description: str,
                     cwd: Path | None = None) -> str:
        r = await sh(["gh", "pr", "create", "--repo", repo.remote, "--base", self.cfg.base_branch,
                      "--head", branch, "--title", title, "--body", description], cwd=cwd, check=False)
        if r.code != 0:
            if "already exists" in r.err:
                return r.err.strip().split()[-1]
            raise PRError(f"gh pr create failed: {r.err.strip()}")
        return r.out.strip().splitlines()[-1]


class PRRouter:
    def __init__(self, cfg: Config):
        self.providers = {"bitbucket": BitbucketPRs(cfg), "github": GitHubPRs(cfg)}

    async def create(self, repo: Repo, branch: str, title: str, description: str,
                     cwd: Path | None = None) -> str:
        p = self.providers.get(repo.provider)
        if not p:
            raise PRError(f"Unsupported provider {repo.provider}")
        if isinstance(p, GitHubPRs):
            return await p.create(repo, branch, title, description, cwd)
        return await p.create(repo, branch, title, description)
