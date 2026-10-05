# Operação e implantação

Cada projeto sobe a partir do próprio Dockerfile e lê configuração só de variáveis de
ambiente e do Consul. Precedência na API: variável de ambiente > Consul (prefixo
`LLMGW_CONSUL_PREFIX`, padrão `llmgw/config/`, uma chave por campo com o nome do campo em
minúsculas, ex. `llmgw/config/cache_chat_ttl_s`) > padrão. Segredos (credencial do Azure,
senha da conta de serviço do LDAP, chave das sessões) ficam no Vault.

Os nomes que a plataforma costuma injetar (`POSTGRES_HOST`, `PGHOST`, `DB_HOST`,
`REDIS_URL`, `VAULT_ADDR`…) são traduzidos num único lugar
(`gateway-api/src/llmgw/settings.py`).

## Ordem de implantação

1. **Postgres** (banco `llmgw`), **Vault** (KV v2), **LDAP/AD** e, de preferência,
   **Redis**.
2. **Segredos no Vault**: a credencial de cada provedor (ver abaixo) e a senha da conta
   de serviço do LDAP em `<prefixo>/_system/ldap` (campo `bind_password`).
3. **gateway-api**: roda `alembic upgrade head` e sobe a API na porta 8000. Na primeira
   subida gera a chave das sessões e grava em `<prefixo>/_system/keys` (se não vier em
   `LLMGW_SESSION_KEY`). Cadastra os provedores de `LLMGW_BOOTSTRAP_PROVIDERS`, carrega
   as credenciais e descobre os deployments.
4. **gateway-console**: nginx na porta 8080, servindo o console e repassando `/api` e
   `/v1` para a API. É o endereço público do gateway (`LLMGW_PUBLIC_URL`).
5. No console, entre com um usuário de `LLMGW_LDAP_ADMIN_GROUPS` (ou de
   `LLMGW_BOOTSTRAP_ADMINS`), confira o provedor, **defina os preços** e habilite os
   modelos.

### Várias réplicas

A API não guarda estado em memória que importe entre réplicas quando há Redis: cache de
respostas, limites por minuto, tentativas de login e avisos de mudança de configuração
(modelo editado, chave revogada, provedor trocado) passam por ele. Sem Redis, cada réplica
tem o próprio cache e os próprios limites, e as mudanças chegam às outras réplicas no
recarregamento periódico (`LLMGW_PROVIDER_REFRESH_S`, padrão 60 s).

As tarefas de fundo rodam em todas as réplicas, mas cada rodada pega um advisory lock no
Postgres, então só uma réplica trabalha por vez:

| Tarefa | Intervalo | O que faz |
| --- | --- | --- |
| Recarregar provedores | `LLMGW_PROVIDER_REFRESH_S` (60 s) | Relê os segredos no Vault; se a credencial mudou, recria o agente (auditoria `provider.secret_reloaded`). Rotação sem reiniciar |
| Descobrir deployments | `LLMGW_DISCOVERY_INTERVAL_S` (3600 s) | Novos deployments entram desabilitados (ou habilitados com `LLMGW_AUTO_ENABLE_MODELS=true`, sem preço); os que sumiram ficam indisponíveis |
| Sincronizar o LDAP | `LLMGW_LDAP_SYNC_INTERVAL_S` (900 s) | Ver [LDAP](#ldap) |
| Limpar o histórico | diário | Apaga registros de uso com mais de `LLMGW_USAGE_RETENTION_DAYS` (400) dias |

`LLMGW_BACKGROUND_TASKS=false` desliga as tarefas numa réplica.

## gateway-api

| Variável | Uso |
| --- | --- |
| `LLMGW_DATABASE_URL` ou `POSTGRES_*`/`PG*`/`DB_*`, `LLMGW_DB_POOL_SIZE` | Postgres |
| `REDIS_URL` ou `REDIS_HOST`/`REDIS_PORT`/`REDIS_PASSWORD` | Cache, limites, eventos entre réplicas. Opcional |
| `VAULT_ADDR` + `VAULT_TOKEN` (ou `VAULT_ROLE_ID`/`VAULT_SECRET_ID`), `VAULT_NAMESPACE` | Vault. Com AppRole, o gateway refaz o login quando o token expira |
| `LLMGW_VAULT_MOUNT` (`secret`), `LLMGW_VAULT_PREFIX` (`llmgw`) | Mount KV v2 e prefixo dos caminhos |
| `CONSUL_HTTP_ADDR`, `CONSUL_HTTP_TOKEN`, `LLMGW_CONSUL_PREFIX` | Configuração não sensível |
| `LLMGW_PUBLIC_URL` | Endereço público do gateway (o do console); a API OpenAI fica em `<url>/v1`. Aparece no console e nos exemplos |
| `LLMGW_AUTH_MODE` | `ldap` (padrão). `dev` aceita qualquer usuário sem senha: só para desenvolvimento |
| `LLMGW_SESSION_TTL_MIN` (480), `LLMGW_SESSION_KEY` | Sessão do console. Sem chave na variável, ela é gerada e guardada no Vault |
| `LLMGW_BOOTSTRAP_ADMINS` | Usuários administradores, separados por vírgula (para o primeiro acesso; prefira grupos) |
| `LLMGW_LOGIN_MAX_ATTEMPTS` (10) | Falhas de login por usuário e IP em 5 min (por usuário: o triplo) |
| `LLMGW_LDAP_*` | Ver [LDAP](#ldap) |
| `LLMGW_BOOTSTRAP_PROVIDERS` | `nome=caminho/no/vault,...`: provedores cadastrados na subida, com segredo provisionado por fora |
| `LLMGW_PROVIDER_VAULT_PREFIXES` | Prefixos do Vault (além de `<prefixo>/providers/`) que o console aceita para segredos provisionados por fora, ex. `plataforma/azure/` |
| `LLMGW_PROVIDER_ALLOWED_HOSTS` | Sufixos de host aceitos nos endpoints informados pelo console. Padrão: nuvens do Azure (`.azure.com`, `.microsoftonline.com`, governo e China). `*` libera qualquer um |
| `LLMGW_PROVIDER_ALLOW_HTTP` (`false`) | Aceita `http://` (só para o Foundry simulado) |
| `LLMGW_FORWARDED_ALLOW_IPS` (`127.0.0.1`) | IPs ou redes (CIDR) do proxy confiáveis para `X-Forwarded-For`. Ver [Proxy](#proxy-e-endereço-do-cliente) |
| `LLMGW_PROVIDER_REFRESH_S` (60), `LLMGW_DISCOVERY_INTERVAL_S` (3600), `LLMGW_AUTO_ENABLE_MODELS` (`false`) | Provedores e descoberta |
| `LLMGW_UPSTREAM_TIMEOUT_S` (600), `LLMGW_UPSTREAM_CONNECT_TIMEOUT_S` (10), `LLMGW_UPSTREAM_RETRIES` (2), `LLMGW_UPSTREAM_MAX_CONNECTIONS` (200) | Chamadas ao Azure |
| `LLMGW_MAX_REQUEST_MB` (50) | Tamanho máximo do corpo (imagens em base64). Ajuste também o `client_max_body_size` do nginx e do ingress |
| `LLMGW_CACHE_ENABLED` (`true`), `LLMGW_CACHE_SCOPE` (`user`/`global`), `LLMGW_CACHE_CHAT_TTL_S` (3600), `LLMGW_CACHE_EMBEDDINGS_TTL_S` (86400), `LLMGW_CACHE_MAX_ENTRY_KB` (1024), `LLMGW_CACHE_MEMORY_MAX_ENTRIES` (5000) | Cache de respostas |
| `LLMGW_TIMEZONE` (`America/Sao_Paulo`) | Fuso do mês de orçamento e dos gráficos |
| `LLMGW_DEFAULT_MONTHLY_BUDGET_USD`, `LLMGW_GLOBAL_MONTHLY_BUDGET_USD`, `LLMGW_BUDGET_ALERT_PCT` (80) | Orçamento padrão de usuários novos, do gateway e alerta |
| `LLMGW_DEFAULT_RPM`, `LLMGW_DEFAULT_TPM` (0 = sem limite) | Limites por minuto de quem não tem limite próprio |
| `LLMGW_USAGE_RETENTION_DAYS` (400) | Retenção do histórico de chamadas (0 = para sempre) |
| `LLMGW_CORS_ORIGINS` | Só se outra origem chamar a API do console direto (o console usa o mesmo host) |
| `LLMGW_BACKGROUND_TASKS` (`true`), `LLMGW_LOG_LEVEL` (`INFO`), `LLMGW_PORT` (8000) | Processo |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | OpenTelemetry (traces das rotas FastAPI); sem endpoint, os logs saem no stdout com `trace_id` |

## Credencial do Azure no Vault

Cada provedor é um segredo KV v2. Os criados pelo console ficam em
`<prefixo>/providers/<nome>`; os provisionados pela plataforma podem ficar em qualquer
prefixo de `LLMGW_PROVIDER_VAULT_PREFIXES` (o gateway só lê, e o console não mostra nem
altera os campos sensíveis).

| Campo | Uso |
| --- | --- |
| `endpoint` | `https://<recurso>.services.ai.azure.com` (ou `https://<recurso>.openai.azure.com`) |
| `auth` | `api_key` ou `service_principal` (deduzido dos campos presentes) |
| `api_key` | Chave do recurso (inferência) |
| `tenant_id`, `client_id`, `client_secret` | Service principal do Entra ID (inferência e/ou descoberta) |
| `discovery` | `project`, `arm`, `static` ou `none` (deduzido) |
| `project_endpoint` | `https://<recurso>.services.ai.azure.com/api/projects/<projeto>` (descoberta `project`) |
| `subscription_id`, `resource_group`, `account_name` | Descoberta `arm` |
| `deployments` | `gpt-4o-mini,text-embedding-3-small` (descoberta `static`) |
| `api_style` | `v1` (padrão) ou `deployments` (formato clássico com `api-version`) |
| `api_version` | Só com `api_style=deployments` (padrão `2024-10-21`) |
| `scope` | Escopo do token de inferência (padrão `https://cognitiveservices.azure.com/.default`) |
| `authority`, `arm_endpoint` | Nuvens soberanas (padrão `https://login.microsoftonline.com` e `https://management.azure.com`) |

Chave do recurso e lista fixa de deployments (o mínimo para começar):

```bash
vault kv put -mount=secret llmgw/providers/foundry \
  endpoint=https://meu-recurso.services.ai.azure.com \
  api_key=<chave do recurso> \
  deployments=gpt-4o-mini,text-embedding-3-small
```

Service principal para tudo, com descoberta pelo projeto do Foundry:

```bash
vault kv put -mount=secret llmgw/providers/foundry \
  endpoint=https://meu-recurso.services.ai.azure.com \
  tenant_id=<tenant> client_id=<app id> client_secret=<segredo> \
  project_endpoint=https://meu-recurso.services.ai.azure.com/api/projects/meu-projeto
```

Chave para a inferência e service principal só para descobrir pelo Azure Resource Manager:

```bash
vault kv put -mount=secret llmgw/providers/foundry \
  endpoint=https://meu-recurso.services.ai.azure.com auth=api_key api_key=<chave> \
  tenant_id=<tenant> client_id=<app id> client_secret=<segredo> \
  subscription_id=<assinatura> resource_group=<grupo> account_name=meu-recurso
```

Depois, `LLMGW_BOOTSTRAP_PROVIDERS=foundry=llmgw/providers/foundry` (ou cadastre pelo
console apontando para o caminho). Para trocar a chave ou o client secret, grave a nova
versão no Vault: em até `LLMGW_PROVIDER_REFRESH_S` o agente é recriado.

### Permissões no Azure

| Para | Credencial | Permissão (atribuição de papel) |
| --- | --- | --- |
| Inferência | Chave do recurso | Nenhuma além da chave |
| Inferência | Service principal | Papel com acesso de dados de inferência no recurso, ex. *Cognitive Services OpenAI User* (modelos Azure OpenAI) ou *Cognitive Services User* / *Azure AI User* (demais modelos do Foundry) |
| Descoberta `project` | Service principal (token para `https://ai.azure.com/.default`) | *Azure AI User* no projeto (ou no recurso) |
| Descoberta `arm` | Service principal (token para `https://management.azure.com/.default`) | Leitura dos deployments da conta, ex. *Reader* no recurso |

Confira os nomes dos papéis com a equipe de nuvem: a Microsoft renomeia papéis do Foundry
com frequência. O teste de conexão do console (**Provedores → Testar conexão**) mostra a
mensagem do Entra ID ou do Azure quando falta permissão.

### Política do Vault para o gateway

```hcl
# Segredos próprios: provedores criados pelo console, chave das sessões, senha do LDAP.
path "secret/data/llmgw/*"                { capabilities = ["create", "read", "update"] }
path "secret/metadata/llmgw/providers/*"  { capabilities = ["delete"] }
# Segredos provisionados pela plataforma (se usar LLMGW_PROVIDER_VAULT_PREFIXES).
path "secret/data/plataforma/azure/*"     { capabilities = ["read"] }
```

## LDAP

| Variável | Uso |
| --- | --- |
| `LLMGW_LDAP_URL` | Um ou mais servidores separados por vírgula (`ldaps://dc1:636,ldaps://dc2:636`) |
| `LLMGW_LDAP_START_TLS`, `LLMGW_LDAP_VERIFY_TLS` (`true`), `LLMGW_LDAP_CA_FILE`, `LLMGW_LDAP_TIMEOUT_S` (10) | TLS e tempo limite |
| `LLMGW_LDAP_BIND_DN` + senha (`LLMGW_LDAP_BIND_PASSWORD` ou Vault `<prefixo>/_system/ldap`) | Conta de serviço que procura o usuário e os grupos |
| `LLMGW_LDAP_USER_BASE`, `LLMGW_LDAP_USER_FILTER` | Onde e como achar o usuário. Padrão: `(\|(sAMAccountName={username})(uid={username}))` |
| `LLMGW_LDAP_USER_DN_TEMPLATE` | Sem conta de serviço: bind direto com `uid={username},ou=...` |
| `LLMGW_LDAP_GROUP_MODE` | `auto` (usa `memberOf` se existir, senão procura), `memberof`, `search` |
| `LLMGW_LDAP_GROUP_BASE`, `LLMGW_LDAP_GROUP_FILTER` | Busca de grupos (modo `search`) |
| `LLMGW_LDAP_NESTED_GROUPS` | Grupos aninhados do Active Directory (`LDAP_MATCHING_RULE_IN_CHAIN`) |
| `LLMGW_LDAP_ALLOWED_GROUPS` | Quem pode entrar. Vazio = qualquer usuário do diretório |
| `LLMGW_LDAP_ADMIN_GROUPS` | Administradores do gateway |
| `LLMGW_LDAP_ATTR_DISPLAY_NAME` (`displayName`), `LLMGW_LDAP_ATTR_EMAIL` (`mail`) | Atributos do usuário |
| `LLMGW_LDAP_SYNC_INTERVAL_S` (900), `LLMGW_LDAP_SYNC_MAX_MISSING_PCT` (20) | Sincronização |

Grupos podem ser configurados pelo nome (CN) ou pelo DN completo. Prefira o DN: o mesmo
CN pode existir em OUs diferentes, e um DN só casa com o DN exato.

Exemplo para Active Directory:

```bash
LLMGW_LDAP_URL=ldaps://dc1.exemplo.local,ldaps://dc2.exemplo.local
LLMGW_LDAP_BIND_DN="CN=svc-gateway-llm,OU=Servicos,DC=exemplo,DC=local"
LLMGW_LDAP_USER_BASE="DC=exemplo,DC=local"
LLMGW_LDAP_NESTED_GROUPS=true
LLMGW_LDAP_ALLOWED_GROUPS="CN=GG-LLM-Usuarios,OU=Grupos,DC=exemplo,DC=local"
LLMGW_LDAP_ADMIN_GROUPS="CN=GG-LLM-Admins,OU=Grupos,DC=exemplo,DC=local"
```

No login, o gateway procura o usuário com a conta de serviço, valida a senha com um bind
do próprio usuário (senha vazia é recusada) e lê os grupos. A sincronização periódica
relê cada usuário pelo DN guardado: quem saiu do diretório ou dos grupos com acesso fica
`disabled` (as chaves param na hora e as sessões caem); quem voltou fica `active`. Se
mais de `LLMGW_LDAP_SYNC_MAX_MISSING_PCT`% dos usuários conferidos (com pelo menos 5)
sumirem de uma vez, a rodada é abortada sem desativar ninguém: costuma ser a conta de
serviço que perdeu permissão, não uma demissão em massa. Usuários bloqueados por um
administrador não são tocados pela sincronização.

## Proxy e endereço do cliente

O IP do cliente aparece no histórico de uso e entra no limite de tentativas de login. Ele
vem do `X-Forwarded-For` só quando a conexão chega de um IP em
`LLMGW_FORWARDED_ALLOW_IPS`. Liste ali o IP ou a rede (CIDR) do nginx do console e do
ingress; com o padrão (`127.0.0.1`), atrás de um proxy todos os clientes aparecem com o
IP do proxy. Nunca use `*` com a API exposta direto: qualquer um forjaria o IP.

O compose usa `*` porque a API só é alcançada pela rede interna do Docker (a porta 8000
fica aberta no host só para o Swagger em desenvolvimento).

Proxies e ingress na frente do gateway precisam de:

- **streaming**: sem buffer de resposta (`proxy_buffering off` no nginx) e tempo de
  leitura longo (o nginx do console usa 15 min; modelos de raciocínio podem levar
  minutos até o primeiro token);
- **corpo grande**: pelo menos `LLMGW_MAX_REQUEST_MB`.

## Custos e orçamento

- **Preços**: por modelo, em US$ por 1 milhão de tokens, definidos pelo administrador no
  console. O gateway não consulta a tabela de preços do Azure. Os preços do `demo.py` são
  só exemplos. Use os do seu contrato.
- **Cálculo**: `(entrada − entrada em cache) × preço de entrada + entrada em cache ×
  preço de entrada em cache + saída × preço de saída`. Tokens de raciocínio já vêm dentro
  dos de saída, como na OpenAI. Chamadas que vieram do cache do gateway custam zero e
  registram a economia.
- **Mês**: mês do calendário no fuso `LLMGW_TIMEZONE`.
- **Orçamento flexível**: o saldo é conferido antes de cada chamada, mas o custo só é
  conhecido no fim. A chamada que cruza o limite termina normalmente, e chamadas que já
  estavam em andamento ao mesmo tempo também. A seguinte é recusada. O excesso fica
  limitado ao que estava em voo; para apertar, combine com limite de requisições por
  minuto. O limite de tokens por minuto funciona igual: é conferido com os tokens já
  usados no minuto.
- **Alerta**: ao passar de `LLMGW_BUDGET_ALERT_PCT`% do orçamento do usuário, o gateway
  registra `budget.alert` na auditoria (uma vez por mês) e o console mostra a barra de
  gasto em amarelo. Não há envio de e-mail.
- **Recusas sem custo** (orçamento, limite, modelo sem acesso) ficam no histórico, mas
  não mexem nos contadores do mês. Recusas repetidas do mesmo usuário são registradas no
  máximo uma vez por minuto para cada motivo.

## Cache

- Chave: hash de escopo + rota + modelo + corpo da requisição, ignorando `user`,
  `metadata` e `stream_options`. Respostas em streaming são guardadas como a lista de
  eventos e devolvidas como stream.
- Só respostas 200 completas entram no cache. A Responses API não usa cache.
- `LLMGW_CACHE_SCOPE=user` (padrão) separa por usuário. `global` compartilha respostas
  entre usuários com acesso ao mesmo modelo; use só se os prompts não tiverem dado
  pessoal ou sigiloso.
- O cache devolve a mesma resposta mesmo com `temperature > 0`. Quem quer variação manda
  `X-LLMGW-Cache: off`; o administrador pode zerar o TTL de um modelo.
- Com Redis, as entradas ficam em `llmgw:cache:*` com TTL. Sem Redis, num LRU em memória
  por réplica.

## gateway-console

| Variável | Uso |
| --- | --- |
| `LLMGW_API_UPSTREAM` | Endereço da API (padrão `http://gateway-api:8000`) |

| Caminho | Destino | Observações |
| --- | --- | --- |
| `/` | SPA (`index.html` como fallback) | Sem cache; CSP restrita a `'self'` |
| `/assets/` | Arquivos com hash do Vite | Cache de 1 ano, imutável |
| `/api/`, `/v1/` | `${LLMGW_API_UPSTREAM}` | Sem buffer (SSE), timeouts de 15 min, corpo até 50 MB |
| `/healthz` | nginx | Health check |

As fontes (Exo 2 e Nunito) vão dentro do bundle; o console não depende do Google Fonts.

## Checklist de produção

- `LLMGW_AUTH_MODE=ldap` (padrão) e LDAP com TLS (`ldaps://` ou `LLMGW_LDAP_START_TLS`).
- Grupos em `LLMGW_LDAP_ALLOWED_GROUPS` e `LLMGW_LDAP_ADMIN_GROUPS`, de preferência por DN;
  `LLMGW_BOOTSTRAP_ADMINS` vazio depois do primeiro acesso.
- `LLMGW_FORWARDED_ALLOW_IPS` com o IP/CIDR do proxy.
- `LLMGW_PROVIDER_ALLOWED_HOSTS` e `LLMGW_PROVIDER_ALLOW_HTTP` com os padrões (ou mais
  restritos, só com o host do seu recurso).
- Vault com AppRole e a política acima.
- Redis com mais de uma réplica.
- Preços revisados em todos os modelos habilitados e orçamento global definido.
