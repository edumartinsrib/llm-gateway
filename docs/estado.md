# Estado da implementação

## Implementado e testado

| Área | O que existe | Como foi verificado |
| --- | --- | --- |
| API compatível com a OpenAI | `/v1/chat/completions`, `/v1/embeddings`, `/v1/responses`, `/v1/models`, com e sem streaming; erros no formato da OpenAI | Testes de API contra o Foundry simulado; teste ponta a ponta com uvicorn e o SDK oficial da OpenAI; teste de interface chamando pela porta do nginx |
| Agente do Foundry | Chave de API ou service principal (token em cache por escopo), API v1 e formato clássico, retentativa em 429/5xx antes do primeiro byte, repasse dos cabeçalhos de limite do Azure, recarga da credencial sem reiniciar | Testes unitários e de API com o Foundry simulado, inclusive 429 uma vez, 500, filtro de conteúdo, stream que cai no meio e tempo esgotado |
| Descoberta de deployments | Pelo projeto do Foundry, pelo Azure Resource Manager ou lista fixa; modelos novos entram desabilitados; nomes únicos | Testes de API |
| Vault | KV v2 com token ou AppRole (novo login quando o token expira), segredos por provedor, caminhos externos restritos a prefixos permitidos | Vault simulado por HTTP (KV v2 + AppRole) e cofre em memória |
| LDAP | Busca com conta de serviço ou bind direto, grupos por `memberOf` ou busca, grupos por nome ou DN, sincronização com trava contra sumiço em massa | OpenLDAP real (slapd) com os dados de `dev/ldap`; sincronização com diretório estático |
| Chaves de API | Uma por uso, hash do segredo, validade, orçamento, RPM, modelos, limites travados pelo administrador, revogação imediata | Testes de API e de interface |
| Custos | Custo por chamada com entrada em cache, contadores mensais por usuário, chave e gateway, alerta, estimativa quando o Azure não informa o uso | Testes de API (precisão de 10 casas, valores grandes, entrada em cache mais barata) e ponta a ponta (cliente que desiste no meio) |
| Limites | Requisições por minuto por usuário e por chave, tokens por minuto por usuário, tentativas de login | Testes de API, memória e Redis real |
| Cache | Chat e embeddings, inclusive streaming; por usuário ou global; TTL por modelo; `off`/`refresh` | Testes de API, Redis real e teste de interface (acerto sem custo) |
| Console | Visão geral, playground, modelos, chaves, uso e custos, como usar (curl, Python, JavaScript, LangChain, Pi), usuários, provedores, auditoria | Teste de interface com Playwright atrás do nginx com CSP, em 1440 px e 390 px; prints em `docs/screenshots` |
| Auditoria | Toda escrita, com antes e depois e segredos omitidos | Testes de API |
| Implantação | Dockerfile por projeto, compose com Vault, LDAP e Foundry simulado, CI no GitHub Actions | nginx do console testado localmente (1.24) com a API; migrações sem diferença para os modelos (`alembic check`) |

## Não verificado neste ambiente

- **Azure de verdade.** Nenhuma chamada foi feita a um recurso real. O Foundry simulado
  segue os contratos documentados (API v1, deployments do projeto, ARM, token do Entra
  ID), mas o primeiro uso real deve começar pelo mais simples: chave do recurso e lista
  fixa de deployments; depois, service principal e descoberta pelo projeto. Confira os
  papéis do Azure listados em [operacao.md](operacao.md#permissões-no-azure).
- **Imagens Docker e compose.** Não havia Docker neste ambiente: os Dockerfiles e o
  `docker-compose.yml` não foram executados. O CI constrói as imagens e roda o teste de
  interface contra o compose.
- **Vault real.** Os testes usam um Vault simulado que fala o mesmo HTTP (KV v2 e AppRole).
- **Active Directory.** Testado com OpenLDAP. Faltam: `sAMAccountName`, grupos aninhados
  pela regra `LDAP_MATCHING_RULE_IN_CHAIN` e LDAPS com a CA interna.

## Pendente / próximos passos

- **Preços reais.** Os preços do `demo.py` são exemplos. Cadastre os do seu contrato em
  cada modelo antes de liberar o uso.
- **Orçamento é flexível.** A chamada que cruza o limite termina, e as que estavam em
  andamento também (ver [operacao.md](operacao.md#custos-e-orçamento)). Um teto rígido
  exigiria reservar um custo máximo antes da chamada (a partir de `max_tokens`).
- **Avisos fora do console.** O alerta de orçamento só vai para a auditoria e para a
  barra do console. E-mail ou chat dependem de onde a equipe quer receber.
- **Rotas OpenAI fora do escopo.** Imagens, áudio, arquivos, batches e as rotas
  auxiliares da Responses API (`GET`/`DELETE /v1/responses/{id}`) não são repassadas.
  `previous_response_id` funciona, porque vai no corpo do `POST`.
- **Cota do deployment.** O gateway não divide a cota de tokens por minuto do
  deployment no Azure entre os usuários; quando o Azure recusa, o 429 dele chega ao
  cliente. Failover entre provedores (outra região com o mesmo modelo) também não existe.
- **Estimativa de tokens** quando o Azure não manda o uso (cliente desistiu, stream
  caiu): cerca de 4 caracteres por token, marcada como estimada no histórico.
- **broto-ui.** O console instala a lib pelo pacote em `gateway-console/vendor/` até ela
  ser publicada. O `StackedBarChart` arredonda o topo do eixo para no mínimo 4; com
  custos de centavos, o gráfico ficaria achatado, e o console contorna escalando os
  valores. Vale a lib aceitar escala decimal.
