"""Twitter/X connector — Twitter API v2.

Secrets:
    bearer_token    — App-only Bearer Token (for read-only actions)
    api_key         — API Key (Consumer Key)
    api_secret      — API Key Secret
    access_token    — OAuth 1.0a access token (for write actions)
    access_secret   — OAuth 1.0a access token secret

Actions:
    post_tweet      — Post a new tweet                                 (MEDIUM)
    search_tweets   — Search recent tweets (last 7 days)               (LOW)
    get_user        — Get user profile by username or ID               (LOW)
    get_timeline    — Get user's recent tweets                         (LOW)
    like_tweet      — Like a tweet                                     (MEDIUM)
    unlike_tweet    — Remove like from a tweet                         (MEDIUM)
    retweet         — Retweet a tweet                                  (MEDIUM)
    delete_tweet    — Delete one of your tweets                        (HIGH)
    get_tweet       — Get a specific tweet by ID                       (LOW)
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
import uuid
from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_API = "https://api.twitter.com/2"
_TIMEOUT = 15.0


class TwitterConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="twitter", version="1.0.0", name="Twitter / X", category="social",
            description="Post tweets, search, and interact with Twitter/X via API v2.",
            capabilities=["post_tweets", "search_tweets", "user_profiles", "timelines", "likes", "retweets"],
            risk_level=RiskLevel.HIGH,
            required_secrets=["bearer_token"],
            config_schema={},
            rate_limits={
                "post_tweet":    RateLimit(requests_per_minute=10, requests_per_hour=50),
                "search_tweets": RateLimit(requests_per_minute=15),
                "get_user":      RateLimit(requests_per_minute=15),
                "get_timeline":  RateLimit(requests_per_minute=15),
                "like_tweet":    RateLimit(requests_per_minute=20),
                "unlike_tweet":  RateLimit(requests_per_minute=20),
                "retweet":       RateLimit(requests_per_minute=10),
                "delete_tweet":  RateLimit(requests_per_minute=10),
                "get_tweet":     RateLimit(requests_per_minute=30),
            },
            actions=[
                ActionSpec(id="post_tweet", description="Post a new tweet",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("text", "string", "Tweet text (max 280 chars)", required=True),
                        ParamSpec("reply_to_tweet_id", "string", "Tweet ID to reply to", required=False),
                        ParamSpec("quote_tweet_id", "string", "Tweet ID to quote", required=False),
                    ]),
                ActionSpec(id="search_tweets", description="Search recent tweets (last 7 days, free tier)",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("query", "string", "Search query (Twitter query syntax)", required=True),
                        ParamSpec("limit", "integer", "Max results (default 10, max 100)", required=False),
                    ]),
                ActionSpec(id="get_user", description="Get user profile by username or ID",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("username", "string", "Twitter @username (without @)", required=False),
                        ParamSpec("user_id", "string", "Twitter user ID", required=False),
                    ]),
                ActionSpec(id="get_timeline", description="Get a user's recent tweets",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("user_id", "string", "Twitter user ID", required=True),
                        ParamSpec("limit", "integer", "Max tweets (default 10)", required=False),
                    ]),
                ActionSpec(id="like_tweet", description="Like a tweet",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("tweet_id", "string", "Tweet ID to like", required=True),
                        ParamSpec("user_id", "string", "Your Twitter user ID", required=True),
                    ]),
                ActionSpec(id="unlike_tweet", description="Remove a like from a tweet",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("tweet_id", "string", "Tweet ID to unlike", required=True),
                        ParamSpec("user_id", "string", "Your Twitter user ID", required=True),
                    ]),
                ActionSpec(id="retweet", description="Retweet a tweet",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("tweet_id", "string", "Tweet ID to retweet", required=True),
                        ParamSpec("user_id", "string", "Your Twitter user ID", required=True),
                    ]),
                ActionSpec(id="delete_tweet", description="Delete one of your tweets",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[ParamSpec("tweet_id", "string", "Tweet ID to delete", required=True)]),
                ActionSpec(id="get_tweet", description="Get a specific tweet by ID",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("tweet_id", "string", "Tweet ID", required=True)]),
            ],
            homepage="https://twitter.com",
            docs_url="https://developer.twitter.com/en/docs/twitter-api",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        bearer = secrets.get("bearer_token", "")
        if not bearer:
            return self.err("bearer_token not configured")
        bearer_h = {"Authorization": f"Bearer {bearer}"}

        if action == "search_tweets": return await self._search(params, bearer_h)
        if action == "get_user":      return await self._get_user(params, bearer_h)
        if action == "get_timeline":  return await self._get_timeline(params, bearer_h)
        if action == "get_tweet":     return await self._get_tweet(params, bearer_h)

        # Write actions require OAuth 1.0a
        oauth_h = self._oauth_header(secrets)
        if not oauth_h:
            return self.err("api_key, api_secret, access_token, access_secret required for write actions")

        if action == "post_tweet":   return await self._post_tweet(params, bearer_h, secrets)
        if action == "like_tweet":   return await self._like(params, bearer_h, secrets, like=True)
        if action == "unlike_tweet": return await self._like(params, bearer_h, secrets, like=False)
        if action == "retweet":      return await self._retweet(params, bearer_h, secrets)
        if action == "delete_tweet": return await self._delete_tweet(params, bearer_h, secrets)
        return self.err(f"Unknown action: {action}")

    def _oauth_header(self, secrets: dict) -> dict | None:
        if not all(secrets.get(k) for k in ("api_key", "api_secret", "access_token", "access_secret")):
            return None
        return {}  # placeholder — real OAuth 1.0a signing below

    def _sign_request(self, method: str, url: str, params: dict, secrets: dict) -> str:
        """Generate OAuth 1.0a Authorization header."""
        oauth_params = {
            "oauth_consumer_key":     secrets["api_key"],
            "oauth_nonce":            uuid.uuid4().hex,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp":        str(int(time.time())),
            "oauth_token":            secrets["access_token"],
            "oauth_version":          "1.0",
        }
        all_params = {**params, **oauth_params}
        sorted_params = "&".join(f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
                                  for k, v in sorted(all_params.items()))
        base = "&".join([method.upper(), urllib.parse.quote(url, safe=""), urllib.parse.quote(sorted_params, safe="")])
        signing_key = f"{urllib.parse.quote(secrets['api_secret'], safe='')}&{urllib.parse.quote(secrets['access_secret'], safe='')}"
        sig = base64_encode_hmac_sha1(signing_key.encode(), base.encode())
        oauth_params["oauth_signature"] = sig
        header = "OAuth " + ", ".join(f'{k}="{urllib.parse.quote(str(v), safe="")}"'
                                       for k, v in sorted(oauth_params.items()))
        return header

    async def _post_tweet(self, p: dict, bearer_h: dict, secrets: dict) -> dict:
        body: dict[str, Any] = {"text": p["text"][:280]}
        if p.get("reply_to_tweet_id"):
            body["reply"] = {"in_reply_to_tweet_id": p["reply_to_tweet_id"]}
        if p.get("quote_tweet_id"):
            body["quote_tweet_id"] = p["quote_tweet_id"]
        url = f"{_API}/tweets"
        auth_header = self._sign_request("POST", url, {}, secrets)
        headers = {"Authorization": auth_header, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body, headers=headers)
        if r.status_code == 201:
            d = r.json()
            return self.ok({"tweet_id": d["data"]["id"], "text": d["data"]["text"]})
        return self.err(f"Twitter {r.status_code}: {r.text[:300]}")

    async def _search(self, p: dict, h: dict) -> dict:
        limit = min(int(p.get("limit") or 10), 100)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/tweets/search/recent", headers=h,
                params={"query": p["query"], "max_results": limit,
                        "tweet.fields": "created_at,author_id,public_metrics"})
        if r.status_code == 200:
            d = r.json()
            tweets = [{"id": t["id"], "text": t["text"],
                "created_at": t.get("created_at"),
                "likes": t.get("public_metrics", {}).get("like_count"),
                "retweets": t.get("public_metrics", {}).get("retweet_count")}
                for t in d.get("data", [])]
            return self.ok({"tweets": tweets, "count": len(tweets)})
        return self.err(f"Twitter {r.status_code}: {r.text[:200]}")

    async def _get_user(self, p: dict, h: dict) -> dict:
        if p.get("username"):
            url = f"{_API}/users/by/username/{p['username']}"
        elif p.get("user_id"):
            url = f"{_API}/users/{p['user_id']}"
        else:
            return self.err("Provide username or user_id")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url, headers=h, params={"user.fields": "name,username,description,public_metrics"})
        if r.status_code == 200:
            d = r.json().get("data", {})
            return self.ok(d)
        return self.err(f"Twitter {r.status_code}")

    async def _get_timeline(self, p: dict, h: dict) -> dict:
        limit = min(int(p.get("limit") or 10), 100)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/users/{p['user_id']}/tweets", headers=h,
                params={"max_results": limit, "tweet.fields": "created_at,public_metrics"})
        if r.status_code == 200:
            tweets = [{"id": t["id"], "text": t["text"], "created_at": t.get("created_at")}
                for t in r.json().get("data", [])]
            return self.ok({"tweets": tweets, "count": len(tweets)})
        return self.err(f"Twitter {r.status_code}")

    async def _get_tweet(self, p: dict, h: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/tweets/{p['tweet_id']}", headers=h,
                params={"tweet.fields": "created_at,author_id,public_metrics"})
        if r.status_code == 200:
            return self.ok(r.json().get("data", {}))
        return self.err(f"Twitter {r.status_code}")

    async def _like(self, p: dict, h: dict, secrets: dict, like: bool) -> dict:
        user_id  = p["user_id"]
        tweet_id = p["tweet_id"]
        url = f"{_API}/users/{user_id}/likes"
        auth_header = self._sign_request("POST" if like else "DELETE", url, {}, secrets)
        headers = {"Authorization": auth_header, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            if like:
                r = await c.post(url, json={"tweet_id": tweet_id}, headers=headers)
            else:
                r = await c.delete(f"{url}/{tweet_id}", headers=headers)
        if r.status_code in (200, 201):
            return self.ok(r.json().get("data", {}))
        return self.err(f"Twitter {r.status_code}: {r.text[:200]}")

    async def _retweet(self, p: dict, h: dict, secrets: dict) -> dict:
        url = f"{_API}/users/{p['user_id']}/retweets"
        auth_header = self._sign_request("POST", url, {}, secrets)
        headers = {"Authorization": auth_header, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json={"tweet_id": p["tweet_id"]}, headers=headers)
        if r.status_code in (200, 201):
            return self.ok(r.json().get("data", {}))
        return self.err(f"Twitter {r.status_code}: {r.text[:200]}")

    async def _delete_tweet(self, p: dict, h: dict, secrets: dict) -> dict:
        url = f"{_API}/tweets/{p['tweet_id']}"
        auth_header = self._sign_request("DELETE", url, {}, secrets)
        headers = {"Authorization": auth_header}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.delete(url, headers=headers)
        if r.status_code == 200:
            return self.ok({"deleted": True, "tweet_id": p["tweet_id"]})
        return self.err(f"Twitter {r.status_code}: {r.text[:200]}")


def base64_encode_hmac_sha1(key: bytes, msg: bytes) -> str:
    import base64
    return base64.b64encode(hmac.new(key, msg, hashlib.sha1).digest()).decode()
