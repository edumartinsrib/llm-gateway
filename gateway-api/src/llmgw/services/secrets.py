"""Cofre de segredos: Vault (KV v2) em produção, memória em testes.

Mesmo desenho do Orquestrador. O Postgres guarda só o caminho; o segredo fica em:

- `<prefixo>/providers/<nome>`: credencial do Azure de cada provedor (Foundry);
- `<prefixo>/_system/keys`: chave que assina as sessões do console (gerada no 1º boot);
- `<prefixo>/_system/ldap`: senha da conta de serviço do LDAP (opcional).

Com AppRole, o token expira; a leitura refaz o login uma vez quando o Vault responde 403.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Protocol

from llmgw.settings import Settings

log = logging.getLogger(__name__)


class SecretStore(Protocol):
    async def read(self, path: str) -> dict[str, Any] | None: ...
    async def write(self, path: str, data: dict[str, Any]) -> None: ...
    async def delete(self, path: str) -> None: ...


class MemorySecretStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def read(self, path: str) -> dict[str, Any] | None:
        value = self._data.get(path)
        return dict(value) if value is not None else None

    async def write(self, path: str, data: dict[str, Any]) -> None:
        self._data[path] = dict(data)

    async def delete(self, path: str) -> None:
        self._data.pop(path, None)


class VaultSecretStore:
    def __init__(self, settings: Settings):
        import hvac

        self._settings = settings
        self._mount = settings.vault_mount
        self._lock = threading.Lock()
        self._client = hvac.Client(
            url=settings.vault_addr,
            token=settings.vault_token,
            namespace=settings.vault_namespace,
        )
        self._approle = bool(not settings.vault_token and settings.vault_role_id)
        if self._approle:
            self._login()

    def _login(self) -> None:
        with self._lock:
            self._client.auth.approle.login(
                role_id=self._settings.vault_role_id, secret_id=self._settings.vault_secret_id
            )

    def _call(self, fn, *args, **kwargs):
        from hvac.exceptions import Forbidden, Unauthorized

        try:
            return fn(*args, **kwargs)
        except (Forbidden, Unauthorized):
            if not self._approle:
                raise
            log.info("token do Vault expirado; refazendo o login AppRole")
            self._login()
            return fn(*args, **kwargs)

    def _read(self, path: str) -> dict[str, Any] | None:
        from hvac.exceptions import InvalidPath

        try:
            resp = self._call(
                self._client.secrets.kv.v2.read_secret_version,
                path=path,
                mount_point=self._mount,
                raise_on_deleted_version=True,
            )
        except InvalidPath:
            return None
        return resp["data"]["data"]

    async def read(self, path: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._read, path)

    async def write(self, path: str, data: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self._call,
            self._client.secrets.kv.v2.create_or_update_secret,
            path=path,
            secret=data,
            mount_point=self._mount,
        )

    async def delete(self, path: str) -> None:
        await asyncio.to_thread(
            self._call,
            self._client.secrets.kv.v2.delete_metadata_and_all_versions,
            path=path,
            mount_point=self._mount,
        )


def build_secret_store(settings: Settings) -> SecretStore:
    if settings.secret_store == "memory" or not settings.vault_addr:
        log.warning("Usando cofre em memória: segredos não sobrevivem a reinício")
        return MemorySecretStore()
    return VaultSecretStore(settings)


def provider_path(settings: Settings, name: str) -> str:
    return f"{settings.vault_prefix}/providers/{name}"


def system_path(settings: Settings, name: str = "keys") -> str:
    return f"{settings.vault_prefix}/_system/{name}"
