"""Chaves de API (opacas, só o hash é guardado) e sessões do console (JWT curto)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

API_KEY_PREFIX = "llmgw_"
SESSION_AUDIENCE = "llmgw-console"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_secret(secret: str, digest: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), digest)


def make_token(prefix: str, ident: uuid.UUID) -> tuple[str, str]:
    """Gera `<prefixo><id>.<segredo>`; devolve (token, hash do segredo)."""
    secret = secrets.token_urlsafe(32)
    return f"{prefix}{ident.hex}.{secret}", hash_secret(secret)


def parse_token(token: str, prefix: str) -> tuple[uuid.UUID, str] | None:
    if not token or not token.startswith(prefix):
        return None
    ident, _, secret = token[len(prefix) :].partition(".")
    if not secret:
        return None
    try:
        return uuid.UUID(ident), secret
    except ValueError:
        return None


def display_key(key_id: uuid.UUID, last4: str) -> str:
    """Forma exibida no console: `llmgw_1a2b3c4d…wxyz`."""
    return f"{API_KEY_PREFIX}{key_id.hex[:8]}…{last4}"


@dataclass
class SessionClaims:
    user_id: uuid.UUID
    username: str
    session_version: int
    expires_at: datetime


class SessionIssuer:
    """JWT HS256 assinado com a chave guardada no Vault (`<prefixo>/_system/keys`)."""

    def __init__(self, key: str):
        self._key = key

    def issue(
        self, user_id: uuid.UUID, username: str, session_version: int, ttl: timedelta
    ) -> tuple[str, datetime]:
        now = datetime.now(UTC)
        expires = now + ttl
        payload = {
            "sub": str(user_id),
            "aud": SESSION_AUDIENCE,
            "iat": now,
            "exp": expires,
            "jti": secrets.token_hex(8),
            "usr": username,
            "sv": session_version,
        }
        return jwt.encode(payload, self._key, algorithm="HS256"), expires

    def verify(self, token: str) -> SessionClaims:
        data = jwt.decode(token, self._key, algorithms=["HS256"], audience=SESSION_AUDIENCE)
        return SessionClaims(
            user_id=uuid.UUID(data["sub"]),
            username=data.get("usr", ""),
            session_version=int(data.get("sv", 0)),
            expires_at=datetime.fromtimestamp(data["exp"], UTC),
        )
