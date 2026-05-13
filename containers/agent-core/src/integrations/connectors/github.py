"""GitHub connector — REST API v3.

Secrets (stored in vault):
    token   — GitHub Personal Access Token (classic or fine-grained)

Actions:
    create_issue       — Open a new issue                               (MEDIUM)
    get_issue          — Fetch issue details                            (LOW)
    list_issues        — List repo issues (open/closed/all)             (LOW)
    create_comment     — Comment on an issue or PR                      (MEDIUM)
    create_pr          — Open a pull request                            (HIGH)
    list_prs           — List open pull requests                        (LOW)
    get_file           — Fetch file content from a repo                 (LOW)
    create_file        — Create or update a file in a repo              (HIGH)
    list_repos         — List repos for authenticated user              (LOW)
    get_repo           — Fetch repository metadata                      (LOW)
    search_code        — Search code across GitHub                      (LOW)
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..base import (
    ActionSpec, BaseConnector, ConnectorManifest,
    ParamSpec, RateLimit, RiskLevel,
)

logger = structlog.get_logger()
_TIMEOUT = 20.0
_API = "https://api.github.com"
_HEADERS_BASE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubConnector(BaseConnector):

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id          = "github",
            version     = "1.0.0",
            name        = "GitHub",
            category    = "productivity",
            description = "Interact with GitHub repos — issues, PRs, files, search.",
            capabilities = [
                "manage_issues",
                "manage_pull_requests",
                "read_write_files",
                "list_repositories",
                "search_code",
            ],
            risk_level       = RiskLevel.HIGH,
            required_secrets = ["token"],
            config_schema    = {},
            rate_limits      = {
                "create_issue":   RateLimit(requests_per_minute=30),
                "get_issue":      RateLimit(requests_per_minute=60),
                "list_issues":    RateLimit(requests_per_minute=30),
                "create_comment": RateLimit(requests_per_minute=30),
                "create_pr":      RateLimit(requests_per_minute=10),
                "list_prs":       RateLimit(requests_per_minute=30),
                "get_file":       RateLimit(requests_per_minute=60),
                "create_file":    RateLimit(requests_per_minute=10),
                "list_repos":     RateLimit(requests_per_minute=20),
                "get_repo":       RateLimit(requests_per_minute=60),
                "search_code":    RateLimit(requests_per_minute=10),
            },
            actions = [
                ActionSpec(
                    id="create_issue", description="Open a new issue in a GitHub repository",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("owner",     "string", "Repository owner (user or org)",    required=True),
                        ParamSpec("repo",      "string", "Repository name",                   required=True),
                        ParamSpec("title",     "string", "Issue title",                       required=True),
                        ParamSpec("body",      "string", "Issue body (markdown)",              required=False),
                        ParamSpec("labels",    "array",  "List of label names",               required=False),
                        ParamSpec("assignees", "array",  "List of GitHub usernames to assign",required=False),
                    ],
                ),
                ActionSpec(
                    id="get_issue", description="Fetch details of a specific issue",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("owner",        "string",  "Repository owner",  required=True),
                        ParamSpec("repo",         "string",  "Repository name",   required=True),
                        ParamSpec("issue_number", "integer", "Issue number",      required=True),
                    ],
                ),
                ActionSpec(
                    id="list_issues", description="List issues in a repository",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("owner",  "string",  "Repository owner",                        required=True),
                        ParamSpec("repo",   "string",  "Repository name",                         required=True),
                        ParamSpec("state",  "string",  "Issue state: open|closed|all (default: open)", required=False),
                        ParamSpec("limit",  "integer", "Max issues to return (default 20)",        required=False),
                    ],
                ),
                ActionSpec(
                    id="create_comment", description="Post a comment on an issue or pull request",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("owner",        "string",  "Repository owner",  required=True),
                        ParamSpec("repo",         "string",  "Repository name",   required=True),
                        ParamSpec("issue_number", "integer", "Issue/PR number",   required=True),
                        ParamSpec("body",         "string",  "Comment body (markdown)", required=True),
                    ],
                ),
                ActionSpec(
                    id="create_pr", description="Open a new pull request",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("owner",  "string", "Repository owner",            required=True),
                        ParamSpec("repo",   "string", "Repository name",             required=True),
                        ParamSpec("title",  "string", "PR title",                   required=True),
                        ParamSpec("body",   "string", "PR description (markdown)",  required=False),
                        ParamSpec("head",   "string", "Branch containing changes",  required=True),
                        ParamSpec("base",   "string", "Target branch (e.g. main)",  required=True),
                        ParamSpec("draft",  "boolean","Open as draft PR",           required=False),
                    ],
                ),
                ActionSpec(
                    id="list_prs", description="List open pull requests in a repository",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("owner", "string",  "Repository owner",               required=True),
                        ParamSpec("repo",  "string",  "Repository name",                required=True),
                        ParamSpec("state", "string",  "PR state: open|closed|all",      required=False),
                        ParamSpec("limit", "integer", "Max PRs to return (default 20)", required=False),
                    ],
                ),
                ActionSpec(
                    id="get_file", description="Fetch the content of a file from a repository",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("owner", "string", "Repository owner",              required=True),
                        ParamSpec("repo",  "string", "Repository name",               required=True),
                        ParamSpec("path",  "string", "File path within the repo",     required=True),
                        ParamSpec("ref",   "string", "Branch/tag/commit (default: repo default branch)", required=False),
                    ],
                ),
                ActionSpec(
                    id="create_file", description="Create or update a file in a repository",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("owner",   "string", "Repository owner",           required=True),
                        ParamSpec("repo",    "string", "Repository name",            required=True),
                        ParamSpec("path",    "string", "File path within the repo",  required=True),
                        ParamSpec("content", "string", "File content (plain text)",  required=True),
                        ParamSpec("message", "string", "Commit message",             required=True),
                        ParamSpec("branch",  "string", "Branch to commit to",        required=False),
                        ParamSpec("sha",     "string", "Existing file SHA (required when updating)", required=False),
                    ],
                ),
                ActionSpec(
                    id="list_repos", description="List repositories for the authenticated user",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("limit", "integer", "Max repos to return (default 30)", required=False),
                        ParamSpec("sort",  "string",  "Sort by: created|updated|pushed|full_name", required=False),
                    ],
                ),
                ActionSpec(
                    id="get_repo", description="Fetch repository metadata",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("owner", "string", "Repository owner", required=True),
                        ParamSpec("repo",  "string", "Repository name",  required=True),
                    ],
                ),
                ActionSpec(
                    id="search_code", description="Search for code across GitHub repositories",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("query", "string",  "Search query (GitHub code search syntax)", required=True),
                        ParamSpec("limit", "integer", "Max results to return (default 10)",        required=False),
                    ],
                ),
            ],
            homepage = "https://github.com",
            docs_url = "https://docs.github.com/en/rest",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        if action == "create_issue":   return await self._create_issue(params, secrets)
        if action == "get_issue":      return await self._get_issue(params, secrets)
        if action == "list_issues":    return await self._list_issues(params, secrets)
        if action == "create_comment": return await self._create_comment(params, secrets)
        if action == "create_pr":      return await self._create_pr(params, secrets)
        if action == "list_prs":       return await self._list_prs(params, secrets)
        if action == "get_file":       return await self._get_file(params, secrets)
        if action == "create_file":    return await self._create_file(params, secrets)
        if action == "list_repos":     return await self._list_repos(params, secrets)
        if action == "get_repo":       return await self._get_repo(params, secrets)
        if action == "search_code":    return await self._search_code(params, secrets)
        return self.err(f"Unknown action: {action}")

    # ------------------------------------------------------------------

    def _headers(self, token: str) -> dict:
        return {**_HEADERS_BASE, "Authorization": f"Bearer {token}"}

    async def _req(self, method: str, path: str, token: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            return await c.request(
                method,
                f"{_API}/{path.lstrip('/')}",
                headers=self._headers(token),
                **kwargs,
            )

    async def _create_issue(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        body: dict[str, Any] = {"title": p.get("title", "")}
        if p.get("body"):      body["body"]      = p["body"]
        if p.get("labels"):    body["labels"]    = p["labels"]
        if p.get("assignees"): body["assignees"] = p["assignees"]
        r = await self._req("POST", f"/repos/{p['owner']}/{p['repo']}/issues", token, json=body)
        if r.status_code == 201:
            d = r.json()
            return self.ok({"number": d["number"], "url": d["html_url"], "title": d["title"]})
        return self.err(f"GitHub {r.status_code}: {r.text[:300]}")

    async def _get_issue(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        r = await self._req("GET", f"/repos/{p['owner']}/{p['repo']}/issues/{p['issue_number']}", token)
        if r.status_code == 200:
            d = r.json()
            return self.ok({
                "number": d["number"],
                "title":  d["title"],
                "state":  d["state"],
                "body":   (d.get("body") or "")[:1000],
                "url":    d["html_url"],
                "author": d["user"]["login"],
                "comments": d["comments"],
            })
        return self.err(f"GitHub {r.status_code}")

    async def _list_issues(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        limit = min(int(p.get("limit") or 20), 100)
        qp = {"state": p.get("state") or "open", "per_page": limit}
        r = await self._req("GET", f"/repos/{p['owner']}/{p['repo']}/issues", token, params=qp)
        if r.status_code == 200:
            issues = [
                {"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"]}
                for i in r.json()
                if "pull_request" not in i  # exclude PRs (GitHub lists them in issues endpoint)
            ]
            return self.ok({"issues": issues, "count": len(issues)})
        return self.err(f"GitHub {r.status_code}")

    async def _create_comment(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        r = await self._req(
            "POST", f"/repos/{p['owner']}/{p['repo']}/issues/{p['issue_number']}/comments",
            token, json={"body": p.get("body", "")}
        )
        if r.status_code == 201:
            d = r.json()
            return self.ok({"comment_id": d["id"], "url": d["html_url"]})
        return self.err(f"GitHub {r.status_code}: {r.text[:300]}")

    async def _create_pr(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        body: dict[str, Any] = {
            "title": p.get("title", ""),
            "head":  p.get("head", ""),
            "base":  p.get("base", "main"),
        }
        if p.get("body"):  body["body"]  = p["body"]
        if p.get("draft"): body["draft"] = bool(p["draft"])
        r = await self._req("POST", f"/repos/{p['owner']}/{p['repo']}/pulls", token, json=body)
        if r.status_code == 201:
            d = r.json()
            return self.ok({"number": d["number"], "url": d["html_url"], "state": d["state"]})
        return self.err(f"GitHub {r.status_code}: {r.text[:300]}")

    async def _list_prs(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        limit = min(int(p.get("limit") or 20), 100)
        qp = {"state": p.get("state") or "open", "per_page": limit}
        r = await self._req("GET", f"/repos/{p['owner']}/{p['repo']}/pulls", token, params=qp)
        if r.status_code == 200:
            prs = [
                {"number": pr["number"], "title": pr["title"], "state": pr["state"],
                 "url": pr["html_url"], "author": pr["user"]["login"]}
                for pr in r.json()
            ]
            return self.ok({"prs": prs, "count": len(prs)})
        return self.err(f"GitHub {r.status_code}")

    async def _get_file(self, p: dict, secrets: dict) -> dict:
        import base64
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        qp = {}
        if p.get("ref"): qp["ref"] = p["ref"]
        r = await self._req("GET", f"/repos/{p['owner']}/{p['repo']}/contents/{p['path']}", token, params=qp)
        if r.status_code == 200:
            d = r.json()
            if d.get("type") != "file":
                return self.err("Path is a directory, not a file")
            content = base64.b64decode(d["content"].replace("\n", "")).decode("utf-8", errors="replace")
            return self.ok({
                "path":    d["path"],
                "sha":     d["sha"],
                "size":    d["size"],
                "content": content[:8000],
                "url":     d["html_url"],
            })
        return self.err(f"GitHub {r.status_code}")

    async def _create_file(self, p: dict, secrets: dict) -> dict:
        import base64
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        encoded = base64.b64encode(p.get("content", "").encode()).decode()
        body: dict[str, Any] = {
            "message": p.get("message", "Update via WASP"),
            "content": encoded,
        }
        if p.get("branch"): body["branch"] = p["branch"]
        if p.get("sha"):    body["sha"]    = p["sha"]
        r = await self._req("PUT", f"/repos/{p['owner']}/{p['repo']}/contents/{p['path']}", token, json=body)
        if r.status_code in (200, 201):
            d = r.json()
            return self.ok({
                "path":   d["content"]["path"],
                "sha":    d["content"]["sha"],
                "commit": d["commit"]["sha"],
                "url":    d["content"]["html_url"],
            })
        return self.err(f"GitHub {r.status_code}: {r.text[:300]}")

    async def _list_repos(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        limit = min(int(p.get("limit") or 30), 100)
        qp = {"per_page": limit, "sort": p.get("sort") or "updated"}
        r = await self._req("GET", "/user/repos", token, params=qp)
        if r.status_code == 200:
            repos = [
                {"name": rp["full_name"], "private": rp["private"],
                 "stars": rp["stargazers_count"], "url": rp["html_url"],
                 "description": rp.get("description") or ""}
                for rp in r.json()
            ]
            return self.ok({"repos": repos, "count": len(repos)})
        return self.err(f"GitHub {r.status_code}")

    async def _get_repo(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        r = await self._req("GET", f"/repos/{p['owner']}/{p['repo']}", token)
        if r.status_code == 200:
            d = r.json()
            return self.ok({
                "full_name":    d["full_name"],
                "description":  d.get("description") or "",
                "stars":        d["stargazers_count"],
                "forks":        d["forks_count"],
                "open_issues":  d["open_issues_count"],
                "default_branch": d["default_branch"],
                "url":          d["html_url"],
                "private":      d["private"],
                "language":     d.get("language"),
            })
        return self.err(f"GitHub {r.status_code}")

    async def _search_code(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        limit = min(int(p.get("limit") or 10), 30)
        r = await self._req("GET", "/search/code", token, params={"q": p.get("query", ""), "per_page": limit})
        if r.status_code == 200:
            items = [
                {"name": i["name"], "path": i["path"], "repo": i["repository"]["full_name"], "url": i["html_url"]}
                for i in r.json().get("items", [])
            ]
            return self.ok({"items": items, "count": len(items)})
        return self.err(f"GitHub {r.status_code}: {r.text[:200]}")
