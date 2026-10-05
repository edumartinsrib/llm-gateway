import {
  Card,
  CodeBlock,
  CopyButton,
  Grid,
  InlineCode,
  KeyValue,
  LinkButton,
  Page,
  PageHeader,
  Table,
  TabPanel,
  Tabs,
} from 'broto-ui';
import { KeyRound } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useModels } from '../api/queries';
import type { Model } from '../api/types';
import { useConfig, useCurrentUser } from '../components/context';
import { formatTtl, formatUsd } from '../lib/format';

type Lang = 'curl' | 'python' | 'node' | 'pi' | 'langchain';

function piConfig(baseUrl: string, models: Model[]): string {
  const chat = models.filter((m) => m.kind === 'chat');
  const config = {
    providers: {
      gateway: {
        baseUrl,
        api: 'openai-completions',
        apiKey: '$LLMGW_API_KEY',
        models: chat.map((m) => ({
          id: m.name,
          name: `${m.name} (gateway)`,
          reasoning: m.supports_reasoning,
          input: m.supports_vision ? ['text', 'image'] : ['text'],
          ...(m.context_window ? { contextWindow: m.context_window } : {}),
          ...(m.max_output_tokens ? { maxTokens: m.max_output_tokens } : {}),
          cost: {
            input: m.input_price,
            output: m.output_price,
            cacheRead: m.cached_input_price ?? m.input_price,
            cacheWrite: 0,
          },
        })),
      },
    },
  };
  return JSON.stringify(config, null, 2);
}

