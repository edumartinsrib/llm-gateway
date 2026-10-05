# Contrato da API do gateway

Duas superfícies no mesmo serviço:

- **`/v1`**: compatível com a API da OpenAI, para aplicações, com chave de API.
- **`/api/v1`**: API do console, com a sessão do usuário logado.

JSON em snake_case. Datas em ISO 8601 UTC. IDs são UUID em texto. Valores em dólar são
números com até 10 casas decimais. A especificação OpenAPI gerada fica em
`/api/v1/openapi.json` (Swagger em `/api/v1/docs`).

## Autenticação

| Quem | Como | Rotas |
| --- | --- | --- |
| Aplicação | `Authorization: Bearer llmgw_<id>.<segredo>`; também aceita `api-key: <chave>` (como no Azure) e `x-api-key` | `/v1/*` |
| Console | `POST /api/v1/auth/login` devolve um JWT de sessão, enviado como `Authorization: Bearer <token>` | `/api/v1/*` |

A sessão vale `LLMGW_SESSION_TTL_MIN` (padrão 480 min). Sair (`POST /api/v1/auth/logout`),
ser bloqueado ou desativado invalida todas as sessões do usuário na hora.

Erros:

- `/api/v1`: status HTTP + `{"detail": "mensagem", "errors": ["..."]}` (`errors` é
  opcional; aparece em validações). 401 sem sessão, 403 sem papel, 404 também quando
  o recurso existe mas é de outra pessoa.
- `/v1`: `{"error": {"message": "...", "type": "...", "code": "...", "param": "..."}}`,
  o formato que os SDKs da OpenAI esperam.

Listas paginadas: `?limit=50&offset=0` → `{"items": [...], "total": 123, "limit": 50, "offset": 0}`.

## Enumerações

- `Role`: `user`, `admin`
- `UserStatus`: `active`; `blocked` (por um administrador, não volta sozinho);
  `disabled` (pela sincronização do LDAP: saiu do diretório ou dos grupos com acesso;
  volta a `active` quando reaparece)
- `KeyStatus`: `active`, `revoked`, `expired`
- `ModelKind`: `chat` (chat completions e Responses API), `embeddings`
- `ModelSource`: `discovered` (veio da descoberta de deployments), `manual`
- `ProviderStatus`: `ok`, `error`, `unknown`
- `ProviderAuth`: `api_key`, `service_principal`
- `Discovery`: `project` (endpoint do projeto do Foundry), `arm` (Azure Resource Manager),
  `static` (lista fixa no segredo), `none`
- `ApiStyle`: `v1` (`/openai/v1/...`, padrão), `deployments` (`/openai/deployments/<nome>/...?api-version=`)
- `Endpoint` (no uso): `chat`, `embeddings`, `responses`

## Rotas compatíveis com a OpenAI (`/v1`)

| Método e rota | O que faz |
| --- | --- |
| `POST /v1/chat/completions` | Chat, com ou sem `stream` |
| `POST /v1/embeddings` | Embeddings |
| `POST /v1/responses` | Responses API (com ou sem `stream`; não passa pelo cache) |
| `GET /v1/models` | Modelos que a chave pode usar: `{"object": "list", "data": [{"id", "object", "created", "owned_by"}]}` |
| `GET /v1/models/{modelo}` | Um modelo |

O corpo segue o da OpenAI. `model` é o **nome do modelo no gateway** (o que aparece em
`GET /v1/models`); o gateway troca pelo nome do deployment no Azure. Os demais campos vão
para o Azure como vieram. Em streaming de chat, o gateway acrescenta
`stream_options.include_usage` para receber o uso e só repassa o pedaço de uso a quem pediu.

Cabeçalhos aceitos:

| Cabeçalho | Efeito |
| --- | --- |
| `X-LLMGW-Cache: off` (ou `Cache-Control: no-store`) | Não lê nem grava o cache |
| `X-LLMGW-Cache: refresh` (ou `Cache-Control: no-cache`) | Ignora o que está no cache, chama o Azure e regrava |

Cabeçalhos de resposta:

| Cabeçalho | Conteúdo |
| --- | --- |
| `x-request-id` | ID da chamada (também no registro de uso) |
| `x-llmgw-cache` | `hit`, `miss`, `bypass` (com `refresh`) ou `off` |
| `x-llmgw-cost-usd` | Custo desta chamada (0 quando veio do cache). Não vem em streaming: o custo só é conhecido no fim |
| `x-llmgw-budget-remaining-usd` | Saldo do orçamento mensal do usuário, quando há orçamento |
| `x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests`, `x-ratelimit-reset-requests` | Limite de requisições por minuto do usuário, quando há |
| `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens` | Limite de tokens por minuto do usuário, quando há |

