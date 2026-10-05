import {
  Button,
  Callout,
  Card,
  EmptyState,
  Field,
  Input,
  LinkButton,
  Page,
  PageHeader,
  Select,
  Switch,
  Tag,
  Textarea,
  formatNumber,
} from 'broto-ui';
import { Eraser, MessagesSquare, Send, Square } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { authHeaders, handleUnauthorized, toApiError } from '../api/client';
import { useModels } from '../api/queries';
import type { Model } from '../api/types';
import { formatMs, formatUsd } from '../lib/format';

interface Turn {
  role: 'user' | 'assistant';
  content: string;
  meta?: {
    model: string;
    prompt_tokens?: number;
    completion_tokens?: number;
    cost?: number;
    approx?: boolean;
    cache?: string | null;
    latency_ms: number;
    error?: string;
  };
}

function estimate(model: Model | undefined, usage: { prompt_tokens: number; completion_tokens: number; prompt_tokens_details?: { cached_tokens?: number } }) {
  if (!model) return undefined;
  const cached = usage.prompt_tokens_details?.cached_tokens ?? 0;
  const cachedPrice = model.cached_input_price ?? model.input_price;
  return ((usage.prompt_tokens - cached) * model.input_price + cached * cachedPrice + usage.completion_tokens * model.output_price) / 1_000_000;
}

