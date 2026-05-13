"""Image generation connector — wraps OpenAI DALL-E 3 and Stable Diffusion APIs.

Secrets:
    openai_api_key      — OpenAI API key (for DALL-E 3)
    stability_api_key   — Stability AI API key (for Stable Diffusion, optional)

Actions:
    generate        — Generate image from text prompt (DALL-E 3)       (MEDIUM)
    generate_sd     — Generate image via Stable Diffusion API          (MEDIUM)
    variations      — Generate variations of an existing image         (MEDIUM)
    describe        — Describe an image (vision, requires OpenAI)      (LOW)
"""
from __future__ import annotations

import base64
from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_OAI = "https://api.openai.com/v1"
_SD  = "https://api.stability.ai/v1"
_TIMEOUT = 60.0  # image gen can be slow


class ImageGenConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="image-gen", version="1.0.0", name="Image Generation", category="media",
            description="Generate images from text prompts using DALL-E 3 or Stable Diffusion.",
            capabilities=["text_to_image", "image_variations", "image_description"],
            risk_level=RiskLevel.MEDIUM,
            required_secrets=["openai_api_key"],
            config_schema={},
            rate_limits={
                "generate":    RateLimit(requests_per_minute=5),
                "generate_sd": RateLimit(requests_per_minute=10),
                "variations":  RateLimit(requests_per_minute=5),
                "describe":    RateLimit(requests_per_minute=20),
            },
            actions=[
                ActionSpec(id="generate", description="Generate image from text prompt using DALL-E 3",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("prompt", "string", "Image description prompt", required=True),
                        ParamSpec("size", "string", "1024x1024|1024x1792|1792x1024 (default 1024x1024)", required=False),
                        ParamSpec("quality", "string", "standard|hd (default standard)", required=False),
                        ParamSpec("style", "string", "vivid|natural (default vivid)", required=False),
                        ParamSpec("n", "integer", "Number of images (default 1, DALL-E 3 max 1)", required=False),
                    ]),
                ActionSpec(id="generate_sd", description="Generate image via Stability AI (Stable Diffusion)",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("prompt", "string", "Image description prompt", required=True),
                        ParamSpec("negative_prompt", "string", "What to exclude from the image", required=False),
                        ParamSpec("width", "integer", "Image width (default 512, must be multiple of 64)", required=False),
                        ParamSpec("height", "integer", "Image height (default 512, must be multiple of 64)", required=False),
                        ParamSpec("steps", "integer", "Diffusion steps (default 30)", required=False),
                        ParamSpec("engine", "string", "SD engine ID (default stable-diffusion-xl-1024-v1-0)", required=False),
                    ]),
                ActionSpec(id="variations", description="Generate image variations using DALL-E 2",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("image_url", "string", "Public URL of source image (PNG, square)", required=True),
                        ParamSpec("n", "integer", "Number of variations (1-4, default 2)", required=False),
                        ParamSpec("size", "string", "256x256|512x512|1024x1024 (default 512x512)", required=False),
                    ]),
                ActionSpec(id="describe", description="Describe the content of an image URL using GPT-4o vision",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("image_url", "string", "Public URL of the image to describe", required=True),
                        ParamSpec("question", "string", "Optional question about the image", required=False),
                    ]),
            ],
            homepage="https://openai.com/dall-e-3",
            docs_url="https://platform.openai.com/docs/guides/images",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        oai_key = secrets.get("openai_api_key", "")
        sd_key  = secrets.get("stability_api_key", "")

        if action == "generate":
            if not oai_key: return self.err("openai_api_key not configured")
            return await self._dalle3(params, oai_key)
        if action == "generate_sd":
            if not sd_key: return self.err("stability_api_key not configured")
            return await self._stable_diffusion(params, sd_key)
        if action == "variations":
            if not oai_key: return self.err("openai_api_key not configured")
            return await self._variations(params, oai_key)
        if action == "describe":
            if not oai_key: return self.err("openai_api_key not configured")
            return await self._describe(params, oai_key)
        return self.err(f"Unknown action: {action}")

    async def _dalle3(self, p: dict, key: str) -> dict:
        body: dict[str, Any] = {
            "model": "dall-e-3",
            "prompt": p["prompt"],
            "n": 1,
            "size": p.get("size") or "1024x1024",
            "quality": p.get("quality") or "standard",
            "style": p.get("style") or "vivid",
            "response_format": "url",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{_OAI}/images/generations", json=body,
                headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 200:
            images = r.json().get("data", [])
            return self.ok({
                "images": [{"url": img["url"], "revised_prompt": img.get("revised_prompt", "")}
                            for img in images],
                "count": len(images),
            })
        return self.err(f"OpenAI {r.status_code}: {r.json().get('error', {}).get('message', r.text[:200])}")

    async def _stable_diffusion(self, p: dict, key: str) -> dict:
        engine = p.get("engine") or "stable-diffusion-xl-1024-v1-0"
        width  = max(64, (int(p.get("width") or 512) // 64) * 64)
        height = max(64, (int(p.get("height") or 512) // 64) * 64)
        steps  = min(150, max(10, int(p.get("steps") or 30)))
        text_prompts: list[dict] = [{"text": p["prompt"], "weight": 1.0}]
        if p.get("negative_prompt"):
            text_prompts.append({"text": p["negative_prompt"], "weight": -1.0})
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{_SD}/generation/{engine}/text-to-image",
                json={"text_prompts": text_prompts, "cfg_scale": 7, "steps": steps,
                      "width": width, "height": height, "samples": 1},
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
        if r.status_code == 200:
            artifacts = r.json().get("artifacts", [])
            images = [{"base64": a["base64"], "finish_reason": a.get("finishReason")} for a in artifacts]
            return self.ok({"images": images, "count": len(images), "note": "Images returned as base64 data"})
        return self.err(f"Stability {r.status_code}: {r.text[:200]}")

    async def _variations(self, p: dict, key: str) -> dict:
        n    = min(4, max(1, int(p.get("n") or 2)))
        size = p.get("size") or "512x512"
        # Download source image
        async with httpx.AsyncClient(timeout=30.0) as c:
            img_r = await c.get(p["image_url"])
        if img_r.status_code != 200:
            return self.err(f"Could not download source image: {img_r.status_code}")
        img_bytes = img_r.content
        import io
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{_OAI}/images/variations",
                headers={"Authorization": f"Bearer {key}"},
                files={"image": ("image.png", io.BytesIO(img_bytes), "image/png")},
                data={"n": str(n), "size": size, "response_format": "url"},
            )
        if r.status_code == 200:
            images = [img["url"] for img in r.json().get("data", [])]
            return self.ok({"urls": images, "count": len(images)})
        return self.err(f"OpenAI {r.status_code}: {r.text[:200]}")

    async def _describe(self, p: dict, key: str) -> dict:
        question = p.get("question") or "What's in this image? Describe it in detail."
        body = {
            "model": "gpt-4o",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": p["image_url"]}},
                ],
            }],
            "max_tokens": 500,
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(f"{_OAI}/chat/completions", json=body,
                headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 200:
            text = r.json()["choices"][0]["message"]["content"]
            return self.ok({"description": text})
        return self.err(f"OpenAI {r.status_code}: {r.text[:200]}")
