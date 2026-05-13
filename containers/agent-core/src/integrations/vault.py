"""Encrypted secret vault backed by Redis.

All integration secrets are encrypted at rest using Fernet
(AES-128-CBC + HMAC-SHA256).  The encryption key is derived from
DASHBOARD_SECRET via PBKDF2-HMAC-SHA256.  A random 16-byte salt is
generated on first initialisation and stored in Redis.

Security model:
    - LLM NEVER calls vault methods directly
    - The IntegrationRegistry resolves secrets and passes them only to
      connector.execute() as an opaque dict
    - Secret values are never written to logs or audit trail
    - Key rotation: change DASHBOARD_SECRET + call rotate_key()
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

_VAULT_NS  = "vault:integrations:"   # Redis hash key prefix per integration
_SALT_KEY  = "vault:salt"            # Redis key for PBKDF2 salt
_PBKDF2_IT = 100_000                 # PBKDF2 iteration count


def _derive_key(secret: str, salt: bytes) -> bytes:
    """Derive a 32-byte URL-safe base64-encoded key for Fernet."""
    raw = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        iterations=_PBKDF2_IT,
        dklen=32,
    )
    return base64.urlsafe_b64encode(raw)


class SecretVault:
    """Encrypted key-value store for integration secrets.

    Usage:
        vault = SecretVault(redis_url, master_secret)
        await vault.set("discord", "bot_token", "Bot xxx")
        token = await vault.get("discord", "bot_token")   # decrypted
        secrets = await vault.get_all("discord")          # all decrypted
    """

    def __init__(self, redis_url: str, master_secret: str) -> None:
        self._redis_url     = redis_url
        self._master_secret = master_secret
        self._fernet_cache: Optional[object] = None  # lazy-init

    # ------------------------------------------------------------------
    # Public CRUD
    # ------------------------------------------------------------------

    async def set(self, integration_id: str, secret_key: str, value: str) -> None:
        f = await self._get_fernet()
        encrypted = f.encrypt(value.encode("utf-8"))
        r = aioredis.from_url(self._redis_url, decode_responses=False)
        try:
            await r.hset(
                f"{_VAULT_NS}{integration_id}",
                secret_key.encode(),
                encrypted,
            )
            logger.info("vault.secret_stored", integration=integration_id, key=secret_key)
        finally:
            await r.aclose()

    async def get(self, integration_id: str, secret_key: str) -> Optional[str]:
        f = await self._get_fernet()
        r = aioredis.from_url(self._redis_url, decode_responses=False)
        try:
            encrypted = await r.hget(
                f"{_VAULT_NS}{integration_id}",
                secret_key.encode(),
            )
            if not encrypted:
                return None
            return f.decrypt(encrypted).decode("utf-8")
        except Exception:
            logger.error("vault.decrypt_failed", integration=integration_id, key=secret_key)
            return None
        finally:
            await r.aclose()

    async def get_all(self, integration_id: str) -> dict[str, str]:
        """Resolve and decrypt all secrets for an integration."""
        f = await self._get_fernet()
        r = aioredis.from_url(self._redis_url, decode_responses=False)
        try:
            raw = await r.hgetall(f"{_VAULT_NS}{integration_id}")
            result: dict[str, str] = {}
            for k, v in raw.items():
                try:
                    result[k.decode()] = f.decrypt(v).decode("utf-8")
                except Exception:
                    logger.warning(
                        "vault.skip_bad_entry",
                        integration=integration_id,
                        key=k,
                    )
            return result
        finally:
            await r.aclose()

    async def delete(self, integration_id: str, secret_key: str) -> None:
        r = aioredis.from_url(self._redis_url, decode_responses=False)
        try:
            await r.hdel(f"{_VAULT_NS}{integration_id}", secret_key.encode())
        finally:
            await r.aclose()

    async def delete_all(self, integration_id: str) -> None:
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            await r.delete(f"{_VAULT_NS}{integration_id}")
        finally:
            await r.aclose()

    async def list_keys(self, integration_id: str) -> list[str]:
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            return await r.hkeys(f"{_VAULT_NS}{integration_id}")
        finally:
            await r.aclose()

    async def has_all_required(
        self, integration_id: str, required: list[str]
    ) -> tuple[bool, list[str]]:
        """Return (all_present, missing_keys)."""
        existing = set(await self.list_keys(integration_id))
        missing = [k for k in required if k not in existing]
        return len(missing) == 0, missing

    # ------------------------------------------------------------------
    # Key rotation
    # ------------------------------------------------------------------

    async def rotate_key(self, new_master_secret: str) -> int:
        """Re-encrypt all vault entries with a new master secret.

        Returns the number of entries rotated.
        """
        old_fernet = await self._get_fernet()
        r = aioredis.from_url(self._redis_url, decode_responses=False)
        try:
            # Scan all vault keys
            cursor = 0
            all_keys: list[str] = []
            while True:
                cursor, keys = await r.scan(cursor, match=f"{_VAULT_NS}*", count=100)
                all_keys.extend(k.decode() for k in keys)
                if cursor == 0:
                    break

            # Derive new key from new salt
            new_salt = os.urandom(16)
            new_key = _derive_key(new_master_secret, new_salt)
            from cryptography.fernet import Fernet
            new_fernet = Fernet(new_key)

            count = 0
            for redis_key in all_keys:
                raw = await r.hgetall(redis_key)
                for k, v in raw.items():
                    try:
                        plaintext = old_fernet.decrypt(v)
                        re_encrypted = new_fernet.encrypt(plaintext)
                        await r.hset(redis_key, k, re_encrypted)
                        count += 1
                    except Exception:
                        pass

            await r.set(_SALT_KEY, new_salt)
            self._master_secret = new_master_secret
            self._fernet_cache = new_fernet
            logger.info("vault.key_rotated", entries=count)
            return count
        finally:
            await r.aclose()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get_fernet(self):
        """Return Fernet instance, initialising on first call."""
        if self._fernet_cache is not None:
            return self._fernet_cache

        from cryptography.fernet import Fernet

        r = aioredis.from_url(self._redis_url, decode_responses=False)
        try:
            salt = await r.get(_SALT_KEY)
            if not salt:
                salt = os.urandom(16)
                await r.set(_SALT_KEY, salt)
            key = _derive_key(self._master_secret, salt)
            self._fernet_cache = Fernet(key)
            return self._fernet_cache
        finally:
            await r.aclose()