export function PlaygroundPage() {
  const [params, setParams] = useSearchParams();
  const models = useModels();
  const chatModels = useMemo(() => (models.data ?? []).filter((m) => m.kind === 'chat' && m.accessible), [models.data]);
  const modelName = params.get('modelo') ?? chatModels[0]?.name ?? '';
  const model = chatModels.find((m) => m.name === modelName);
  const [system, setSystem] = useState('Você é um assistente prestativo. Responda em português.');
  const [temperature, setTemperature] = useState('');
  const [stream, setStream] = useState(true);
  const [input, setInput] = useState('');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [turns]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const send = async () => {
    const text = input.trim();
    if (!text || !modelName || busy) return;
    const history: Turn[] = [...turns.filter((t) => !t.meta?.error), { role: 'user', content: text }];
    setTurns([...history, { role: 'assistant', content: '' }]);
    setInput('');
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const started = performance.now();
    const temp = Number(temperature.replace(',', '.'));
    const body = {
      model: modelName,
      stream,
      ...(stream ? { stream_options: { include_usage: true } } : {}),
      ...(temperature.trim() && !Number.isNaN(temp) ? { temperature: temp } : {}),
      messages: [
        ...(system.trim() ? [{ role: 'system', content: system.trim() }] : []),
        ...history.map((t) => ({ role: t.role, content: t.content })),
      ],
    };
    const finish = (patch: Partial<Turn>) =>
      setTurns((all) => {
        const copy = [...all];
        copy[copy.length - 1] = { ...copy[copy.length - 1], ...patch };
        return copy;
      });
    try {
      const res = await fetch('/api/v1/playground/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (res.status === 401) handleUnauthorized();
      if (!res.ok) throw await toApiError(res);
      const cache = res.headers.get('x-llmgw-cache');
      if (!stream) {
        const data = await res.json();
        finish({
          content: data.choices?.[0]?.message?.content ?? '',
          meta: {
            model: modelName,
            prompt_tokens: data.usage?.prompt_tokens,
            completion_tokens: data.usage?.completion_tokens,
            cost: Number(res.headers.get('x-llmgw-cost-usd') ?? 0),
            cache,
            latency_ms: performance.now() - started,
          },
        });
        return;
      }
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let content = '';
      let usage: { prompt_tokens: number; completion_tokens: number } | null = null;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        buffer = events.pop() ?? '';
        for (const event of events) {
          const data = event
            .split('\n')
            .filter((l) => l.startsWith('data:'))
            .map((l) => l.slice(5).trim())
            .join('\n');
          if (!data || data === '[DONE]') continue;
          const chunk = JSON.parse(data);
          if (chunk.error) throw new Error(chunk.error.message ?? 'falha no stream');
          if (chunk.usage) usage = chunk.usage;
          const delta = chunk.choices?.[0]?.delta?.content;
          if (delta) {
            content += delta;
            finish({ content });
          }
        }
      }
      finish({
        content,
        meta: {
          model: modelName,
          prompt_tokens: usage?.prompt_tokens,
          completion_tokens: usage?.completion_tokens,
          cost: usage ? estimate(model, usage) : undefined,
          approx: true,
          cache,
          latency_ms: performance.now() - started,
        },
      });
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError';
      finish({
        meta: {
          model: modelName,
          latency_ms: performance.now() - started,
          error: aborted ? 'Interrompido.' : e instanceof Error ? e.message : 'Falha na chamada.',
        },
      });
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  };

  return (
    <Page>
      <PageHeader
        crumbs={[{ label: 'Início', href: '/' }, { label: 'Playground' }]}
        title="Playground"
        description="Converse com um modelo pelo gateway. As chamadas usam o seu orçamento e aparecem no seu uso com a chave 'console'."
      />

      {models.isSuccess && !chatModels.length ? (
        <Card>
          <EmptyState
            icon={<MessagesSquare size={24} />}
            title="Nenhum modelo de chat liberado para você"
            description="Peça ao administrador do gateway para habilitar um modelo para o seu grupo."
            action={
              <LinkButton href="/modelos" variant="secondary">
                Ver modelos
              </LinkButton>
            }
          />
        </Card>
      ) : (
        <div className="gw-playground">
          <Card title="Configuração" className="gw-playground-side">
            <div className="bt-stack">
              <Field label="Modelo">
                <Select
                  value={modelName}
                  onChange={(e) => setParams({ modelo: e.target.value }, { replace: true })}
                  disabled={busy}
                >
                  {chatModels.map((m) => (
                    <option key={m.id} value={m.name}>
                      {m.name}
                    </option>
                  ))}
                </Select>
              </Field>
              {model && (
                <div className="bt-small bt-muted">
                  Entrada {formatUsd(model.input_price)} · saída {formatUsd(model.output_price)} por 1 milhão de tokens
                </div>
              )}
              <Field label="Instruções do sistema">
                <Textarea rows={5} value={system} onChange={(e) => setSystem(e.target.value)} />
              </Field>
              <Field label="Temperatura" hint="Vazio: padrão do modelo. Modelos de raciocínio não aceitam.">
                <Input inputMode="decimal" value={temperature} placeholder="ex.: 0.2" onChange={(e) => setTemperature(e.target.value)} />
              </Field>
              <Switch checked={stream} onCheckedChange={setStream} label="Resposta em streaming" />
              <Button variant="ghost" icon={<Eraser size={16} />} disabled={busy || !turns.length} onClick={() => setTurns([])}>
                Limpar conversa
              </Button>
            </div>
          </Card>

          <Card className="gw-chat" bare>
            <div className="gw-chat-log" aria-live="polite">
              {!turns.length && (
                <EmptyState
                  compact
                  icon={<MessagesSquare size={22} />}
                  title="Comece uma conversa"
                  description="Ctrl+Enter envia. Respostas iguais podem vir do cache do gateway."
                />
              )}
              {turns.map((t, i) => (
                <div key={i} className={`gw-turn gw-turn-${t.role}`}>
                  <div className="gw-bubble">{t.content || (busy && i === turns.length - 1 ? '…' : '')}</div>
                  {t.meta && (
                    <div className="gw-turn-meta">
                      {t.meta.error ? (
                        <Callout tone="danger">{t.meta.error}</Callout>
                      ) : (
                        <>
                          <span className="bt-mono">{t.meta.model}</span>
                          {t.meta.prompt_tokens !== undefined && (
                            <span>
                              {formatNumber(t.meta.prompt_tokens)} + {formatNumber(t.meta.completion_tokens ?? 0)} tokens
                            </span>
                          )}
                          {t.meta.cost !== undefined && (
                            <span>
                              {t.meta.approx ? '≈ ' : ''}
                              {formatUsd(t.meta.cache === 'hit' ? 0 : t.meta.cost)}
                            </span>
                          )}
                          <span>{formatMs(t.meta.latency_ms)}</span>
                          {t.meta.cache === 'hit' && <Tag variant="green">do cache</Tag>}
                        </>
                      )}
                    </div>
                  )}
                </div>
              ))}
              <div ref={endRef} />
            </div>
            <form
              className="gw-composer"
              onSubmit={(e) => {
                e.preventDefault();
                void send();
              }}
            >
              <Textarea
                rows={3}
                value={input}
                placeholder="Escreva uma mensagem"
                aria-label="Mensagem"
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                    e.preventDefault();
                    void send();
                  }
                }}
              />
              {busy ? (
                <Button type="button" variant="secondary" icon={<Square size={16} />} onClick={() => abortRef.current?.abort()}>
                  Parar
                </Button>
              ) : (
                <Button type="submit" variant="primary" icon={<Send size={16} />} disabled={!input.trim() || !modelName}>
                  Enviar
                </Button>
              )}
            </form>
          </Card>
        </div>
      )}
    </Page>
  );
}
