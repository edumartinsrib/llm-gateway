"""Agente do Microsoft Foundry: lê a credencial do Vault e dispara as chamadas no Azure.

Cada provedor cadastrado vira um `FoundryAgent`. O agente sabe:

- autenticar: chave de API (`api-key`) ou service principal do Entra ID
  (client credentials, token em cache até 5 min antes de expirar);
- montar a URL: API v1 (`<endpoint>/openai/v1/...`, sem api-version, o `model` é o
  nome do deployment) ou o formato clássico (`/openai/deployments/<nome>/...`);
- descobrir os deployments: pelo endpoint do projeto do Foundry
  (`<projeto>/deployments?api-version=v1`), pelo Azure Resource Manager
  (`Microsoft.CognitiveServices/accounts/<conta>/deployments`) ou por uma lista fixa;
- repetir a chamada em 429/5xx antes de devolver qualquer byte ao cliente,
  respeitando `retry-after-ms`/`retry-after`.

Formato do segredo no Vault (`<prefixo>/providers/<nome>`, KV v2):

    endpoint          https://<recurso>.services.ai.azure.com   (ou .openai.azure.com)
    auth              api_key | service_principal              (deduzido se ausente)
    api_key           chave do recurso                          (auth=api_key)
    tenant_id         \
    client_id          > service principal (inferência e/ou descoberta)
    client_secret     /
    discovery         project | arm | static | none             (deduzido se ausente)
    project_endpoint  https://<recurso>.services.ai.azure.com/api/projects/<projeto>
    subscription_id, resource_group, account_name               (discovery=arm)
    deployments       "gpt-4o,text-embedding-3-small"           (discovery=static)
    api_style         v1 (padrão) | deployments
    api_version       2024-10-21 (só para api_style=deployments)
    scope             https://cognitiveservices.azure.com/.default (escopo do token de inferência)
    authority         https://login.microsoftonline.com
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger(__name__)

DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
INFERENCE_SCOPE = "https://cognitiveservices.azure.com/.default"
PROJECT_SCOPE = "https://ai.azure.com/.default"
ARM_SCOPE = "https://management.azure.com/.default"
ARM_ENDPOINT = "https://management.azure.com"
ARM_API_VERSION = "2024-10-01"
CLASSIC_API_VERSION = "2024-10-21"
RETRYABLE = {408, 429, 500, 502, 503, 504}
MAX_RETRY_WAIT_S = 10.0

AUTH_TYPES = ("api_key", "service_principal")
DISCOVERY_MODES = ("project", "arm", "static", "none")
API_STYLES = ("v1", "deployments")

# Operações expostas e o caminho de cada uma na API do Azure.
OPERATIONS = {"chat": "chat/completions", "embeddings": "embeddings", "responses": "responses"}


class ProviderConfigError(ValueError):
    """Segredo do provedor incompleto ou inválido (mensagem segura para exibir)."""


class UpstreamError(Exception):
    """Falha ao falar com o Azure antes de haver resposta utilizável."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str = "upstream_error",
        auth_failure: bool = False,
        detail: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.auth_failure = auth_failure
        self.detail = detail


# --------------------------------------------------------------------------- configuração
def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_endpoint(raw: str) -> tuple[str, str | None]:
    """Aceita o endpoint em vários formatos; devolve (endpoint base, endpoint do projeto)."""
    raw = raw.strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise ProviderConfigError("endpoint inválido: use https://<recurso>.services.ai.azure.com")
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    project = None
    if path.startswith("/api/projects/"):
        project = base + path
    return base, project


def _as_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.replace("\n", ",").split(",")
    elif isinstance(value, list | tuple):
        items = [str(v) for v in value]
    else:
        raise ProviderConfigError("deployments deve ser uma lista ou texto separado por vírgulas")
    return tuple(dict.fromkeys(i.strip() for i in items if i.strip()))