export function QuickstartPage() {
  const config = useConfig();
  const me = useCurrentUser();
  const models = useModels();
  const [lang, setLang] = useState<Lang>('curl');
  const base = config.api_base_url;
  const chat = models.data?.find((m) => m.kind === 'chat')?.name ?? 'gpt-4.1';
  const embed = models.data?.find((m) => m.kind === 'embeddings')?.name ?? 'text-embedding-3-small';

  const snippets: Record<Lang, string> = useMemo(
    () => ({
      curl: `curl ${base}/chat/completions \\
  -H "Authorization: Bearer $LLMGW_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${chat}",
    "messages": [{"role": "user", "content": "Olá!"}]
  }'

# Lista os modelos liberados para a sua chave
curl ${base}/models -H "Authorization: Bearer $LLMGW_API_KEY"`,
      python: `# pip install openai
import os
from openai import OpenAI

client = OpenAI(base_url="${base}", api_key=os.environ["LLMGW_API_KEY"])

resposta = client.chat.completions.create(
    model="${chat}",
    messages=[{"role": "user", "content": "Olá!"}],
)
print(resposta.choices[0].message.content)

# Streaming
for parte in client.chat.completions.create(
    model="${chat}",
    messages=[{"role": "user", "content": "Conte até 5"}],
    stream=True,
):
    if parte.choices:
        print(parte.choices[0].delta.content or "", end="")

# Embeddings
vetores = client.embeddings.create(model="${embed}", input=["texto 1", "texto 2"])`,
      node: `// npm install openai
import OpenAI from 'openai';

const client = new OpenAI({ baseURL: '${base}', apiKey: process.env.LLMGW_API_KEY });

const resposta = await client.chat.completions.create({
  model: '${chat}',
  messages: [{ role: 'user', content: 'Olá!' }],
});
console.log(resposta.choices[0].message.content);`,
      pi: piConfig(base, models.data ?? []),
      langchain: `# pip install langchain-openai
import os
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

llm = ChatOpenAI(model="${chat}", base_url="${base}", api_key=os.environ["LLMGW_API_KEY"])
emb = OpenAIEmbeddings(model="${embed}", base_url="${base}", api_key=os.environ["LLMGW_API_KEY"],
                       check_embedding_ctx_length=False)

print(llm.invoke("Olá!").content)`,
    }),
    [base, chat, embed, models.data],
  );

  return (
    <Page>
      <PageHeader
        crumbs={[{ label: 'Início', href: '/' }, { label: 'Como usar' }]}
        title="Como usar"
        description="O gateway fala a API da OpenAI: troque o endereço e a chave no seu cliente e pronto."
        actions={
          <LinkButton href="/chaves" variant="primary" icon={<KeyRound size={16} />}>
            Criar chave
          </LinkButton>
        }
      />

      <Grid layout="main-side">
        <Card title="1. Endereço e chave">
          <div className="bt-stack">
            <div className="gw-endpoint">
              <code className="gw-endpoint-url">{base}</code>
              <CopyButton value={base} label="Copiar endereço" />
            </div>
            <p className="bt-muted">
              Crie uma chave em <strong>Chaves de API</strong> e guarde numa variável de ambiente, por exemplo{' '}
              <InlineCode>LLMGW_API_KEY</InlineCode>. Ela vai no cabeçalho <InlineCode>Authorization: Bearer</InlineCode>{' '}
              (o cabeçalho <InlineCode>api-key</InlineCode>, do Azure, também é aceito).
            </p>
            <p className="bt-muted">
              Rotas: <InlineCode>/chat/completions</InlineCode> (com streaming), <InlineCode>/responses</InlineCode>,{' '}
              <InlineCode>/embeddings</InlineCode> e <InlineCode>/models</InlineCode>.
            </p>
          </div>
        </Card>
        <Card title="Seus limites">
          <KeyValue
            items={[
              ['Orçamento mensal', me.monthly_budget_usd === null ? 'Sem limite' : formatUsd(me.monthly_budget_usd)],
              ['Requisições por minuto', me.rpm_limit ?? 'Padrão do gateway'],
              ['Tokens por minuto', me.tpm_limit ?? 'Padrão do gateway'],
              ['Modelos', me.allowed_models ? me.allowed_models.join(', ') : 'Todos liberados para os seus grupos'],
              ['Cache de respostas', config.cache.enabled ? `Ligado (${config.cache.scope === 'user' ? 'só seu' : 'compartilhado'})` : 'Desligado'],
            ]}
          />
        </Card>
      </Grid>

      <Card title="2. Chame um modelo" flush>
        <Tabs
          inset
          idPrefix="quick"
          value={lang}
          onChange={setLang}
          tabs={[
            { id: 'curl', label: 'curl' },
            { id: 'python', label: 'Python' },
            { id: 'node', label: 'JavaScript' },
            { id: 'langchain', label: 'LangChain' },
            { id: 'pi', label: 'Pi' },
          ]}
        />
        <TabPanel id={lang} idPrefix="quick">
          <div className="gw-tab-body bt-stack">
            {lang === 'pi' && (
              <p className="bt-muted">
                Salve como <InlineCode>~/.pi/agent/models.json</InlineCode> (ou junte ao arquivo que você já tem). Os modelos e
                preços abaixo são os liberados para você agora.
              </p>
            )}
            <CodeBlock tall>{snippets[lang]}</CodeBlock>
          </div>
        </TabPanel>
      </Card>

      <Card title="3. Cache, custos e cabeçalhos" flush>
        <Table>
          <thead>
            <tr>
              <th>Cabeçalho</th>
              <th>O que faz</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>
                <InlineCode>Cache-Control: no-cache</InlineCode>
              </td>
              <td>Ignora a resposta guardada e chama o modelo de novo (a nova resposta substitui a antiga).</td>
            </tr>
            <tr>
              <td>
                <InlineCode>X-LLMGW-Cache: off</InlineCode>
              </td>
              <td>Nem lê nem grava no cache do gateway.</td>
            </tr>
            <tr>
              <td>
                <InlineCode>x-llmgw-cache</InlineCode> (resposta)
              </td>
              <td>
                <InlineCode>hit</InlineCode>, <InlineCode>miss</InlineCode>, <InlineCode>bypass</InlineCode> ou{' '}
                <InlineCode>off</InlineCode>. Resposta do cache custa zero.
              </td>
            </tr>
            <tr>
              <td>
                <InlineCode>x-llmgw-cost-usd</InlineCode> (resposta)
              </td>
              <td>Custo da chamada em dólares (só em respostas sem streaming).</td>
            </tr>
            <tr>
              <td>
                <InlineCode>x-request-id</InlineCode> (resposta)
              </td>
              <td>Identifica a chamada na tela de uso.</td>
            </tr>
          </tbody>
        </Table>
        <div className="gw-tab-body bt-muted bt-small">
          O cache vale para requisições idênticas (mesmo modelo, mensagens e parâmetros). Tempo padrão: chat{' '}
          {formatTtl(config.cache.chat_ttl_s)}, embeddings {formatTtl(config.cache.embeddings_ttl_s)}. Quando o orçamento do mês
          acaba, o gateway responde <InlineCode>429 insufficient_quota</InlineCode>.
        </div>
      </Card>
    </Page>
  );
}
