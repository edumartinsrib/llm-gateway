# gateway-console

Console web do **Gateway LLM**: onde cada pessoa cria as próprias chaves, testa os
modelos e acompanha uso e custo, e onde os administradores cuidam de modelos, preços,
provedores, usuários e auditoria.

- Vite + React 19 + TypeScript (strict)
- react-router 7, @tanstack/react-query 5, lucide-react
- Componentes e identidade visual da **broto-ui** (instalada de `vendor/broto-ui-0.1.0.tgz`
  até a lib ser publicada)
- Fontes Exo 2 e Nunito empacotadas com `@fontsource-variable` (sem Google Fonts, que
  costuma ser bloqueado em redes corporativas)
- Contrato da API: [docs/api.md](../docs/api.md) (tipos em `src/api/types.ts`)

## Telas

| Rota | Quem | O que tem |
| --- | --- | --- |
| `/login` | todos | Usuário e senha do LDAP (no modo `dev`, só o usuário) |
| `/` | todos | Gasto do mês, chamadas, erros, tokens, cache, latência, custo por dia e modelo, últimas chamadas; administradores alternam entre o gateway inteiro e o próprio uso |
| `/playground` | todos | Conversa com streaming com os modelos de chat liberados, usando a sessão (sem chave) |
| `/modelos` | todos | Modelos liberados, preços e capacidades; administradores editam, habilitam e cadastram |
| `/chaves` | todos | Criar (o segredo aparece uma vez), editar limites, revogar |
| `/uso` | todos | Histórico com filtros, detalhe de cada chamada e exportação CSV |
| `/como-usar` | todos | Endereço, exemplos em curl, Python, JavaScript, LangChain e o `models.json` do Pi com os modelos liberados |
| `/admin/usuarios` | admin | Usuários, orçamento, limites, bloqueio, chaves de cada um, sincronizar com o LDAP |
| `/admin/provedores` | admin | Provedores do Foundry, teste de conexão, descoberta, troca da credencial |
| `/admin/auditoria` | admin | Tudo o que mudou, com antes e depois |

## Requisitos

- Node.js 20.19 ou mais novo e npm 10
- API do gateway em `http://localhost:8000` (o compose expõe essa porta)

## Desenvolvimento

```bash
npm install
npm run dev        # http://localhost:5174
```

O Vite faz proxy de `/api` e `/v1` para `http://localhost:8000`. Para outro endereço:

```bash
LLMGW_API_UPSTREAM=http://minha-api:8000 npm run dev
```

O modo de login vem de `GET /api/v1/config`. A sessão (JWT da API) fica no
`sessionStorage` da aba; **Sair** encerra as sessões do usuário no servidor.

## Build

```bash
npm run build      # tsc -b && vite build → dist/
npm run preview    # serve o dist/ em http://localhost:4174 (com o mesmo proxy)
npm run lint       # checagem de tipos (tsc -b)
```

## Docker

Imagem multi-stage: build com `node:20-alpine`, servida por `nginx:1.27-alpine` na porta
**8080**.

```bash
docker build -t gateway-console .
docker run --rm -p 8090:8080 -e LLMGW_API_UPSTREAM=http://gateway-api:8000 gateway-console
```

O nginx (`nginx/default.conf.template`, processado pelo envsubst nativo da imagem) é
também a porta de entrada da API OpenAI: `/v1` e `/api` vão para a API sem buffer (o
streaming chega token a token), com timeouts de 15 minutos e corpo de até 50 MB. Detalhes
em [docs/operacao.md](../docs/operacao.md#gateway-console).

## Atualizar a broto-ui

Gere o pacote na pasta da lib (`npm pack`), copie o `.tgz` para `vendor/` e rode
`npm install ./vendor/broto-ui-<versão>.tgz`. Quando a lib estiver publicada, troque a
dependência em `package.json` pelo endereço do GitHub ou do npm e apague `vendor/`
(lembre de tirar o `COPY vendor` do Dockerfile).
