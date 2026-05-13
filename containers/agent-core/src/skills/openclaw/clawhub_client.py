"""HTTP client for the ClawHub skill registry (clawhub.ai)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import structlog

from .loader import get_skills_dir, load_skill
from .models import OpenClawSkill

logger = structlog.get_logger()

BASE_URL = "https://clawhub.ai"
TIMEOUT = 30.0


class ClawHubClient:
    """Interact with the ClawHub public registry."""

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search for skills on ClawHub. Returns list of {slug, name, description}."""
        url = f"{self.base_url}/api/v1/search"
        params = {"q": query, "limit": str(limit)}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                # Normalize response — ClawHub returns different formats
                results = []
                items = data if isinstance(data, list) else data.get("results", data.get("skills", []))
                for item in items[:limit]:
                    results.append({
                        "slug": item.get("slug", item.get("name", "")),
                        "name": item.get("displayName", item.get("name", item.get("slug", ""))),
                        "description": item.get("summary", item.get("description", "")),
                        "version": item.get("latestVersion", item.get("version", "")),
                        "stars": item.get("stars", item.get("starCount", 0)),
                    })
                return results
        except Exception as e:
            logger.error("clawhub.search_error", query=query, error=str(e))
            return []

    async def resolve(self, slug: str) -> dict | None:
        """Resolve skill metadata by slug."""
        url = f"{self.base_url}/api/v1/resolve"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(url, params={"slug": slug})
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error("clawhub.resolve_error", slug=slug, error=str(e))
            return None

    async def download(self, slug: str, version: str = "latest") -> OpenClawSkill | None:
        """Download and install a skill from ClawHub.

        Downloads the zip, extracts to /data/skills/<slug>/, returns parsed skill.
        """
        url = f"{self.base_url}/api/v1/download"
        params = {"slug": slug}
        if version and version != "latest":
            params["version"] = version

        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()

                # Extract zip to skills directory
                skills_dir = get_skills_dir()
                skill_dir = skills_dir / slug
                skill_dir.mkdir(parents=True, exist_ok=True)

                # Mark as clawhub-sourced
                clawhub_meta = skill_dir / ".clawhub"
                clawhub_meta.mkdir(exist_ok=True)
                (clawhub_meta / "origin.json").write_text(
                    f'{{"slug": "{slug}", "version": "{version}"}}'
                )

                # Extract zip contents
                content = resp.content
                if content[:2] == b"PK":  # ZIP magic bytes
                    with zipfile.ZipFile(io.BytesIO(content)) as zf:
                        # Extract all files, flattening if needed
                        for info in zf.infolist():
                            if info.is_dir():
                                continue
                            # Handle nested directories (some zips have slug/ prefix)
                            name = info.filename
                            parts = Path(name).parts
                            if len(parts) > 1 and parts[0] == slug:
                                name = str(Path(*parts[1:]))
                            target = skill_dir / name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(zf.read(info))
                else:
                    # Not a zip — might be raw SKILL.md content
                    (skill_dir / "SKILL.md").write_bytes(content)

                logger.info("clawhub.installed", slug=slug, path=str(skill_dir))

                # Load and return the installed skill
                return load_skill(skill_dir)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning("clawhub.not_found", slug=slug)
                return None
            logger.error("clawhub.download_error", slug=slug, status=e.response.status_code)
            return None
        except Exception as e:
            logger.error("clawhub.download_error", slug=slug, error=str(e))
            return None


# Singleton
_client: ClawHubClient | None = None


def get_client() -> ClawHubClient:
    global _client
    if _client is None:
        _client = ClawHubClient()
    return _client