@dataclass(frozen=True)
class FoundryConfig:
    endpoint: str
    auth: str
    api_key: str | None = None
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    authority: str = DEFAULT_AUTHORITY
    scope: str = INFERENCE_SCOPE
    api_style: str = "v1"
    api_version: str = CLASSIC_API_VERSION
    discovery: str = "none"
    project_endpoint: str | None = None
    subscription_id: str | None = None
    resource_group: str | None = None
    account_name: str | None = None
    arm_endpoint: str = ARM_ENDPOINT
    static_deployments: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_service_principal(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)

    @classmethod
    def from_secret(cls, data: dict[str, Any] | None) -> FoundryConfig:
        if not data:
            raise ProviderConfigError("segredo do provedor não encontrado no Vault")
        d = {str(k).strip().lower(): v for k, v in data.items()}
        raw_endpoint = _clean(d.get("endpoint") or d.get("azure_endpoint") or d.get("base_url"))
        project_endpoint = _clean(d.get("project_endpoint"))
        if not raw_endpoint and project_endpoint:
            raw_endpoint = project_endpoint
        if not raw_endpoint:
            raise ProviderConfigError("o segredo não tem 'endpoint'")
        endpoint, project_from_endpoint = _normalize_endpoint(raw_endpoint)
        if project_endpoint:
            project_endpoint = _normalize_endpoint(project_endpoint)[1] or project_endpoint
        project_endpoint = project_endpoint or project_from_endpoint

        api_key = _clean(d.get("api_key") or d.get("key"))
        tenant_id = _clean(d.get("tenant_id"))
        client_id = _clean(d.get("client_id"))
        client_secret = _clean(d.get("client_secret"))
        has_sp = bool(tenant_id and client_id and client_secret)

        auth = _clean(d.get("auth") or d.get("auth_type"))
        if auth is None:
            auth = "api_key" if api_key else "service_principal"
        if auth not in AUTH_TYPES:
            raise ProviderConfigError(f"auth deve ser um de {', '.join(AUTH_TYPES)}")
        if auth == "api_key" and not api_key:
            raise ProviderConfigError("auth=api_key exige 'api_key'")
        if auth == "service_principal" and not has_sp:
            raise ProviderConfigError(
                "auth=service_principal exige 'tenant_id', 'client_id' e 'client_secret'"
            )

        deployments = _as_list(d.get("deployments"))
        subscription_id = _clean(d.get("subscription_id"))
        resource_group = _clean(d.get("resource_group"))
        account_name = _clean(d.get("account_name"))
        discovery = _clean(d.get("discovery"))
        if discovery is None:
            if project_endpoint and has_sp:
                discovery = "project"
            elif subscription_id and resource_group and account_name and has_sp:
                discovery = "arm"
            elif deployments:
                discovery = "static"
            else:
                discovery = "none"
        if discovery not in DISCOVERY_MODES:
            raise ProviderConfigError(f"discovery deve ser um de {', '.join(DISCOVERY_MODES)}")
        if discovery in ("project", "arm") and not has_sp:
            raise ProviderConfigError(
                f"discovery={discovery} exige service principal "
                "(tenant_id, client_id, client_secret)"
            )
        if discovery == "project" and not project_endpoint:
            raise ProviderConfigError("discovery=project exige 'project_endpoint'")
        if discovery == "arm" and not (subscription_id and resource_group and account_name):
            raise ProviderConfigError(
                "discovery=arm exige 'subscription_id', 'resource_group' e 'account_name'"
            )
        if discovery == "static" and not deployments:
            raise ProviderConfigError("discovery=static exige 'deployments'")

        api_style = _clean(d.get("api_style")) or "v1"
        if api_style not in API_STYLES:
            raise ProviderConfigError(f"api_style deve ser um de {', '.join(API_STYLES)}")

        return cls(
            endpoint=endpoint,
            auth=auth,
            api_key=api_key,
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            authority=(_clean(d.get("authority")) or DEFAULT_AUTHORITY).rstrip("/"),
            scope=_clean(d.get("scope")) or INFERENCE_SCOPE,
            api_style=api_style,
            api_version=_clean(d.get("api_version")) or CLASSIC_API_VERSION,
            discovery=discovery,
            project_endpoint=project_endpoint,
            subscription_id=subscription_id,
            resource_group=resource_group,
            account_name=account_name,
            arm_endpoint=(_clean(d.get("arm_endpoint")) or ARM_ENDPOINT).rstrip("/"),
            static_deployments=deployments,
        )

    def fingerprint(self) -> str:
        """Hash do conteúdo: muda quando a credencial é rotacionada no Vault."""
        raw = json.dumps(asdict(self), sort_keys=True, default=list)
        return hashlib.sha256(raw.encode()).hexdigest()

    def public_view(self) -> dict[str, Any]:
        """Campos não sensíveis, para exibir no console."""
        return {
            "endpoint": self.endpoint,
            "auth": self.auth,
            "discovery": self.discovery,
            "api_style": self.api_style,
            "project_endpoint": self.project_endpoint,
            "subscription_id": self.subscription_id,
            "resource_group": self.resource_group,
            "account_name": self.account_name,
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "deployments": list(self.static_deployments),
            "authority": self.authority,
            "arm_endpoint": self.arm_endpoint,
            "scope": self.scope,
            "api_version": self.api_version,
            "has_api_key": bool(self.api_key),
            "has_client_secret": bool(self.client_secret),
        }


