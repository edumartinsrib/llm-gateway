"""Diretório de usuários: LDAP/Active Directory em produção, estático no modo dev.

Login (`authenticate`):

1. com conta de serviço (`LLMGW_LDAP_BIND_DN`): bind da conta de serviço, busca do
   usuário por `LLMGW_LDAP_USER_FILTER` e bind com o DN encontrado e a senha digitada;
2. sem conta de serviço: bind direto com `LLMGW_LDAP_USER_DN_TEMPLATE`
   (ex.: `uid={username},ou=people,dc=empresa,dc=com` ou `{username}@empresa.local`).

Grupos: atributo `memberOf` do usuário (AD e OpenLDAP com overlay memberof) ou busca
em `LLMGW_LDAP_GROUP_BASE` por `LLMGW_LDAP_GROUP_FILTER`. Com
`LLMGW_LDAP_NESTED_GROUPS=true` (só AD) a busca resolve grupos aninhados.

Senha vazia é recusada antes do bind: no LDAP, bind com senha vazia é um bind
anônimo e "funciona".
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from dataclasses import dataclass, field
from typing import Any, Protocol

from llmgw.settings import Settings

log = logging.getLogger(__name__)


class DirectoryError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class DirectoryUser:
    username: str
    dn: str | None = None
    display_name: str | None = None
    email: str | None = None
    groups: list[str] = field(default_factory=list)  # nomes (CN)
    group_dns: list[str] = field(default_factory=list)  # DNs completos


class Directory(Protocol):
    can_lookup: bool

    async def authenticate(self, username: str, password: str | None) -> DirectoryUser: ...

    async def lookup(self, username: str, dn: str | None = None) -> DirectoryUser | None: ...


class StaticDirectory:
    """Modo dev: qualquer nome entra sem senha. `users` define grupos e senhas para testes."""

    def __init__(self, users: dict[str, dict[str, Any]] | None = None, *, require_password=False):
        self._users = {k.lower(): v for k, v in (users or {}).items()}
        self._require_password = require_password
        self.can_lookup = True

    async def authenticate(self, username: str, password: str | None) -> DirectoryUser:
        name = (username or "").strip().lower()
        if not name:
            raise DirectoryError(401, "informe o usuário")
        entry = self._users.get(name)
        if self._require_password:
            if entry is None or not password or entry.get("password") != password:
                raise DirectoryError(401, "usuário ou senha inválidos")
        return self._user(name, entry or {})

    async def lookup(self, username: str, dn: str | None = None) -> DirectoryUser | None:
        entry = self._users.get(username.lower())
        return self._user(username.lower(), entry) if entry is not None else None

    @staticmethod
    def _user(name: str, entry: dict[str, Any]) -> DirectoryUser:
        return DirectoryUser(
            username=name,
            dn=entry.get("dn"),
            display_name=entry.get("display_name"),
            email=entry.get("email"),
            groups=list(entry.get("groups") or []),
            group_dns=list(entry.get("group_dns") or []),
        )


def _first_value(attrs: dict[str, Any], name: str) -> str | None:
    for key, value in attrs.items():
        if key.lower() == name.lower():
            if isinstance(value, list):
                return str(value[0]) if value else None
            return str(value) if value not in (None, "") else None
    return None


def _all_values(attrs: dict[str, Any], name: str) -> list[str]:
    for key, value in attrs.items():
        if key.lower() == name.lower():
            if isinstance(value, list):
                return [str(v) for v in value]
            return [str(value)] if value else []
    return []


def group_name_from_dn(dn: str) -> str:
    from ldap3.utils.dn import parse_dn

    try:
        return parse_dn(dn)[0][1]
    except Exception:  # noqa: BLE001 - DN fora do padrão: usa o texto antes da 1ª vírgula
        first = dn.split(",", 1)[0]
        return first.partition("=")[2] or dn


class LdapDirectory:
    def __init__(self, settings: Settings, bind_password: str | None):
        if not settings.ldap_url_list:
            raise DirectoryError(503, "LLMGW_LDAP_URL não configurado")
        self._s = settings
        self._bind_password = bind_password
        self.can_lookup = bool(settings.ldap_bind_dn and settings.ldap_user_base)
        self._server = self._build_server()

    def _build_server(self):
        from ldap3 import FIRST, NONE, Server, ServerPool, Tls

        s = self._s
        tls = Tls(
            validate=ssl.CERT_REQUIRED if s.ldap_verify_tls else ssl.CERT_NONE,
            ca_certs_file=s.ldap_ca_file,
        )
        servers = [
            Server(
                url,
                use_ssl=url.lower().startswith("ldaps://"),
                tls=tls,
                connect_timeout=s.ldap_timeout_s,
                get_info=NONE,
            )
            for url in s.ldap_url_list
        ]
        if len(servers) == 1:
            return servers[0]
        return ServerPool(servers, pool_strategy=FIRST, active=1, exhaust=60)

    # ------------------------------------------------------------------ conexões (síncrono)
    def _bind(self, user: str, password: str):
        from ldap3 import Connection
        from ldap3.core.exceptions import LDAPException

        try:
            conn = Connection(
                self._server,
                user=user,
                password=password,
                receive_timeout=self._s.ldap_timeout_s,
                read_only=True,
                auto_referrals=False,
                raise_exceptions=False,
            )
            conn.open()
            if self._s.ldap_start_tls and not getattr(conn.server, "ssl", False):
                conn.start_tls()
            ok = conn.bind()
        except LDAPException as exc:
            log.warning("LDAP indisponível: %s", exc)
            raise DirectoryError(503, f"LDAP indisponível ({type(exc).__name__})") from exc
        if not ok:
            description = (conn.result or {}).get("description")
            conn.unbind()
            if description == "invalidCredentials":
                raise DirectoryError(401, "usuário ou senha inválidos")
            raise DirectoryError(503, f"o LDAP recusou o bind ({description})")
        return conn

    def _service_bind(self):
        if not self._s.ldap_bind_dn:
            raise DirectoryError(503, "conta de serviço do LDAP não configurada")
        try:
            return self._bind(self._s.ldap_bind_dn, self._bind_password or "")
        except DirectoryError as exc:
            if exc.status == 401:
                raise DirectoryError(503, "a conta de serviço do LDAP foi recusada") from exc
            raise

    def _find(self, conn, username: str) -> tuple[str, dict[str, Any]] | None:
        from ldap3 import SUBTREE
        from ldap3.utils.conv import escape_filter_chars

        s = self._s
        flt = s.ldap_user_filter.replace("{username}", escape_filter_chars(username))
        conn.search(
            s.ldap_user_base, flt, search_scope=SUBTREE, attributes=self._attrs(), size_limit=2
        )
        entries = [e for e in (conn.response or []) if e.get("type") == "searchResEntry"]
        if len(entries) != 1:
            if len(entries) > 1:
                log.warning(
                    "filtro de usuário do LDAP devolveu mais de uma entrada para %s", username
                )
            return None
        return entries[0]["dn"], dict(entries[0].get("attributes") or {})

    def _attrs(self) -> list[str]:
        s = self._s
        return list(
            {s.ldap_attr_display_name, s.ldap_attr_email, "memberOf", "uid", "sAMAccountName", "cn"}
        )

    def _read_dn(self, conn, dn: str) -> dict[str, Any] | None:
        """Lê a entrada pelo DN (busca de escopo base); None se o DN não existe mais."""
        from ldap3 import BASE

        conn.search(dn, "(objectClass=*)", search_scope=BASE, attributes=self._attrs())
        result = (conn.result or {}).get("description")
        if result == "noSuchObject":
            return None
        if result not in ("success", None):
            raise DirectoryError(503, f"o LDAP recusou a leitura de {dn} ({result})")
        entries = [e for e in (conn.response or []) if e.get("type") == "searchResEntry"]
        return dict(entries[0].get("attributes") or {}) if entries else None

    def _groups(self, conn, dn: str, attrs: dict[str, Any], username: str) -> list[str]:
        """DNs dos grupos do usuário."""
        from ldap3 import SUBTREE
        from ldap3.utils.conv import escape_filter_chars

        s = self._s
        member_of = _all_values(attrs, "memberOf")
        if s.ldap_group_mode == "memberof" or (s.ldap_group_mode == "auto" and member_of):
            dns = member_of
        elif s.ldap_group_base:
            if s.ldap_nested_groups:
                flt = f"(member:1.2.840.113556.1.4.1941:={escape_filter_chars(dn)})"
            else:
                flt = s.ldap_group_filter.replace("{user_dn}", escape_filter_chars(dn)).replace(
                    "{username}", escape_filter_chars(username)
                )
            conn.search(s.ldap_group_base, flt, search_scope=SUBTREE, attributes=["cn"])
            dns = [e["dn"] for e in (conn.response or []) if e.get("type") == "searchResEntry"]
        else:
            dns = []
        return sorted(dict.fromkeys(d for d in dns if d), key=str.lower)

    def _to_user(self, username: str, dn: str, attrs: dict[str, Any], group_dns: list[str]):
        s = self._s
        canonical = (
            _first_value(attrs, "sAMAccountName") or _first_value(attrs, "uid") or username
        ).lower()
        return DirectoryUser(
            username=canonical,
            dn=dn,
            display_name=_first_value(attrs, s.ldap_attr_display_name) or _first_value(attrs, "cn"),
            email=_first_value(attrs, s.ldap_attr_email),
            groups=sorted(
                dict.fromkeys(n for n in (group_name_from_dn(d) for d in group_dns) if n),
                key=str.lower,
            ),
            group_dns=group_dns,
        )

    def _authenticate(self, username: str, password: str) -> DirectoryUser:
        s = self._s
        if s.ldap_bind_dn:
            svc = self._service_bind()
            try:
                found = self._find(svc, username)
                if found is None:
                    raise DirectoryError(401, "usuário ou senha inválidos")
                dn, attrs = found
                self._bind(dn, password).unbind()
                groups = self._groups(svc, dn, attrs, username)
            finally:
                svc.unbind()
            return self._to_user(username, dn, attrs, groups)
        if s.ldap_user_dn_template:
            from ldap3.utils.dn import escape_rdn

            template = s.ldap_user_dn_template
            value = escape_rdn(username) if "=" in template.split("{username}")[0] else username
            user_dn = template.replace("{username}", value)
            conn = self._bind(user_dn, password)
            try:
                found = self._find(conn, username) if s.ldap_user_base else None
                if found is None:
                    return DirectoryUser(username=username.lower(), dn=user_dn)
                dn, attrs = found
                return self._to_user(username, dn, attrs, self._groups(conn, dn, attrs, username))
            finally:
                conn.unbind()
        raise DirectoryError(
            503, "configure LLMGW_LDAP_BIND_DN ou LLMGW_LDAP_USER_DN_TEMPLATE para o login LDAP"
        )

    def _lookup(self, username: str, dn: str | None) -> DirectoryUser | None:
        svc = self._service_bind()
        try:
            if dn:
                attrs = self._read_dn(svc, dn)
                if attrs is None:
                    return None
            else:
                found = self._find(svc, username)
                if found is None:
                    return None
                dn, attrs = found
            return self._to_user(username, dn, attrs, self._groups(svc, dn, attrs, username))
        finally:
            svc.unbind()

    # ------------------------------------------------------------------ interface assíncrona
    async def authenticate(self, username: str, password: str | None) -> DirectoryUser:
        name = (username or "").strip()
        if not name or not password:
            raise DirectoryError(401, "informe usuário e senha")
        if len(name) > 256 or "\x00" in name or len(password) > 1024:
            raise DirectoryError(401, "usuário ou senha inválidos")
        return await asyncio.to_thread(self._authenticate, name, password)

    async def lookup(self, username: str, dn: str | None = None) -> DirectoryUser | None:
        """Consulta pela conta de serviço; com `dn`, lê a entrada direto pelo DN."""
        if not self.can_lookup:
            raise DirectoryError(
                503, "consulta ao LDAP exige conta de serviço e LLMGW_LDAP_USER_BASE"
            )
        return await asyncio.to_thread(self._lookup, username, dn)
