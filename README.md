# Gateway LLM para o Azure AI Foundry

Um proxy compatível com a API da OpenAI na frente dos modelos publicados no
**Azure AI Foundry**. Os times usam qualquer SDK ou ferramenta que fale OpenAI
(`openai`, LangChain, Pi, curl…) com uma chave própria; o gateway decide se a chamada
pode passar, encaminha ao Azure pelo agente do provedor e registra tokens, custo e cache.

- **Credencial do Azure só no Vault**: chave de API ou service principal do Entra ID,
  lida e recarregada pelo agente do provedor; nunca passa pelo banco nem pelo console.
- **Login pelo LDAP** (Active Directory ou OpenLDAP), com grupos que liberam o acesso e
  grupos de administradores; sincronização periódica desativa quem saiu do diretório.
- **Uma chave de API por uso** (`llmgw_…`), com validade, orçamento, limite por minuto e
  lista de modelos próprios; revogação imediata.
- **Gestão dos modelos**: os deployments são descobertos no projeto do Foundry (ou no
  Azure Resource Manager) e o administrador habilita, renomeia, define preços, TTL do
  cache e grupos com acesso.
- **Controle de custos**: custo por chamada a partir dos tokens, orçamento mensal por
  usuário, por chave e do gateway inteiro, alerta ao passar de um percentual, limites de
  requisições e tokens por minuto.
- **Cache de respostas** (Redis ou memória) para chat e embeddings, por usuário ou global,
  inclusive para respostas em streaming.
- **Console web** feito com a broto-ui (pacote em `gateway-console/vendor/`): visão
  geral, playground, chaves, uso e custos, guia de uso e administração.

Mesmo arranjo do Orquestrador: FastAPI + Postgres/Alembic, configuração por variáveis de
ambiente e Consul, segredos no Vault, Redis opcional, um Dockerfile por projeto.

## Componentes

| Projeto | O que é |
| --- | --- |
| [gateway-api](gateway-api/) | API do gateway (FastAPI): rotas OpenAI em `/v1`, API do console em `/api/v1`, agente do Foundry, cache, custos, LDAP |
| [gateway-console](gateway-console/) | Console React + TypeScript com a broto-ui, servido por nginx (que também faz o proxy de `/api` e `/v1`) |
| [dev/ldap](dev/ldap/) | OpenLDAP de desenvolvimento com usuários e grupos de exemplo |
| `llmgw-fake-foundry` | Foundry simulado (Entra ID, inferência v1 e clássica, descoberta de deployments), para desenvolvimento e testes |

```mermaid
flowchart LR
  subgraph Clientes
    sdk[SDK OpenAI / LangChain / Pi / curl]
    browser[Navegador]
  end
  subgraph Gateway
    console[gateway-console<br/>nginx] -->|/api, /v1| api[gateway-api]
    api --> pg[(Postgres<br/>usuários, chaves, modelos, uso)]
    api --> redis[(Redis<br/>cache, limites, eventos)]
    api --> vault[(Vault<br/>credencial do Azure)]
    api --> ldap[(LDAP / AD)]
  end
  sdk -->|/v1 + chave llmgw_| console
  browser --> console
  api -->|agente do provedor| foundry[Azure AI Foundry<br/>deployments]
  api -.->|token| entra[Entra ID]
```

## Como uma chamada passa pelo gateway

1. **Chave**: `Authorization: Bearer llmgw_…` (também aceita `api-key` e `x-api-key`).
   A chave precisa estar ativa e o usuário dono dela também.
2. **Modelo**: o campo `model` é o nome do modelo no gateway. Ele aponta para um
   provedor e um deployment; precisa estar habilitado, disponível no Azure e liberado
   para os grupos do usuário e para a chave.
3. **Orçamento e limites**: gasto do mês do usuário, da chave e do gateway; requisições
   e tokens por minuto. Recusas voltam no formato de erro da OpenAI (`429
   insufficient_quota` para orçamento; `429 rate_limit_exceeded` com `Retry-After`
   para limite por minuto).
4. **Cache** (chat e embeddings): o mesmo pedido (corpo idêntico, ignorando `user`,
   `metadata` e `stream_options`) devolve a resposta guardada, inclusive em streaming,
   sem custo.
   `X-LLMGW-Cache: off` não usa o cache; `refresh` refaz a chamada e regrava.
5. **Agente**: autentica no Azure (chave ou token do Entra ID em cache), chama a API v1
   do Foundry (`/openai/v1/...`) e repete em 429/5xx quando o `retry-after` do Azure
   é de até 10 s, sempre antes de mandar qualquer byte ao cliente. Quando o Azure
   recusa, os cabeçalhos de limite dele (`retry-after`, `x-ratelimit-*`) seguem para o
   cliente.
6. **Streaming**: os eventos SSE seguem para o cliente assim que chegam. O gateway pede
   o uso ao Azure (`include_usage`) e só repassa esse pedaço a quem pediu. Se o cliente
   desistir ou o stream cair antes do uso, o custo é estimado e marcado como estimado.
7. **Registro**: tokens (entrada, entrada em cache, saída, raciocínio), custo pelo preço
   do modelo, latência, tempo até o primeiro token e economia do cache vão para o
   Postgres; os contadores do mês são atualizados na mesma transação.