# --------------------------------------------------------------------------- tokens do Entra ID
class TokenProvider:
    """Client credentials do Entra ID, com cache por escopo."""

    def __init__(self, http: httpx.AsyncClient, config: FoundryConfig):
        self._http = http
        self._config = config
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._cache.clear()

    async def get(self, scope: str) -> str:
        cached = self._cache.get(scope)
        if cached and cached[1] - 300 > time.time():
            return cached[0]
        async with self._lock:
            cached = self._cache.get(scope)
            if cached and cached[1] - 300 > time.time():
                return cached[0]
            cfg = self._config
            url = f"{cfg.authority}/{quote(cfg.tenant_id or '')}/oauth2/v2.0/token"
            try:
                resp = await self._http.post(
                    url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": cfg.client_id,
                        "client_secret": cfg.client_secret,
                        "scope": scope,
                    },
                    timeout=30,
                )
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    502, f"Entra ID indisponível: {type(exc).__name__}", code="provider_auth_error"
                ) from exc
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("error_description") or resp.text
                except ValueError:
                    detail = resp.text
                raise UpstreamError(
                    502,
                    "o Entra ID recusou o service principal do provedor",
                    code="provider_auth_error",
                    auth_failure=True,
                    detail=str(detail)[:500],
                )
            body = resp.json()
            token = body["access_token"]
            self._cache[scope] = (token, time.time() + int(body.get("expires_in", 3600)))
            return token


# --------------------------------------------------------------------------- descoberta
@dataclass
class Deployment:
    name: str
    model_name: str | None = None
    model_version: str | None = None
    publisher: str | None = None
    sku: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    kind: str = "chat"


_NON_TEXT = ("dall-e", "gpt-image", "whisper", "tts", "sora", "flux", "stable-diffusion")
_REASONING = (
    "o1",
    "o3",
    "o4",
    "gpt-5",
    "deepseek-r1",
    "mai-ds-r1",
    "codex",
    "grok-3-mini",
    "phi-4-reasoning",
)
_VISION = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-4-turbo",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "vision",
    "multimodal",
    "mistral-medium",
    "llama-4",
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def classify(model_name: str | None, capabilities: dict[str, Any] | None) -> str:
    """chat | embeddings | other (imagem, áudio: não expostos pelo gateway)."""
    caps = {k.replace("_", "").lower(): v for k, v in (capabilities or {}).items()}
    name = (model_name or "").lower()
    if _truthy(caps.get("embeddings")) or "embed" in name:
        return "embeddings"
    if _truthy(caps.get("chatcompletion")) or _truthy(caps.get("responses")):
        return "chat"
    if _truthy(caps.get("imagegenerations")) or _truthy(caps.get("audio")):
        return "other"
    if any(token in name for token in _NON_TEXT):
        return "other"
    return "chat"


def guess_features(model_name: str | None) -> dict[str, bool]:
    name = (model_name or "").lower()
    return {
        "supports_reasoning": any(name.startswith(p) or f"-{p}" in name for p in _REASONING),
        "supports_vision": any(p in name for p in _VISION),
    }


def _deployment(name: str, model_name, version, publisher, sku, caps) -> Deployment:
    caps = caps if isinstance(caps, dict) else {}
    return Deployment(
        name=name,
        model_name=model_name or name,
        model_version=version,
        publisher=publisher,
        sku=sku,
        capabilities=caps,
        kind=classify(model_name or name, caps),
    )