Quando o Azure recusa (429, 5xx), o gateway repassa `retry-after`, `retry-after-ms` e
os `x-ratelimit-*` do Azure.

Erros mais comuns:

| Status | `code` (`type`) | Quando |
| --- | --- | --- |
| 400 | `missing_model`, `missing_messages`, `missing_input`, `invalid_json` | Corpo incompleto ou inválido; modelo do tipo errado para a rota |
| 401 | `invalid_api_key` | Chave ausente, inválida, revogada ou expirada |
| 403 | `user_blocked` | Dono da chave bloqueado ou desativado |
| 403 | (`permission_error`) | O modelo existe, mas não está liberado para os grupos do usuário ou para a chave |
| 404 | `model_not_found` | Modelo inexistente ou desabilitado |
| 413 | `request_too_large` | Corpo acima de `LLMGW_MAX_REQUEST_MB` |
| 429 | `insufficient_quota` | Orçamento do mês esgotado (usuário, chave ou gateway) |
| 429 | `rate_limit_exceeded` | Limite por minuto do usuário ou da chave, com `Retry-After` |
| 429, 5xx | o do Azure | Recusa do Azure depois das retentativas |
| 502 | `upstream_unavailable` | Falha de conexão com o Azure depois das retentativas |
| 502 | `provider_auth_error` | O Azure ou o Entra ID recusou a credencial do gateway (o provedor fica com erro no console; não é a chave do cliente) |
| 502 | `invalid_upstream_response`, `upstream_stream_error` | Resposta inválida do Azure; stream que caiu no meio (vem como evento `data: {"error": ...}`) |
| 503 | `provider_unavailable` | Provedor desabilitado ou sem credencial carregada |
| 504 | `upstream_timeout` | O Azure não respondeu em `LLMGW_UPSTREAM_TIMEOUT_S` |

## API do console (`/api/v1`)

### Sessão

| Método e rota | Quem | O que faz |
| --- | --- | --- |
| `GET /config` | público | Modo de login (`dev`/`ldap`), versão, `api_base_url`, cache, fuso, alerta de orçamento |
| `POST /auth/login` | público | `{"username", "password"}` → `{"token", "expires_at", "user"}`. No modo `ldap` valida a senha e os grupos no diretório; no modo `dev` aceita qualquer usuário sem senha |
| `POST /auth/logout` | logado | Invalida todas as sessões do usuário |
| `GET /me` | logado | Usuário, grupos, papel, orçamento e gasto do mês, limites |

Falhas de login são limitadas por usuário e IP (`LLMGW_LOGIN_MAX_ATTEMPTS` em 5 minutos)
e por usuário (o triplo).

### Chaves

| Método e rota | Quem | O que faz |
| --- | --- | --- |
| `GET /keys?user_id=` | logado | Chaves do usuário (administrador pode ver as de outro) |
| `POST /keys?user_id=` | logado | `{"name", "expires_in_days", "monthly_budget_usd", "rpm_limit", "allowed_models"}` → chave com o campo `key` (o segredo aparece só nesta resposta) |
| `PATCH /keys/{id}` | dono ou administrador | Nome e limites |
| `POST /keys/{id}/revoke` | dono ou administrador | Revoga na hora |

O gateway guarda só o hash do segredo. `display` mostra o prefixo e os 4 últimos
caracteres. Quando um administrador cria ou limita a chave de outra pessoa,
`limits_locked` fica `true` e o dono não consegue mais mudar orçamento, limite por minuto
e modelos (só o nome, ou revogar).

### Modelos

| Método e rota | Quem | O que faz |
| --- | --- | --- |
| `GET /models?all=` | logado | Modelos que o usuário pode usar; com `all=true`, o administrador vê todos (habilitados ou não) |
| `POST /models` | admin | Cadastra um modelo apontando para `provider_id` + `deployment` (para o que a descoberta não acha) |
| `PATCH /models/{id}` | admin | `name`, `kind`, `enabled`, preços, `cache_ttl_s`, `allowed_groups`, contexto, capacidades, descrição |
| `DELETE /models/{id}` | admin | Remove (o histórico de uso continua) |
| `POST /cache/clear` | admin | Limpa o cache de respostas |

Preços em US$ por 1 milhão de tokens: `input_price`, `cached_input_price` (entrada que
o Azure informou como vinda do cache de prompt; vazio = mesmo preço da entrada) e
`output_price`. `allowed_groups` vazio libera para todos que entram no gateway; um grupo
é o nome (CN) ou o DN completo. `cache_ttl_s` vazio usa o padrão do tipo; 0 desliga.
Alterações chegam a todas as réplicas na hora (eventos pelo Redis).

