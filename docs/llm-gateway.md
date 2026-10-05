# llm-gateway — visão geral e pendências

> Situação em 05/10/2026 · pasta `0005-work-home/llm-gateway`

## 1. O que é

Proxy compatível com a API da OpenAI na frente dos modelos do **Azure AI Foundry**, no mesmo padrão do Orquestrador (FastAPI + Postgres/Alembic, env + Consul, Vault, Redis opcional, um Dockerfile por projeto), com console feito na **broto-ui**.

- Credencial do Azure só no Vault (chave do recurso ou service principal), lida pelo agente do provedor e recarregada sem reiniciar.
- Login do console pelo LDAP/AD, com grupos de acesso e de administradores; sincronização periódica desativa quem saiu.
- Chave de API por uso (`llmgw_…`), com validade, orçamento, RPM e modelos próprios.
- Modelos descobertos no projeto do Foundry (ou ARM, ou lista fixa); o administrador habilita, renomeia, define preço, TTL do cache e grupos.
- Custo por chamada, orçamento mensal por usuário, chave e gateway; cache de respostas (Redis ou memória), inclusive em streaming.

| Projeto | Conteúdo |
| --- | --- |
| `gateway-api` | API (`/v1` OpenAI e `/api/v1` do console), agente do Foundry, Foundry simulado (`llmgw-fake-foundry`) |
| `gateway-console` | React 19 + broto-ui (pacote em `vendor/`), nginx que também faz o proxy de `/v1` |
| `dev/ldap`, `dev/vault` | OpenLDAP de exemplo e gravação dos segredos de desenvolvimento no Vault |

Documentação no repositório: `README.md`, `docs/api.md`, `docs/operacao.md` (variáveis, formato do segredo, papéis do Azure, política do Vault, LDAP, proxy, custos), `docs/estado.md`.

## 2. Como rodar

```bash
docker compose up -d --build
uv run python scripts/demo.py
```

Console em http://localhost:8090 (`ana`/`diego` usuários, `bruno` admin, senha `Senha@123`); API OpenAI em `http://localhost:8090/v1`.

## 3. O que foi verificado

| Verificação | Resultado |
| --- | --- |
| Testes da API (Postgres real, Foundry simulado, Vault simulado por HTTP, Redis e OpenLDAP reais) | ✅ 83 testes |
| Ponta a ponta com uvicorn e o SDK oficial da OpenAI (streaming, queda no meio, cliente que desiste) | ✅ |
| Teste de interface (Playwright) atrás do nginx com CSP, 1440 px e 390 px | ✅ 4 fluxos, 18 prints em `docs/screenshots` |
| Revisão de código independente | 21 achados; 20 corrigidos, 1 documentado (orçamento flexível) |
| ruff, `alembic check`, `tsc` e build do console | ✅ |
| Nome da empresa no código, docs e pacote da broto-ui | ✅ nenhuma ocorrência |

**Não verificado:** Azure real, Vault real, imagens Docker e o compose (sem Docker no ambiente; o CI faz isso), Active Directory (só OpenLDAP).

## 4. Pendências

### 4.1 Antes do primeiro uso

- [ ] **Mover o CI**: o arquivo veio em `ci/ci.yml` (a pasta `.github/workflows` não pôde ser gravada remotamente).
  ```bash
  mkdir -p .github/workflows && mv ci/ci.yml .github/workflows/ci.yml && rmdir ci
  ```
- [ ] **Subir o compose** e rodar `uv run pytest tests/ui` uma vez na sua máquina (primeira execução real das imagens).
- [ ] **Criar o repositório no GitHub** e preencher o titular em `LICENSE` (hoje: "os autores do llm-gateway").
- [ ] **Primeiro teste com Azure real**: chave do recurso + `deployments` fixos; depois service principal + descoberta pelo projeto. Conferir os papéis listados em `docs/operacao.md`.
- [ ] **Preços reais** em cada modelo (os do `demo.py` são exemplos).
- [ ] **Configurar na implantação**: `LLMGW_FORWARDED_ALLOW_IPS` (IP/CIDR do proxy), `LLMGW_PROVIDER_ALLOWED_HOSTS` (padrão: nuvens do Azure), `LLMGW_PROVIDER_VAULT_PREFIXES` (se a plataforma provisionar o segredo), grupos do LDAP por DN.

### 4.2 Limitações conhecidas

| Tema | Situação | Sugestão |
| --- | --- | --- |
| Orçamento | Flexível: a chamada que cruza o limite termina, e as que estavam em voo também | Reservar custo máximo antes da chamada (a partir de `max_tokens`) |
| Alertas | Só auditoria e barra do console | E-mail/chat conforme a equipe preferir |
| Rotas OpenAI | Sem imagens, áudio, arquivos, batches e `GET/DELETE /v1/responses/{id}` | Adicionar se houver demanda |
| Cota do deployment | O 429 do Azure chega ao cliente; sem failover entre regiões | Roteamento com mais de um provedor por modelo |
| broto-ui | Instalada de `vendor/broto-ui-0.1.0.tgz`; `StackedBarChart` com `niceMax` mínimo 4 (contornado no console) | Ver pendências da broto-ui |