# --------------------------------------------------------------------------- agente
@dataclass
class CheckResult:
    ok: bool
    message: str
    deployments: int | None = None


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    ms = resp.headers.get("retry-after-ms")
    if ms:
        try:
            return float(ms) / 1000
        except ValueError:
            pass
    seconds = resp.headers.get("retry-after")
    if seconds:
        try:
            return float(seconds)
        except ValueError:
            pass
    return min(0.5 * (2**attempt), MAX_RETRY_WAIT_S)


class FoundryAgent:
    def __init__(
        self,
        provider_id: str,
        name: str,
        config: FoundryConfig,
        http: httpx.AsyncClient,
        *,
        retries: int = 2,
    ):
        self.provider_id = provider_id
        self.name = name
        self.config = config
        self.fingerprint = config.fingerprint()
        self._http = http
        self._retries = max(0, retries)
        self._tokens = TokenProvider(http, config) if config.has_service_principal else None

    # ------------------------------------------------------------------ autenticação e URL
    async def _auth_headers(self, scope: str | None = None) -> dict[str, str]:
        if scope is None and self.config.auth == "api_key":
            return {"api-key": self.config.api_key or ""}
        assert self._tokens is not None
        token = await self._tokens.get(scope or self.config.scope)
        return {"Authorization": f"Bearer {token}"}

    def url(self, operation: str, deployment: str) -> str:
        path = OPERATIONS[operation]
        cfg = self.config
        if cfg.api_style == "deployments" and operation != "responses":
            return (
                f"{cfg.endpoint}/openai/deployments/{quote(deployment, safe='')}/{path}"
                f"?api-version={quote(cfg.api_version)}"
            )
        return f"{cfg.endpoint}/openai/v1/{path}"

    # ------------------------------------------------------------------ inferência
    async def send(self, operation: str, deployment: str, body: dict[str, Any]) -> httpx.Response:
        """Envia a chamada e devolve a resposta aberta em modo stream.

        O chamador lê o corpo (`aread()` ou `aiter_lines()`) e fecha (`aclose()`).
        429/5xx são repetidos aqui, antes de qualquer byte chegar ao cliente.
        """
        payload = dict(body)
        payload["model"] = deployment
        content = json.dumps(payload, ensure_ascii=False).encode()
        url = self.url(operation, deployment)
        attempt = 0
        while True:
            headers = {
                **(await self._auth_headers()),
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if body.get("stream") else "application/json",
            }
            request = self._http.build_request("POST", url, content=content, headers=headers)
            try:
                resp = await self._http.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                # Só repete quando a requisição não chegou ao Azure: depois de enviada,
                # repetir pode cobrar a mesma geração duas vezes.
                if attempt < self._retries:
                    await asyncio.sleep(min(0.5 * (2**attempt), MAX_RETRY_WAIT_S))
                    attempt += 1
                    continue
                raise UpstreamError(
                    502,
                    f"falha de conexão com o provedor ({type(exc).__name__})",
                    code="upstream_unavailable",
                ) from exc
            except httpx.TimeoutException as exc:
                raise UpstreamError(
                    504, "o provedor não respondeu a tempo", code="upstream_timeout"
                ) from exc
            except httpx.TransportError as exc:
                raise UpstreamError(
                    502,
                    f"falha de conexão com o provedor ({type(exc).__name__})",
                    code="upstream_unavailable",
                ) from exc

            if resp.status_code in RETRYABLE and attempt < self._retries:
                delay = _retry_delay(resp, attempt)
                if delay <= MAX_RETRY_WAIT_S:
                    await resp.aclose()
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
            if resp.status_code in (401, 403):
                raw = await resp.aread()
                await resp.aclose()
                if self._tokens is not None:
                    self._tokens.invalidate()
                raise UpstreamError(
                    502,
                    "o provedor recusou a credencial do gateway",
                    code="provider_auth_error",
                    auth_failure=True,
                    detail=raw[:500].decode(errors="replace"),
                )
            return resp

    # ------------------------------------------------------------------ descoberta
    async def _get_json(self, url: str, scope: str | None) -> dict[str, Any]:
        headers = {**(await self._auth_headers(scope)), "Accept": "application/json"}
        try:
            resp = await self._http.get(url, headers=headers, timeout=60)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                502, f"falha ao consultar o provedor ({type(exc).__name__})"
            ) from exc
        if resp.status_code in (401, 403):
            raise UpstreamError(
                502,
                "o provedor recusou a credencial na descoberta de deployments "
                "(confira o papel do service principal)",
                code="provider_auth_error",
                auth_failure=True,
                detail=resp.text[:500],
            )
        if resp.status_code != 200:
            raise UpstreamError(
                502,
                f"descoberta de deployments falhou com HTTP {resp.status_code}",
                detail=resp.text[:500],
            )
        return resp.json()

    async def _paged(self, url: str, scope: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        for _ in range(50):
            if not next_url:
                break
            page = await self._get_json(next_url, scope)
            items.extend(page.get("value") or [])
            next_url = page.get("nextLink")
        return items

    async def discover(self) -> list[Deployment]:
        cfg = self.config
        if cfg.discovery == "project":
            url = f"{cfg.project_endpoint}/deployments?api-version=v1"
            out = []
            for item in await self._paged(url, PROJECT_SCOPE):
                if item.get("type", "ModelDeployment") != "ModelDeployment" or not item.get("name"):
                    continue
                sku = item.get("sku") or {}
                out.append(
                    _deployment(
                        item["name"],
                        item.get("modelName"),
                        item.get("modelVersion"),
                        item.get("modelPublisher"),
                        sku.get("name"),
                        item.get("capabilities"),
                    )
                )
            return out
        if cfg.discovery == "arm":
            url = (
                f"{cfg.arm_endpoint}/subscriptions/{quote(cfg.subscription_id or '')}"
                f"/resourceGroups/{quote(cfg.resource_group or '')}"
                f"/providers/Microsoft.CognitiveServices/accounts/{quote(cfg.account_name or '')}"
                f"/deployments?api-version={ARM_API_VERSION}"
            )
            out = []
            for item in await self._paged(url, ARM_SCOPE):
                props = item.get("properties") or {}
                if props.get("provisioningState", "Succeeded") != "Succeeded" or not item.get(
                    "name"
                ):
                    continue
                model = props.get("model") or {}
                sku = item.get("sku") or {}
                out.append(
                    _deployment(
                        item["name"],
                        model.get("name"),
                        model.get("version"),
                        model.get("publisher") or model.get("format"),
                        sku.get("name"),
                        props.get("capabilities"),
                    )
                )
            return out
        if cfg.discovery == "static":
            return [
                _deployment(name, name, None, None, None, {}) for name in cfg.static_deployments
            ]
        return []

    async def check(self) -> CheckResult:
        """Valida a credencial sem gastar tokens de modelo."""
        cfg = self.config
        try:
            if self._tokens is not None:
                await self._tokens.get(cfg.scope)
            if cfg.discovery in ("project", "arm"):
                found = await self.discover()
                return CheckResult(True, f"{len(found)} deployment(s) encontrado(s)", len(found))
            headers = {**(await self._auth_headers()), "Accept": "application/json"}
            resp = await self._http.get(
                f"{cfg.endpoint}/openai/v1/models", headers=headers, timeout=30
            )
            if resp.status_code in (401, 403):
                return CheckResult(
                    False, f"o provedor recusou a credencial (HTTP {resp.status_code})"
                )
            if resp.status_code >= 400:
                return CheckResult(False, f"o provedor respondeu HTTP {resp.status_code}")
            count = len(cfg.static_deployments) if cfg.discovery == "static" else None
            return CheckResult(True, "credencial aceita pelo provedor", count)
        except UpstreamError as exc:
            return CheckResult(False, exc.message)
        except httpx.HTTPError as exc:
            return CheckResult(False, f"falha de conexão com o provedor ({type(exc).__name__})")


def build_http_client(
    *,
    timeout_s: float,
    connect_timeout_s: float,
    max_connections: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s, pool=30),
        limits=httpx.Limits(
            max_connections=max_connections, max_keepalive_connections=max(10, max_connections // 4)
        ),
        transport=transport,
        follow_redirects=False,
    )