### Provedores (admin)

| Método e rota | O que faz |
| --- | --- |
| `GET /providers` | Lista, com estado do agente, último erro, modelos e a configuração sem segredos (`config`) |
| `GET /providers/{id}` | Um provedor |
| `POST /providers` | `{"name", "display_name", "secret"}` grava o segredo em `<prefixo>/providers/<nome>` no Vault; ou `{"name", "vault_path"}` usa um segredo provisionado por fora, num dos prefixos de `LLMGW_PROVIDER_VAULT_PREFIXES` |
| `PATCH /providers/{id}` | `display_name`, `enabled` |
| `PUT /providers/{id}/secret` | Troca a configuração (só segredos gravados pelo gateway). `keep_existing_secrets: true` mantém chave e client secret guardados, desde que o destino (endpoint, projeto, tenant, client) não mude |
| `POST /providers/{id}/check` | Testa autenticação e descoberta: `{"ok", "message", "deployments"}` |
| `POST /providers/{id}/sync` | Descobre os deployments: `{"found", "created", "updated", "missing", "skipped"}` |
| `DELETE /providers/{id}` | Remove o provedor e os modelos dele; apaga o segredo do Vault só se foi o gateway que gravou |

Campos do segredo (`ProviderSecretIn`): `endpoint`, `auth`, `api_key`, `tenant_id`,
`client_id`, `client_secret`, `discovery`, `project_endpoint`, `subscription_id`,
`resource_group`, `account_name`, `deployments`, `api_style`, `api_version`, `scope`,
`authority`, `arm_endpoint`. Formato e exemplos em [operacao.md](operacao.md#credencial-do-azure-no-vault).
Endpoints precisam ser `https://` e de um host em `LLMGW_PROVIDER_ALLOWED_HOSTS`.

### Usuários (admin)

| Método e rota | O que faz |
| --- | --- |
| `GET /users?q=&status=&role=` | Lista com gasto e chamadas do mês e chaves ativas |
| `POST /users` | Cadastra antes do primeiro login (para já definir orçamento e limites) |
| `GET /users/{id}` | Um usuário |
| `PATCH /users/{id}` | Papel, `status` (`active`/`blocked`) e motivo, orçamento, RPM, TPM, modelos. `groups` só no modo `dev` |
| `POST /users/{id}/ldap-sync` | Relê grupos, nome e e-mail no diretório agora |

Um administrador não consegue bloquear a si mesmo nem tirar o próprio papel. Quem é
administrador por grupo do LDAP (`LLMGW_LDAP_ADMIN_GROUPS`) ou por
`LLMGW_BOOTSTRAP_ADMINS` continua administrador mesmo com `role = user`.

### Uso e custos

| Método e rota | Quem | O que faz |
| --- | --- | --- |
| `GET /usage` | logado | Chamadas. Filtros: `user_id` (admin), `model`, `outcome` (`ok`/`error`), `cache` (`hit`/`miss`), `endpoint`, `key_id`, `since`, `until`, `q` |
| `GET /usage/export` | logado | Os mesmos filtros, em CSV |
| `GET /usage/summary?days=&user_id=&mine=` | logado | Totais, custo por dia e modelo, ranking de modelos e usuários, gasto do mês do usuário e do gateway |

Quem não é administrador só vê o próprio uso. Cada registro traz tokens (entrada,
entrada em cache, saída, raciocínio), `cost_usd`, `cache_hit` e `saved_usd` (o que a
chamada custaria sem o cache), `usage_estimated` (o Azure não informou o uso, porque o
cliente desistiu ou o stream caiu, e o gateway estimou por caracteres), latência e
tempo até o primeiro token.

### Auditoria (admin)

`GET /audit?action=&actor=&resource_type=&resource_id=&q=&since=&until=`

Toda escrita é auditada com antes e depois (segredos são omitidos): `auth.login`,
`key.create`, `key.update`, `key.revoke`, `model.create`, `model.update`,
`model.delete`, `cache.clear`, `provider.create`, `provider.update`,
`provider.secret_update`, `provider.secret_reloaded`, `provider.sync`,
`provider.delete`, `user.create`, `user.update`, `user.disabled`,
`user.reactivated`, `budget.alert`.

### Playground

`POST /api/v1/playground/chat/completions` e `POST /api/v1/playground/embeddings`: o
mesmo contrato das rotas `/v1`, autenticado pela sessão do console. O uso entra no
orçamento do usuário com a chave `console`.

## Health checks

- `GET /healthz`: processo de pé.
- `GET /readyz`: banco respondendo; devolve quantos modelos estão roteáveis.