Cabeçalhos de resposta: `x-request-id`, `x-llmgw-cache` (`hit`, `miss`, `bypass` no
`refresh`, `off`),
`x-llmgw-cost-usd`, `x-llmgw-budget-remaining-usd` e `x-ratelimit-*`.

## Rodando localmente

Pré-requisitos: Docker e [uv](https://docs.astral.sh/uv/). Node 20 só para mexer no console.

```bash
docker compose up -d --build
```

```bash
uv run python scripts/demo.py
```

- Console: http://localhost:8090. Usuários do LDAP de exemplo, senha `Senha@123`:
  `ana` e `diego` (usuários), `bruno` (administrador). `carla` existe no diretório mas
  não está em nenhum grupo com acesso.
- API OpenAI: `http://localhost:8090/v1`, com uma chave criada no console.
- Swagger da API: http://localhost:8000/api/v1/docs

O compose sobe Postgres, Redis, Vault (modo dev), OpenLDAP, o Foundry simulado, a API e
o console. O `vault-init` grava no Vault a credencial do Foundry simulado
(`secret/llmgw/providers/foundry`) e a senha da conta de serviço do LDAP. O `demo.py`
sincroniza os deployments, habilita os modelos com **preços de exemplo** e faz chamadas
variadas com o SDK da OpenAI como cada usuário. Com `--espalhar-dias 21`, espalha essas
chamadas pelos últimos dias para o gráfico da visão geral.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8090/v1", api_key="llmgw_...")
resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Resuma o relatório em três tópicos"}],
)
print(resp.choices[0].message.content)
```

A página **Como usar** do console traz exemplos prontos (curl, Python, JavaScript,
LangChain e o `models.json` do Pi) com o endereço e os modelos liberados para quem está
logado.

Para apontar para um Foundry de verdade, grave a credencial no Vault e cadastre o
provedor (pelo console ou por `LLMGW_BOOTSTRAP_PROVIDERS`). O formato do segredo e as
permissões no Azure estão em [docs/operacao.md](docs/operacao.md).

## Desenvolvimento

```bash
uv sync --all-packages
```

```bash
uv run pytest gateway-api/tests
```

Os testes usam um Postgres real (`LLMGW_TEST_DATABASE_URL`, padrão
`postgresql://llmgw:llmgw@localhost:5432/llmgw_test`) migrado com Alembic e o Foundry
simulado. Os testes de Redis e LDAP rodam quando as variáveis estão definidas:

```bash
docker build -t llmgw-ldap dev/ldap && docker run -d -p 3389:389 llmgw-ldap
```

```bash
LLMGW_TEST_REDIS_URL=redis://localhost:6379/15 LLMGW_TEST_LDAP_URL=ldap://localhost:3389 uv run pytest gateway-api/tests
```

Há também um teste de ponta a ponta com servidores HTTP de verdade (Vault simulado →
agente → Foundry simulado, com o SDK oficial da OpenAI): streaming, queda do provedor no
meio do stream e cliente que desiste no meio da resposta.

Teste de interface (Playwright), com a pilha do compose de pé e o `demo.py` já rodado:
login negado para quem não está no grupo, criação de chave restrita e uso dela pela API,
cache, playground, revogação, telas de administração e layout no celular.

```bash
uv sync --all-packages --group ui && uv run playwright install chromium
```

```bash
LLMGW_UI_SCREENSHOTS=docs/screenshots uv run pytest tests/ui -v
```

API fora do Docker, em modo dev (login sem senha, segredos em memória):

```bash
uv run llmgw-fake-foundry &
cd gateway-api && LLMGW_DATABASE_URL=postgresql://llmgw:llmgw@localhost:55432/llmgw uv run alembic upgrade head && cd ..
LLMGW_DATABASE_URL=postgresql://llmgw:llmgw@localhost:55432/llmgw LLMGW_AUTH_MODE=dev \
LLMGW_BOOTSTRAP_ADMINS=admin LLMGW_SECRET_STORE=memory LLMGW_AUTO_ENABLE_MODELS=true \
LLMGW_PROVIDER_ALLOW_HTTP=true LLMGW_PROVIDER_ALLOWED_HOSTS=localhost LLMGW_PORT=8010 \
uv run llm-gateway
```

Depois, entre como `admin` e cadastre um provedor com endpoint `http://localhost:8070`,
chave `chave-falsa`, descoberta `static` e os deployments `gpt-4o-mini,text-embedding-3-small`.
Para o console, veja [gateway-console/README.md](gateway-console/README.md).

## Telas

| | |
| --- | --- |
| ![Visão geral](docs/screenshots/03-visao-geral.png) | ![Playground](docs/screenshots/05-playground.png) |
| ![Modelos](docs/screenshots/04-modelos.png) | ![Chave criada](docs/screenshots/08-chave-criada.png) |
| ![Detalhe de uma chamada](docs/screenshots/11-uso-detalhe.png) | ![Provedores](docs/screenshots/15-provedores.png) |

Todas as telas em [docs/screenshots](docs/screenshots/).

## Documentação

- [Contrato da API](docs/api.md)
- [Operação e implantação](docs/operacao.md): variáveis, Vault, LDAP, Azure, proxy, custos
- [Estado da implementação](docs/estado.md): o que foi verificado e o que falta
- [Console](gateway-console/README.md)

## Licença

[MIT](LICENSE).
